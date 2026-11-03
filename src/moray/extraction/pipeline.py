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
    Entity,
    EntityMention,
    ExtractionResult,
    Relation,
    Span,
)
from .chunk_metrics import FINISH_UNKNOWN, ChunkCallMetrics, DocumentMetrics
from .chunking import Chunk
from .document import Document, ExtractionOutput, Usage
from .extractor import ExtractionError, Extractor

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


def naive_merge(partials: list[ExtractionResult]) -> ExtractionResult:
    """朴素归并（L09 会升级为向量+LLM 共指消解）。

    规则：实体按 (type, 标准名) 聚合；别名并集；mention 全部保留。
    关系按三元组字符串去重，置信度取最大值。
    overlap 区域产生的重复 mention 会自然落在同一实体下，不会重复造实体。
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
            for a in e.aliases:
                if a not in ent.aliases and a != e.name:
                    ent.aliases.append(a)
            ent.mentions.extend(e.mentions)
            ent.confidence = max(ent.confidence, e.confidence)
            # 动态属性做一次浅合并（后者补前者缺的键）
            for k, v in e.attributes.items():
                ent.attributes.setdefault(k, v)

    rel_seen: set[tuple[str, str, str]] = set()
    relations: list[Relation] = []
    for part in partials:
        for r in part.relations:
            key = (r.head, r.predicate, r.tail)
            if key in rel_seen:
                continue
            rel_seen.add(key)
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

    async def _extract_one_chunk(
        self, chunk: Chunk, metrics: DocumentMetrics,
    ) -> tuple[ExtractionResult | None, Usage, str, ChunkCallMetrics | None]:
        """抽单个块（带限流）。

        返回 (换算后的结果|None, 用量, 块 id, 分块度量)。结果为 None 表示
        该块多次尝试后仍失败（度量里带 error/截断信号），由调用方决定是否容忍。
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
            try:
                output = await self.extractor.extract(mini_doc)
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
                return None, failed_usage, chunk.id, record
            m = output.call_metrics
            if m is not None:
                m.chunk_id = chunk.id
                m.wait_s = wait_s
            else:
                # 不经标准度量路径的兜底（正常 Extractor 不会走到这）
                m = ChunkCallMetrics(
                    chunk_id=chunk.id, attempts=output.attempts,
                    finish_reason="unknown",
                    input_tokens=output.usage.input_tokens,
                    output_tokens=output.usage.output_tokens,
                    cached_input_tokens=output.usage.cached_input_tokens,
                    wait_s=wait_s, prefill_s=None, decode_s=None)
            metrics.add(m)
            return remap_spans(output.result, chunk), output.usage, chunk.id, m

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
        if self.warm_prefix and total >= 2:
            warm_usage = await self.extractor.warm_prefix()
            usage.add(warm_usage)
            metrics.warmup = warm_usage.model_dump()

        async def worker(chunk: Chunk):
            result = await self._extract_one_chunk(chunk, metrics)
            res, _usage, cid, _record = result
            # 恢复"每块完成即回调"的进度语义（改动工单前就是这样）：
            # 失败块仍不回调，但成功块不再等到全部 gather 完才通知。
            if res is not None and on_chunk:
                on_chunk(res, cid, total)
            return result

        settled = await asyncio.gather(*(worker(c) for c in chunks))

        partials: list[ExtractionResult] = []
        failed: list[str] = []
        for res, u, cid, _m in settled:
            usage.add(u)
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

        merged = naive_merge(partials)
        return ExtractionOutput(
            document_id=document.id,
            domain_code=self.extractor.domain.code,
            result=merged,
            model_name=self.extractor.model_name,
            usage=usage,
            attempts=total,
            document_metrics=metrics,
        )
