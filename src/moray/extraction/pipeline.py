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
)
from .chunking import Chunk
from .document import Document, ExtractionOutput, Usage
from .extractor import Extractor
from .local_backfill import (
    BackfillReport,
    merge_backfill_reports,
    merge_entity_metadata,
)


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
            # 与 local_backfill 共用同一套元数据合并规则（别名并集/属性首见/
            # 置信度取大），避免两处逻辑漂移；mention 在归并层必须全量保留。
            merge_entity_metadata(ent, e)
            ent.mentions.extend(e.mentions)

    rel_index: dict[tuple[str, str, str], Relation] = {}
    relations: list[Relation] = []
    for part in partials:
        for r in part.relations:
            key = (r.head, r.predicate, r.tail)
            existing = rel_index.get(key)
            if existing is not None:
                # 同一三元组在别的块可能才拿到证据/更高置信度：首见不能赢家通吃。
                if existing.evidence_span is None and r.evidence_span is not None:
                    existing.evidence_span = r.evidence_span
                existing.confidence = max(existing.confidence, r.confidence)
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
        self, chunk: Chunk
    ) -> tuple[ExtractionResult, Usage, str, dict | None]:
        """抽单个块（带限流）。返回换算后的结果、用量、块 id、回填信号。"""
        async with self.sem:
            # 复用 Extractor：把一个 chunk 当成一篇微型文档
            mini_doc = Document(
                id=chunk.id,
                text=chunk.text,
                source="chunk",
                domain_code=self.extractor.domain.code,
            )
            output = await self.extractor.extract(mini_doc)
            return (remap_spans(output.result, chunk), output.usage, chunk.id,
                    output.backfill)

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

        async def worker(chunk: Chunk) -> tuple[ExtractionResult, Usage, str, dict | None]:
            res, u, cid, backfill = await self._extract_one_chunk(chunk)
            if on_chunk:
                on_chunk(res, cid, total)
            return res, u, cid, backfill

        settled = await asyncio.gather(*(worker(c) for c in chunks))

        partials = [item[0] for item in settled]
        for _, u, _, _ in settled:
            usage.add(u)

        merged = naive_merge(partials)
        # 分块回填信号必须汇总后继续往上层传，不能在 pipeline 这里丢掉。
        # 关开关时所有块都是 None -> 保持 None（与改造前一致）。
        reports = []
        for _, _, _, raw in settled:
            if raw is not None:
                reports.append(BackfillReport.from_dict(raw))
        backfill = merge_backfill_reports(reports).to_dict() if reports else None
        return ExtractionOutput(
            document_id=document.id,
            domain_code=self.extractor.domain.code,
            result=merged,
            model_name=self.extractor.model_name,
            usage=usage,
            attempts=total,
            backfill=backfill,
        )
