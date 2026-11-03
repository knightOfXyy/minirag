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
import hashlib

from ..schema import (
    DomainSpec,
    Entity,
    EntityMention,
    ExtractionResult,
    Relation,
    Span,
    domain_fingerprint,
)
from .chunking import Chunk
from .document import CacheSavings, Document, ExtractionOutput, Usage
from .extractor import Extractor


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

    def _domain_fingerprint(self) -> str:
        """当前抽取器所用领域配置 + 提示模板的版本指纹。

        真 Extractor 持有完整 DomainSpec，走内容级指纹（system prompt + schema）。
        对不是 DomainSpec 的假对象，回退值必须包含该对象的完整 repr：只退化成
        code 会让“同 code、不同枚举”的配置共用缓存键（N3）。

        user 消息模板（``Extractor._LEAD_IN`` / ``_retry_prompt``）同样影响输出，
        且改动不会体现在 DomainSpec 里：把它们的 hash 并进指纹，改模板自动失效，
        不再依赖人手递增 ``_PROMPT_TEMPLATE_VERSION``（nit-2）。
        """
        domain = getattr(self.extractor, "domain", None)
        if isinstance(domain, DomainSpec):
            base = domain_fingerprint(domain)
        else:
            base = f"code:{getattr(domain, 'code', 'unknown')}|repr:{domain!r}"
        lead_in = getattr(self.extractor, "_LEAD_IN", "")
        retry_prompt = getattr(self.extractor, "_retry_prompt", None)
        retry_sample = retry_prompt("{error}") if callable(retry_prompt) else ""
        template = hashlib.sha256(
            f"{lead_in}\x00{retry_sample}".encode("utf-8")).hexdigest()[:16]
        return f"{base}|tpl:{template}"

    def _cache_model_identity(self) -> str:
        """缓存键里的模型身份：优先用 Extractor 的完整身份（provider+base_url+
        model），假对象退化为裸 model_name（nit-1）。"""
        return (getattr(self.extractor, "model_identity", None)
                or getattr(self.extractor, "model_name", "") or "")

    async def _extract_one_chunk(self, chunk: Chunk) -> tuple[ExtractionResult, Usage, str, int]:
        """抽单个块（带限流）。返回换算后的结果、用量、块 id、真实模型尝试次数。

        有缓存时以 (领域指纹, 模型名, 块正文) 为键取【块内】产物：命中则不发起
        模型调用，用量只记一次 cache_hits；偏移换算仍在缓存之外做，语义不变。
        """
        async with self.sem:
            # 复用 Extractor：把一个 chunk 当成一篇微型文档
            mini_doc = Document(
                id=chunk.id,
                text=chunk.text,
                source="chunk",
                domain_code=self.extractor.domain.code,
            )

            if self.result_cache is None:
                output = await self.extractor.extract(mini_doc)
            else:
                model_name = self._cache_model_identity()

                async def _do_extract() -> ExtractionOutput:
                    return await self.extractor.extract(mini_doc)

                output, hit = await self.result_cache.get_or_extract(
                    self._cached_domain_fingerprint, model_name,
                    chunk.text, _do_extract)
                if hit:
                    # 命中：还原结构化结果与【用量记录】；但不计真实 token，
                    # 把缓存里的 input/output 记到独立的 cache_savings（S4/N2），
                    # 成本/看板只汇总真实计量字段。命中块没有模型尝试（N4）。
                    usage = Usage(
                        cache_hits=1,
                        cache_savings=CacheSavings(
                            input_tokens=output.usage.input_tokens,
                            output_tokens=output.usage.output_tokens,
                        ),
                    )
                    return remap_spans(output.result, chunk), usage, chunk.id, 0

            return (remap_spans(output.result, chunk), output.usage,
                    chunk.id, output.attempts)

    async def run(
        self,
        document: Document,
        chunks: list[Chunk],
        *,
        on_chunk=None,
    ) -> ExtractionOutput:
        """并发抽取所有块，换算偏移后归并。on_chunk(done,total,chunk_id) 可做进度回调。"""
        total = len(chunks)
        usage = Usage()

        # 只有多块才值得预热（单块文档预热等于白烧一次前缀）。
        # 探测请求本身的开销记在触发它这篇文档上：宁可多记，不要漏记成本。
        # 但若所有块都已命中缓存，预热会凭空造出一次真实模型调用，与“第二次
        # 零调用”冲突（N3）：先只读探测一遍，全命中就跳过预热。
        if self.warm_prefix and total >= 2:
            all_cached = False
            if self.result_cache is not None:
                model_name = self._cache_model_identity()
                # 并发探测：postgres 后端下串行 N 次 RTT 只为决定要不要预热
                # 是纯浪费（nit-4）。
                probes = await asyncio.gather(*[
                    self.result_cache.has_cached(
                        self._cached_domain_fingerprint, model_name, c.text)
                    for c in chunks
                ])
                all_cached = all(probes)
            if not all_cached:
                usage.add(await self.extractor.warm_prefix())

        async def worker(chunk: Chunk) -> tuple[ExtractionResult, Usage, str, int]:
            res, u, cid, attempts = await self._extract_one_chunk(chunk)
            if on_chunk:
                on_chunk(res, cid, total)
            return res, u, cid, attempts

        settled = await asyncio.gather(*(worker(c) for c in chunks))

        partials = [item[0] for item in settled]
        for _, u, _, _ in settled:
            usage.add(u)

        merged = naive_merge(partials)
        # attempts 口径（should-2，写死避免再漂移）：
        #   · result_cache is None：与历史实现完全一致 = 块数（默认配置不改语义）；
        #   · 开缓存：真实模型尝试次数累计，命中块计 0（全命中为 0）。
        attempts = (total if self.result_cache is None
                    else sum(item[3] for item in settled))
        return ExtractionOutput(
            document_id=document.id,
            domain_code=self.extractor.domain.code,
            result=merged,
            model_name=self.extractor.model_name,
            usage=usage,
            attempts=attempts,
        )
