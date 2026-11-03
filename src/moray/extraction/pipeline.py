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

from ..schema import (
    Entity,
    EntityMention,
    ExtractionResult,
    Relation,
    Span,
    domain_fingerprint,
)
from .chunking import Chunk
from .document import Document, ExtractionOutput, Usage
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

    def _domain_fingerprint(self) -> str:
        """当前抽取器所用领域配置的版本指纹。

        Extractor 持有完整 DomainSpec；测试注入的假抽取器可能只有 .domain.code，
        此时回退到 code 本身（换 code 仍会失效；改 spec 的场景用真 Extractor）。
        """
        domain = getattr(self.extractor, "domain", None)
        try:
            return domain_fingerprint(domain)
        except Exception:  # noqa: BLE001 假对象/非 DomainSpec：退回可用标识
            return f"code:{getattr(domain, 'code', 'unknown')}"

    async def _extract_one_chunk(self, chunk: Chunk) -> tuple[ExtractionResult, Usage, str]:
        """抽单个块（带限流）。返回换算后的结果、用量、块 id。

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
                fingerprint = self._domain_fingerprint()
                model_name = getattr(self.extractor, "model_name", "") or ""

                async def _do_extract() -> ExtractionOutput:
                    return await self.extractor.extract(mini_doc)

                output, hit = await self.result_cache.get_or_extract(
                    fingerprint, model_name, chunk.text, _do_extract)
                if hit:
                    # 命中：还原结果，但不计真实 token，只标记一次缓存来源。
                    usage = Usage(cache_hits=1)
                    return remap_spans(output.result, chunk), usage, chunk.id

            return remap_spans(output.result, chunk), output.usage, chunk.id

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
        if self.warm_prefix and total >= 2:
            usage.add(await self.extractor.warm_prefix())

        async def worker(chunk: Chunk) -> tuple[ExtractionResult, Usage, str]:
            res, u, cid = await self._extract_one_chunk(chunk)
            if on_chunk:
                on_chunk(res, cid, total)
            return res, u, cid

        settled = await asyncio.gather(*(worker(c) for c in chunks))

        partials = [item[0] for item in settled]
        for _, u, _ in settled:
            usage.add(u)

        merged = naive_merge(partials)
        return ExtractionOutput(
            document_id=document.id,
            domain_code=self.extractor.domain.code,
            result=merged,
            model_name=self.extractor.model_name,
            usage=usage,
            attempts=total,
        )
