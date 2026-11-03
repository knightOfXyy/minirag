"""
分块 map-reduce 抽取流水线（L05）
=================================

解决大文件的三层问题：
  1. 超限  -> 切块，每块单独抽；
  2. 溯源  -> 块内 span 是"块内偏移"，统一 + chunk.start 换算成【全局偏移】；
  3. 并发/成本 -> 用信号量限流，单块失败可重试，不影响其他块。

注意：本模块先解决"切得开 + 偏移准 + 抽得到"。跨块的【实体归并/共指消解】
（"华为/华为公司"合并、overlap 重复去重）在 L07/L09 用向量相似度处理；
这里先做一个朴素的"按标准名聚合"兜底，并给每个实体保留全部 mention，
为后续消歧保留原始信息。
"""

from __future__ import annotations

import asyncio
import logging
import time

from ..schema import (
    DomainSpec,
    Entity,
    EntityMention,
    ExtractionResult,
    Relation,
    Span,
    domain_fingerprint,
)
from .chunk_metrics import (
    FINISH_CACHE_HIT,
    FINISH_UNKNOWN,
    ChunkCallMetrics,
    DocumentMetrics,
)
from .chunking import Chunk
from .document import CacheSavings, Document, ExtractionOutput, Usage
from .extractor import ExtractionError, Extractor
from .local_backfill import (
    BackfillReport,
    max_confidence,
    merge_backfill_reports,
    merge_entity_metadata,
)

logger = logging.getLogger(__name__)


def remap_spans(result: ExtractionResult, chunk: Chunk) -> ExtractionResult:
    """把"块内偏移"换算成"文档全局偏移"。

    chunk 是原文 text[chunk.start:chunk.end]，模型在块文本上从 0 计数，
    因此所有 span 只需平移 chunk.start；越界的 span 置空（交给质量探针标记）。
    同时给 mention 打上块来源，方便排查。
    """
    def shift(span: Span | None) -> Span | None:
        if span is None:
            return None
        start = span.start + chunk.start
        end = span.end + chunk.start
        if not (chunk.start <= start < end <= chunk.end):
            return None  # 块内偏移不自洽，宁可不保留也不写错位置
        return Span(start=start, end=end, text=span.text)

    entities = [
        Entity(
            name=e.name,
            type=e.type,
            aliases=list(e.aliases),
            mentions=[
                EntityMention(surface=m.surface, span=shift(m.span))
                for m in e.mentions
            ],
            confidence=e.confidence,
            attributes=dict(e.attributes),
        )
        for e in result.entities
    ]

    relations = [
        Relation(
            head=r.head,
            predicate=r.predicate,
            tail=r.tail,
            evidence_span=shift(r.evidence_span),
            confidence=r.confidence,
        )
        for r in result.relations
    ]
    return ExtractionResult(entities=entities, relations=relations)


def naive_merge(
    partials: list[ExtractionResult], *,
    merge_relation_duplicates: bool = False,
    fill_description: bool = False,
) -> ExtractionResult:
    """朴素归并（L09 会升级为向量+LLM 共指消解）。

    规则：实体按 (type, 标准名) 聚合；别名并集；mention 全部保留。
    关系按三元组字符串去重，置信度取最大值。
    overlap 区域产生的重复 mention 会自然落在同一实体下，不会重复造实体。

    ``merge_relation_duplicates``：重复三元组是否把后见的证据/更高置信度
    并进首见条目。默认 **False**，即 262319b 的「先见赢家」旧语义——本函数
    是公开 helper（``extraction/__init__`` 导出、lessons 直接调用），默认值
    不能悄悄改变直接调用方的输出。分块流水线只在瘦身开关打开时显式传 True。

    ``fill_description``：是否用后见条目的 description 补全空描述。与关系
    合并是两个独立行为（r4 N6）：此前它隐式跟随
    ``merge_relation_duplicates``，改关系口径会莫名改变实体描述。默认
    False，保持 262319b 旧输出；分块流水线在瘦身开关打开时显式传 True。
    """
    bucket: dict[tuple[str, str], Entity] = {}
    for part in partials:
        for e in part.entities:
            key = (e.type, e.name)
            if key not in bucket:
                bucket[key] = Entity(
                    name=e.name, type=e.type,
                    aliases=[], mentions=[],
                    confidence=e.confidence, attributes=dict(e.attributes),
                )
            ent = bucket[key]
            # 与 local_backfill 共用同一套元数据合并规则（别名并集/属性首见/
            # 置信度取大），避免两处逻辑漂移；mention 在归并层必须全量保留。
            # description 补全由独立开关控制：开关关闭时必须保持 262319b 旧输出。
            merge_entity_metadata(ent, e, fill_description=fill_description)
            ent.mentions.extend(e.mentions)

    rel_index: dict[tuple[str, str, str], Relation] = {}
    relations: list[Relation] = []
    for part in partials:
        for r in part.relations:
            key = (r.head, r.predicate, r.tail)
            existing = rel_index.get(key)
            if existing is not None:
                if merge_relation_duplicates:
                    # 同一三元组在别的块可能才拿到证据/更高置信度：
                    # 首见不能赢家通吃。
                    if existing.evidence_span is None and r.evidence_span is not None:
                        existing.evidence_span = r.evidence_span
                    existing.confidence = max_confidence(
                        existing.confidence, r.confidence)
                continue
            rel_index[key] = r
            relations.append(r)

    return ExtractionResult(entities=list(bucket.values()), relations=relations)


class ChunkedExtractionPipeline:
    """分块 map 抽取 + 偏移换算 + 朴素归并 的流水线。"""

    def __init__(
        self,
        extractor: Extractor,
        *,
        concurrency: int = 4,
        warm_prefix: bool = False,
        result_cache=None,
    ) -> None:
        self.extractor = extractor
        self.sem = asyncio.Semaphore(concurrency)
        # 提示缓存预热：多块并发前先串行发一发探测请求，免得 N 个请求同时
        # miss、各自付全价 prefill（Extractor 按领域版本去重，只探一次）。
        # 默认关：实测（2026-10-22）真实抽块路径 cached 仍为 0，裸探测只在部分
        # 请求命中（多副本、无会话亲和），收益未确立；而探测本身要付一次完整
        # prefill + 思考输出。先跑 lessons/prompt_cache_probe.py --live 统计出
        # 命中率，再决定是否常开。
        self.warm_prefix = warm_prefix
        # 分块结果内容级缓存（纯加速层，默认 None=行为与无缓存逐字节一致）。
        # 键=领域配置指纹+模型名+块正文；命中不调模型，用量记 cache_hits 而非
        # 真实 token；同键并发在缓存内部单飞去重。
        self.result_cache = result_cache
        # 领域指纹只依赖抽取器配置，一次 pipeline.run 内不会变：构造时算一次，
        # 避免每个分块都重建 pydantic 模型 + JSON schema（N2）。
        self._cached_domain_fingerprint = (
            self._domain_fingerprint() if result_cache is not None else None)

    @property
    def slim(self) -> bool:
        """当前抽取器是否处于瘦身后回填模式（r5 F8：单一判定入口）。"""
        return bool(getattr(self.extractor, "local_backfill", False))

    @property
    def _sampling_fingerprint(self) -> str:
        """影响输出的采样参数摘要；进缓存键，避免换温度后复用旧产物（r5 F7）。"""
        return getattr(self.extractor, "sampling_fingerprint", "") or ""

    def _domain_fingerprint(self) -> str:
        """当前抽取器所用领域配置的版本指纹。

        真 Extractor 持有完整 DomainSpec，走内容级指纹（system prompt + schema）。
        对不是 DomainSpec 的假对象，回退值必须包含该对象的完整 repr：只退化成
        code 会让“同 code、不同枚举”的配置共用缓存键（N3）。
        """
        domain = getattr(self.extractor, "domain", None)
        # 回填模式（slim/full）换 prompt 与 schema，产物形状不同，必须进指纹；
        # 否则共享缓存会把 slim 的实体/backfill 发给关闭态（r4 B1）。
        slim = self.slim
        if isinstance(domain, DomainSpec):
            return domain_fingerprint(domain, slim=slim)
        return (
            f"code:{getattr(domain, 'code', 'unknown')}"
            f"|backfill={slim}|repr:{domain!r}"
        )

    async def _extract_chunk_output(
        self, mini_doc: Document, chunk: Chunk,
    ) -> tuple[ExtractionOutput, bool]:
        """取该块的【块内】抽取产物，返回 (产物, 是否命中内容级缓存)。

        有缓存时以 (领域指纹, 模型名, 块正文) 为键查/单飞；命中则不发起模型调用。
        偏移换算（remap_spans）在缓存之外做，命中与未命中的结果语义完全一致。
        """
        if self.result_cache is None:
            return await self.extractor.extract(mini_doc), False

        fingerprint = self._cached_domain_fingerprint
        model_name = getattr(self.extractor, "model_name", "") or ""
        sampling = self._sampling_fingerprint

        async def _do_extract() -> ExtractionOutput:
            return await self.extractor.extract(mini_doc)

        return await self.result_cache.get_or_extract(
            fingerprint, model_name, chunk.text, _do_extract,
            sampling_fingerprint=sampling)

    async def _extract_one_chunk(
        self, chunk: Chunk, metrics: DocumentMetrics,
    ) -> tuple[
        ExtractionResult | None, Usage, str, ChunkCallMetrics | None, dict | None,
    ]:
        """抽单个块（带限流）。

        返回 (换算后的结果|None, 用量, 块 id, 分块度量, 回填信号|None)。结果为
        None 表示该块多次尝试后仍失败（度量里带 error/截断信号），由调用方决定
        是否容忍。

        缓存（05）× 度量（01）× 回填（03）的交叉语义：命中缓存不发起模型调用、
        不计真实 token（用量只记一次 cache_hits，被省下的 token 进 cache_savings），
        但仍为该块落一条
        ChunkCallMetrics（finish_reason=cache_hit、attempts=0、token 全 0）——
        文档级"每块一条"的口径不能被缓存打破，否则 chunk_calls 会随缓存命中率
        漂移、看板失去可比性。回填信号是 03 的旁路可观测信息，作为额外的第 5 个
        返回值透出，不写回内核 Entity/Relation.confidence；命中缓存时随缓存产物
        一并还原（同一块同一领域同一模型，回填结果确定）。
        """
        # 排队耗时：从 worker 被创建（事件循环开始调度它）到拿到信号量。
        # 这是"时间花在哪"的第一跳——并发配额/RPM 限流造成的等待都在这里显形。
        queued_at = time.perf_counter()
        async with self.sem:
            wait_s = time.perf_counter() - queued_at
            # 复用 Extractor：把一个 chunk 当成一篇微型文档
            mini_doc = Document(
                id=chunk.id,
                text=chunk.text,
                source="chunk",
                domain_code=self.extractor.domain.code,
            )
            # 缓存与外层 try 的顺序很关键：命中缓存时不会抛抽取异常，而缓存
            # 未命中时 Extractor 的 ExtractionError 必须走下面同一条失败路径
            # （把该块已花 token 计入文档用量 + 把度量挂到异常上），不能被缓存层吞掉。
            try:
                output, hit = await self._extract_chunk_output(mini_doc, chunk)
            except ExtractionError as exc:
                record = exc.metrics
                if record is None:
                    # 兜底：底层异常没带度量时也要留一条 unknown 记录，
                    # 否则 chunk_calls < 块数，聚合会静默少算一块。
                    logger.warning(
                        "分块失败但异常未带度量，补一条 unknown 记录 "
                        "chunk_id=%s error=%r", chunk.id, str(exc))
                    record = ChunkCallMetrics(
                        chunk_id=chunk.id, attempts=1,
                        finish_reason=FINISH_UNKNOWN,
                        input_tokens=0, output_tokens=0,
                        cached_input_tokens=0, wait_s=wait_s,
                        prefill_s=None, decode_s=None,
                        error=str(exc))
                else:
                    # Extractor 不知道排队耗时，在这一层补上
                    record.wait_s = wait_s
                metrics.add(record)
                if record.truncated:
                    logger.error(
                        "分块最终因长度截断失败 event=chunk_truncated "
                        "chunk_id=%s attempts=%d out_tokens=%d truncated=True",
                        chunk.id, record.attempts, record.output_tokens,
                        extra={"event": "chunk_truncated", "truncated": True,
                               "chunk_id": chunk.id,
                               "attempts": record.attempts},
                    )
                # 失败块已经花掉的 token 也要进文档用量：只记成功块会让
                # 成本被系统性低估（尤其在截断导致整块返工失败时）。
                failed_usage = Usage(
                    input_tokens=record.input_tokens,
                    output_tokens=record.output_tokens,
                    cached_input_tokens=record.cached_input_tokens,
                ) if record is not None else Usage()
                return None, failed_usage, chunk.id, record, None

            if hit:
                # 命中：还原结果但不计真实 token，只标记一次缓存来源；
                # 被省下的 input/output 记到独立的 cache_savings 命名空间（S4/N2），
                # 成本/看板只汇总真实计量字段。
                # 度量仍要为该块落一条（见方法 docstring 的口径说明）。
                m = ChunkCallMetrics(
                    chunk_id=chunk.id,
                    attempts=0,
                    finish_reason=FINISH_CACHE_HIT,
                    input_tokens=0,
                    output_tokens=0,
                    cached_input_tokens=0,
                    wait_s=wait_s,
                    prefill_s=None,
                    decode_s=None,
                )
                metrics.add(m)
                return (remap_spans(output.result, chunk),
                        Usage(
                            cache_hits=1,
                            # 05 r2：省下的量收进独立命名空间（N2），不污染真实 token
                            cache_savings=CacheSavings(
                                input_tokens=output.usage.input_tokens,
                                output_tokens=output.usage.output_tokens,
                            ),
                        ), chunk.id, m, output.backfill)

            m = output.call_metrics
            if m is not None:
                m.chunk_id = chunk.id
                m.wait_s = wait_s
            else:
                # 不经标准度量路径的兜底（正常 Extractor 不会走到这）
                m = ChunkCallMetrics(
                    chunk_id=chunk.id, attempts=output.attempts,
                    finish_reason=FINISH_UNKNOWN,
                    input_tokens=output.usage.input_tokens,
                    output_tokens=output.usage.output_tokens,
                    cached_input_tokens=output.usage.cached_input_tokens,
                    wait_s=wait_s, prefill_s=None, decode_s=None)
            metrics.add(m)
            return (remap_spans(output.result, chunk), output.usage, chunk.id, m,
                    output.backfill)

    async def run(
        self,
        document: Document,
        chunks: list[Chunk],
        *,
        on_chunk=None,
        raise_on_chunk_error: bool = True,
    ) -> ExtractionOutput:
        """并发抽取所有块，换算偏移后归并。on_chunk(done,total,chunk_id) 可做进度回调。

        raise_on_chunk_error：某块重试耗尽时默认抛异常（与历史行为一致）；
        批量/看板路径需要"失败也落一条度量"时传 False，失败块被跳过、其余块
        照常归并，失败信息留在 document_metrics 里。
        """
        total = len(chunks)
        usage = Usage()
        metrics = DocumentMetrics(document_id=document.id, n_chunks=total)

        # 只有多块才值得预热（单块文档预热等于白烧一次前缀）。
        # 探测请求本身的开销记在触发它这篇文档上：宁可多记，不要漏记成本。
        # 但若所有块都已命中缓存，预热会凭空造出一次真实模型调用，与“第二次
        # 零调用”冲突（N3）：先只读探测一遍，全命中就跳过预热。
        if self.warm_prefix and total >= 2:
            all_cached = False
            if self.result_cache is not None:
                model_name = getattr(self.extractor, "model_name", "") or ""
                all_cached = all([
                    await self.result_cache.has_cached(
                        self._cached_domain_fingerprint, model_name, c.text,
                        sampling_fingerprint=self._sampling_fingerprint)
                    for c in chunks
                ])
            if not all_cached:
                warm_usage = await self.extractor.warm_prefix()
                usage.add(warm_usage)
                metrics.warmup = warm_usage.model_dump()

        async def worker(chunk: Chunk):
            result = await self._extract_one_chunk(chunk, metrics)
            res, _usage, cid, _record, _backfill = result
            # 恢复"每块完成即回调"的进度语义（改动工单前就是这样）：
            # 失败块仍不回调，但成功块不再等到全部 gather 完才通知。
            if res is not None and on_chunk:
                on_chunk(res, cid, total)
            return result

        settled = await asyncio.gather(*(worker(c) for c in chunks))

        partials: list[ExtractionResult] = []
        failed: list[str] = []
        backfill_reports: list[BackfillReport] = []
        chunk_by_id = {c.id: c for c in chunks}
        for res, u, cid, _m, raw_backfill in settled:
            usage.add(u)
            # 分块回填信号在这里汇总（03）：先收集，等归并后再统一聚合，
            # 失败块没有信号（None）直接跳过。块内偏移在此一次性平移到
            # 文档全局坐标，并带上 chunk_id，看板才能定位到具体块。
            if raw_backfill is not None:
                ch = chunk_by_id.get(cid)
                backfill_reports.append(BackfillReport.from_dict(
                    raw_backfill,
                    chunk_start=ch.start if ch is not None else 0,
                    chunk_id=cid))
            if res is None:
                failed.append(cid)
                continue
            partials.append(res)

        if failed and raise_on_chunk_error:
            # 失败语义与改动前一致（整篇抛错），但度量不能随异常一起蒸发：
            # 挂上完整的文档级度量，调用方仍能读到截断块数/返工率。
            err = ExtractionError(
                f"文档 {document.id} 有 {len(failed)}/{total} 个分块抽取失败："
                f"{failed[:5]}；分块度量见 document_metrics（可能含截断信号）")
            err.document_metrics = metrics  # type: ignore[attr-defined]
            raise err

        # 归并口径跟抽取器开关走：关开关时保持 262319b 的旧语义
        # （同一三元组首见赢家），打开才合并后见证据/更高置信度（03 r2）。
        # 抽取器没有该属性时按「关」处理，保证旧对象逐字段等于改造前。
        slim = self.slim
        merged = naive_merge(
            partials,
            merge_relation_duplicates=slim,
            fill_description=slim)
        # 分块回填信号必须汇总后继续往上层传，不能在 pipeline 这里丢掉。
        # 关开关时所有块都是 None -> 保持 None（与改造前一致）。
        # 块内偏移的平移由 merge_backfill_reports 按各块 chunk_start 统一完成。
        # document_text 用于整篇口径的别名幻觉判定：某块漏抽实体时，仍按
        # 整篇正文确认别名是否真实存在（r4 S1）。坐标系必须用 source_text：
        # 返工轮 text 可能追加了审校反馈，反馈里的别名不是正文（r5 F3）。
        document_text = document.source_text or document.text
        backfill = (merge_backfill_reports(
            backfill_reports, document_text=document_text).to_dict()
            if backfill_reports else None)
        return ExtractionOutput(
            document_id=document.id,
            domain_code=self.extractor.domain.code,
            result=merged,
            model_name=self.extractor.model_name,
            usage=usage,
            # 实际模型尝试次数：命中块计 0，真实抽取块按提取器上报的次数累加。
            # 全命中时是 0 而不是块数（N4）。五元组里尝试次数挂在分块度量上。
            attempts=sum(
                item[3].attempts for item in settled if item[3] is not None),
            document_metrics=metrics,
            backfill=backfill,
        )
