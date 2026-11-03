"""工单 03 第 4 轮评审（r4）发现的回归用例。

覆盖 r4 的 blocker / should / nit 中可在离线单测里钉住的行为：

* B1：分块结果缓存键必须区分 slim/full 回填模式，共享缓存不得互相投毒；
* S1：跨块未命中别名以整篇文档为口径，实体在某块漏抽时不能误报幻觉；
* N1：规则审校的关系端点与回填同口径（唯一归属的别名才认）；
* N4：重叠块计数按 chunk-weighted 语义记录（文档级去重在信号消费侧说明）；
* N6：实体 description 补全与关系重复合并是两个独立开关。
"""

import json
from pathlib import Path
from types import SimpleNamespace

from moray.extraction import Chunk, Chunker, Document, ExtractionOutput, Extractor
from moray.extraction.cache import ContentResultCache, InMemoryCacheBackend
from moray.extraction.document import Usage
from moray.extraction.local_backfill import (
    BackfillReport,
    backfill_provenance,
    entity_signal_key,
    merge_backfill_reports,
)
from moray.extraction.pipeline import ChunkedExtractionPipeline, naive_merge
from moray.orchestration.specialists import ReviewVerdict, rule_review
from moray.schema import (
    GENERAL_DOMAIN,
    Entity,
    ExtractionResult,
    Relation,
    Span,
    domain_fingerprint,
)


# ---------------------------------------------------------------------------
# B1：缓存键不含回填模式 -> 关闭态会拿到 slim 的实体与 backfill
# ---------------------------------------------------------------------------
_SLIM_REPLY = {
    "entities": [
        {"name": "腾讯", "type": "ORG", "aliases": ["鹅厂"]},
    ],
    "relations": [],
}
_FULL_REPLY = {
    "entities": [
        {
            "name": "腾讯",
            "type": "ORG",
            "aliases": ["鹅厂"],
            "mentions": [
                {"surface": "腾讯", "span": {"start": 0, "end": 2, "text": "腾讯"}}
            ],
            "confidence": 0.9,
            "attributes": {},
        },
    ],
    "relations": [],
}


class _ModeStub(Extractor):
    """同一段正文、两种 schema 的固定模型桩（不联网）。"""

    def __init__(self, *, local_backfill: bool):
        super().__init__("general", max_retries=0, local_backfill=local_backfill)
        self._reply = _SLIM_REPLY if local_backfill else _FULL_REPLY

    def _build_agent(self):
        return SimpleNamespace()

    async def _call_structured(self, agent, content):
        self._calls = getattr(self, "_calls", 0) + 1
        return SimpleNamespace(
            structured_output=self._reply,
            usage=SimpleNamespace(input_tokens=11, output_tokens=7),
        )


def test_domain_fingerprint_changes_with_slim_mode():
    assert domain_fingerprint(GENERAL_DOMAIN) != domain_fingerprint(
        GENERAL_DOMAIN, slim=True)


async def _run_one(stub: Extractor, cache, text: str):
    chunks = [Chunk(id="d#c0", text=text, start=0, end=len(text), index=0)]
    return await ChunkedExtractionPipeline(
        stub, concurrency=1, result_cache=cache,
    ).run(Document(id="d", text=text), chunks)


async def test_shared_cache_slim_then_full_does_not_poison_switch_off():
    text = "腾讯发布财报。"
    cache = ContentResultCache(InMemoryCacheBackend())
    slim = _ModeStub(local_backfill=True)
    full = _ModeStub(local_backfill=False)

    slim_out = await _run_one(slim, cache, text)
    assert slim_out.backfill is not None

    full_out = await _run_one(full, cache, text)
    # full 必须自己抽（不能命中 slim 的缓存键），且关闭态没有 backfill
    assert full._calls == 1
    assert full_out.backfill is None
    assert full_out.result.entities[0].confidence == 0.9
    assert full_out.result.entities[0].mentions[0].span.start == 0


async def test_shared_cache_full_then_slim_does_not_lose_backfill():
    text = "腾讯发布财报。"
    cache = ContentResultCache(InMemoryCacheBackend())
    full = _ModeStub(local_backfill=False)
    slim = _ModeStub(local_backfill=True)

    full_out = await _run_one(full, cache, text)
    assert full_out.backfill is None

    slim_out = await _run_one(slim, cache, text)
    # slim 必须自己抽（不能命中 full 的缓存键），并拿到回填 mention/信号
    assert slim._calls == 1
    assert slim_out.backfill is not None
    assert [m.surface for m in slim_out.result.entities[0].mentions] == ["腾讯"]


# ---------------------------------------------------------------------------
# S1：chunk1 漏抽实体但正文含别名 -> 不能把 chunk0 的别名误报成幻觉
# ---------------------------------------------------------------------------
class _ChunkMissingEntityFake:
    """chunk0 输出腾讯（别名鹅厂），chunk1 漏抽腾讯但正文有「鹅厂」。"""

    domain = SimpleNamespace(code="general")
    model_name = "fake-r4"
    local_backfill = True

    async def extract(self, document: Document):
        if "腾讯" in document.text:
            logical = ExtractionResult(entities=[
                Entity(name="腾讯", type="ORG", aliases=["鹅厂"])])
        else:
            logical = ExtractionResult(entities=[Entity(name="华为", type="ORG")])
        result, report = backfill_provenance(logical, document.text)
        return ExtractionOutput(
            document_id=document.id, domain_code="general",
            result=result, model_name=self.model_name, usage=Usage(),
            attempts=1, backfill=report.to_dict(),
        )


async def test_missing_entity_in_one_chunk_does_not_report_alias_hallucination():
    text = "腾讯发布财报。鹅厂开源模型。"
    s1 = text.index("。") + 1
    chunks = [
        Chunk(id="d#c0", text=text[:s1], start=0, end=s1, index=0),
        Chunk(id="d#c1", text=text[s1:], start=s1, end=len(text), index=1),
    ]
    out = await ChunkedExtractionPipeline(
        _ChunkMissingEntityFake(), concurrency=1).run(
            Document(id="d", text=text), chunks)

    # 「鹅厂」确实在整篇正文里，不能因为 chunk1 漏抽腾讯就判幻觉
    assert out.backfill["unmatched_aliases"] == []
    assert "鹅厂" in out.backfill["seen_literals"]


def test_merge_backfill_reports_scans_document_text_for_unmatched_aliases():
    logical = ExtractionResult(entities=[
        Entity(name="腾讯", type="ORG", aliases=["鹅厂"])])
    _, r0 = backfill_provenance(logical, "腾讯发布财报。")
    _, r1 = backfill_provenance(
        ExtractionResult(entities=[Entity(name="华为", type="ORG")]),
        "鹅厂开源模型。",
    )
    merged = merge_backfill_reports(
        [
            BackfillReport.from_dict(r0.to_dict(), chunk_start=0, chunk_id="d#c0"),
            BackfillReport.from_dict(r1.to_dict(), chunk_start=7, chunk_id="d#c1"),
        ],
        document_text="腾讯发布财报。鹅厂开源模型。",
    )
    assert merged.unmatched_aliases == []
    assert "鹅厂" in merged.seen_literals


def test_merge_backfill_reports_keeps_alias_absent_from_whole_document():
    logical = ExtractionResult(entities=[
        Entity(name="腾讯", type="ORG", aliases=["鹅厂"])])
    _, r0 = backfill_provenance(logical, "腾讯发布财报。")
    merged = merge_backfill_reports(
        [BackfillReport.from_dict(r0.to_dict(), chunk_start=0, chunk_id="d#c0")],
        document_text="腾讯发布财报。",
    )
    assert merged.unmatched_aliases == [{"entity": "腾讯", "literal": "鹅厂"}]


# ---------------------------------------------------------------------------
# N1：规则审校的关系端点必须与回填同口径
# ---------------------------------------------------------------------------
def test_rule_review_accepts_unique_alias_relation_endpoint():
    text = "鹅厂发布了混元。"
    result, report = backfill_provenance(
        ExtractionResult(
            entities=[
                Entity(name="腾讯", type="ORG", aliases=["鹅厂"]),
                Entity(name="混元", type="MISC"),
            ],
            relations=[Relation(head="鹅厂", predicate="发布", tail="混元")],
        ),
        text,
    )
    review = rule_review(text, result, backfill=report.to_dict())
    assert review.verdict == ReviewVerdict.PASS, review.issues


def test_rule_review_still_rejects_ambiguous_alias_relation_endpoint():
    text = "同名发布了混元。"
    result, report = backfill_provenance(
        ExtractionResult(
            entities=[
                Entity(name="腾讯", type="ORG", aliases=["同名"]),
                Entity(name="网易", type="ORG", aliases=["同名"]),
                Entity(name="混元", type="MISC"),
            ],
            relations=[Relation(head="同名", predicate="发布", tail="混元")],
        ),
        text,
    )
    review = rule_review(text, result, backfill=report.to_dict())
    assert review.verdict == ReviewVerdict.NEEDS_FIX
    assert any("不在实体列表" in issue for issue in review.issues)


# ---------------------------------------------------------------------------
# N4：重叠块计数是 chunk-weighted（不是文档级去重值），语义要钉住并写清
# ---------------------------------------------------------------------------
def test_overlapping_chunk_counts_are_chunk_weighted():
    text = "腾讯发布财报。"
    logical = ExtractionResult(entities=[Entity(name="腾讯", type="ORG")])
    _, r0 = backfill_provenance(logical, text)
    _, r1 = backfill_provenance(logical, text)
    merged = merge_backfill_reports([
        BackfillReport.from_dict(r0.to_dict(), chunk_start=0, chunk_id="d#c0"),
        BackfillReport.from_dict(r1.to_dict(), chunk_start=2, chunk_id="d#c1"),
    ])
    # 同一段原文被两个重叠块各计一次：这是块加权口径，不是文档级去重值。
    key = entity_signal_key(Entity(name="腾讯", type="ORG"))
    assert merged.entities_total == 2
    assert merged.entities_grounded == 2
    assert merged.matched_literals[key] == 2
    assert merged.block_confidence == 1.0


# ---------------------------------------------------------------------------
# N6：description 补全与关系重复合并解耦
# ---------------------------------------------------------------------------
def _description_merge_inputs():
    a = ExtractionResult(entities=[
        Entity(name="甲", type="ORG", aliases=["甲甲"]),
        Entity(name="乙", type="PER"),
    ])
    b = ExtractionResult(entities=[
        Entity(name="甲", type="ORG", aliases=["甲总"], description="甲集团简介"),
    ])
    return a, b


def test_naive_merge_fill_description_is_independent_of_relation_merge():
    a, b = _description_merge_inputs()
    no_fill = naive_merge(
        [a, b], merge_relation_duplicates=True, fill_description=False)
    assert no_fill.entities[0].description == ""

    fill = naive_merge(
        [a, b], merge_relation_duplicates=True, fill_description=True)
    assert fill.entities[0].description == "甲集团简介"
