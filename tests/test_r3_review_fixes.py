"""工单 03 第 3 轮评审（r3）14 条发现的回归用例。

每个用例对应 r3 报告的一条 finding（B1/B2/S3/S4/S5/N8..N14）；
只测公开接口（backfill_provenance / merge_backfill_reports / Extractor /
provider / rule_review / Chunker / naive_merge），不碰私有实现细节。
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from moray.extraction import Chunker, Document, ExtractionOutput, Extractor
from moray.extraction.document import Usage
from moray.extraction.local_backfill import (
    BackfillReport,
    backfill_provenance,
    merge_backfill_reports,
)
from moray.extraction.pipeline import ChunkedExtractionPipeline, naive_merge
from moray.orchestration.specialists import ReviewVerdict, rule_review
from moray.schema import (
    GENERAL_DOMAIN,
    Entity,
    ExtractionResult,
    Relation,
    build_extraction_model,
    to_core_result,
)

_GOLDEN = Path(__file__).parent / "golden" / "r3_switch_off_262319b.json"
_LOGICAL = {
    "entities": [
        {"name": "阿里巴巴", "type": "ORG", "aliases": ["阿里"]},
        {"name": "马云", "type": "PER"},
        {"name": "杭州", "type": "LOC"},
    ],
    "relations": [
        {"head": "马云", "predicate": "创立", "tail": "阿里巴巴"},
        {"head": "阿里巴巴", "predicate": "总部位于", "tail": "杭州"},
    ],
}


class _StubExtractor(Extractor):
    """固定模型桩：返回预设结构化结果，不联网。"""

    def __init__(self, replies, *, local_backfill=False):
        super().__init__("general", max_retries=0, local_backfill=local_backfill)
        self._replies = list(replies)
        self._calls = 0
        self.seen_documents = []

    def _build_agent(self):
        return SimpleNamespace()

    async def extract(self, document):
        self.seen_documents.append(document)
        return await super().extract(document)

    async def _call_structured(self, agent, content):
        reply = self._replies[self._calls]
        self._calls += 1
        return SimpleNamespace(
            structured_output=reply,
            usage=SimpleNamespace(input_tokens=11, output_tokens=7),
        )


# ---------------------------------------------------------------------------
# B1：回填不得按别名逐个扫描全文（验收 10 的单趟线性承诺）
# ---------------------------------------------------------------------------
class _CountingContainsStr(str):
    """str 子类：统计 ``literal in text`` 触发的全文包含检查次数。"""

    def __new__(cls, value):
        obj = super().__new__(cls, value)
        obj.contains_calls = 0
        return obj

    def __contains__(self, item):
        self.contains_calls += 1
        return super().__contains__(item)


def test_backfill_does_not_scan_full_text_per_alias():
    aliases = [f"别名{i}" for i in range(40)] + ["别名3"]
    text = _CountingContainsStr("华为在深圳发布，别名3出现过。")
    result, report = backfill_provenance(
        ExtractionResult(entities=[
            Entity(name="华为", type="ORG", aliases=aliases),
        ]),
        text,
    )

    # 别名数量不增加任何一次全文包含扫描（旧实现是 40 次）
    assert text.contains_calls == 0
    # 出现的别名不能误报成幻觉；其余 39 个才算未命中
    assert "别名3" in report.seen_literals
    literals = {u["literal"] for u in report.unmatched_aliases}
    assert "别名3" not in literals
    assert len(literals) == 39
    assert [m.surface for m in result.entities[0].mentions] == ["华为", "别名3"]


# ---------------------------------------------------------------------------
# B2：返工反馈拼进正文后，回填仍必须相对原始正文
# ---------------------------------------------------------------------------
_TEXT = "阿里巴巴由马云在杭州创立。"


async def test_extractor_backfills_against_source_text_not_feedback_body():
    fake = _StubExtractor([_LOGICAL], local_backfill=True)
    body = _TEXT + "\n\n【上次问题】\n- 阿里 在正文中不存在；杭州-[总部位于]->阿里巴巴 无证据"
    out = await fake.extract(Document(id="d", text=body, source_text=_TEXT))

    for e in out.result.entities:
        for m in e.mentions:
            assert m.span is not None
            assert 0 <= m.span.start < m.span.end <= len(_TEXT)
            assert _TEXT[m.span.start:m.span.end] == m.surface
    for r in out.result.relations:
        if r.evidence_span is not None:
            assert 0 <= r.evidence_span.start < r.evidence_span.end <= len(_TEXT)
            assert _TEXT[r.evidence_span.start:r.evidence_span.end] == r.evidence_span.text


async def test_provider_keeps_original_text_for_backfill_after_feedback(monkeypatch):
    from moray.extraction import provider as provider_mod
    from moray.extraction.provider import DynamicExtractorProvider

    fake = _StubExtractor([_LOGICAL], local_backfill=True)

    class _Factory:
        def __new__(cls, *args, **kwargs):
            return fake

    class _Resolver:
        async def resolve_with_version(self, code):
            return GENERAL_DOMAIN, 1

    monkeypatch.setattr(provider_mod, "Extractor", _Factory)
    provider = DynamicExtractorProvider(_Resolver(), max_retries=0)
    fn = await provider.extract("general")
    feedback = "请修正：关系 阿里巴巴-[总部位于]->杭州 无原文证据；别名 阿里 疑似幻觉"
    result = await fn(_TEXT, feedback)

    assert fake.seen_documents[-1].text != _TEXT  # 模型仍看到反馈
    for e in result.entities:
        for m in e.mentions:
            assert m.span is not None
            assert 0 <= m.span.start < m.span.end <= len(_TEXT)
            assert _TEXT[m.span.start:m.span.end] == m.surface
    for r in result.relations:
        if r.evidence_span is not None:
            assert 0 <= r.evidence_span.start < r.evidence_span.end <= len(_TEXT)


# ---------------------------------------------------------------------------
# S3：规则④不能把「端点都在正文、只是跨句/跨块」的合法关系判成臆测
# ---------------------------------------------------------------------------
def test_rule_review_allows_cross_sentence_relation_without_evidence_in_slim():
    text = "华为在深圳。任正非创立了华为。"
    logical = ExtractionResult(
        entities=[
            Entity(name="华为", type="ORG"),
            Entity(name="深圳", type="LOC"),
            Entity(name="任正非", type="PER"),
        ],
        relations=[Relation(head="任正非", predicate="关联", tail="深圳")],
    )
    result, report = backfill_provenance(logical, text)
    assert result.relations[0].evidence_span is None  # 跨句：证据合法为空
    review = rule_review(text, result, backfill=report.to_dict())
    assert review.verdict == ReviewVerdict.PASS, review.issues


def test_rule_review_still_flags_ungrounded_endpoint_without_evidence_in_slim():
    text = "华为在深圳。"
    logical = ExtractionResult(
        entities=[
            Entity(name="华为", type="ORG"),
            Entity(name="深圳", type="LOC"),
            Entity(name="幽灵公司", type="ORG"),
        ],
        relations=[Relation(head="幽灵公司", predicate="关联", tail="深圳")],
    )
    result, report = backfill_provenance(logical, text)
    review = rule_review(text, result, backfill=report.to_dict())
    assert review.verdict == ReviewVerdict.NEEDS_FIX
    assert any("无原文证据" in issue for issue in review.issues)


# ---------------------------------------------------------------------------
# S4：跨块后「正文」是整篇文档，块内未见的别名不能被判幻觉
# ---------------------------------------------------------------------------
def test_cross_chunk_alias_seen_in_another_chunk_is_not_hallucination():
    logical = ExtractionResult(
        entities=[Entity(name="腾讯", type="ORG", aliases=["鹅厂"])]
    )
    _, r0 = backfill_provenance(logical, "腾讯发布了财报。")
    _, r1 = backfill_provenance(logical, "鹅厂今天开源了模型。")
    assert [(u["entity"], u["literal"]) for u in r0.unmatched_aliases] == [
        ("腾讯", "鹅厂")]
    assert r1.unmatched_aliases == []

    merged = merge_backfill_reports([
        BackfillReport.from_dict(r0.to_dict(), chunk_start=0, chunk_id="d#c0"),
        BackfillReport.from_dict(r1.to_dict(), chunk_start=8, chunk_id="d#c1"),
    ])
    assert merged.unmatched_aliases == []
    assert "鹅厂" in merged.seen_literals


def test_unmatched_aliases_are_deduped_after_merge():
    logical = ExtractionResult(
        entities=[Entity(name="腾讯", type="ORG", aliases=["鹅厂"])]
    )
    _, r0 = backfill_provenance(logical, "腾讯发布了财报。")
    _, r1 = backfill_provenance(logical, "腾讯再次发布。")
    merged = merge_backfill_reports([
        BackfillReport.from_dict(r0.to_dict(), chunk_start=0, chunk_id="d#c0"),
        BackfillReport.from_dict(r1.to_dict(), chunk_start=8, chunk_id="d#c1"),
    ])
    # 两块都未见过该别名：合并后只保留一条，不按块堆叠
    assert merged.unmatched_aliases == [
        {"type": "ORG", "entity": "腾讯", "literal": "鹅厂"}]


# ---------------------------------------------------------------------------
# S5：开关关闭时分块边界必须与 262319b 逐字段一致
# ---------------------------------------------------------------------------
def test_chunking_matches_262319b_golden_including_leading_boundary():
    golden = json.loads(_GOLDEN.read_text(encoding="utf-8"))
    for case in golden["chunking_cases"]:
        chunks = Chunker(
            max_units=case["max_units"],
            overlap_units=case["overlap_units"],
        ).chunk_text(case["text"])
        actual = [
            {"id": c.id, "text": c.text, "start": c.start,
             "end": c.end, "index": c.index}
            for c in chunks
        ]
        assert actual == case["chunks"], case["text"]

    # 旧实现的边界：以结束符/换行开头时返回空列表（不是逐字切出第一句）
    assert Chunker().chunk_text("\n华为在深圳。") == []
    assert Chunker().chunk_text("。华为在深圳。") == []


# ---------------------------------------------------------------------------
# N8：开关关闭的完整 ExtractionOutput 与 262319b golden 逐字段一致
# ---------------------------------------------------------------------------
async def test_switch_off_extraction_output_matches_262319b_golden():
    golden = json.loads(_GOLDEN.read_text(encoding="utf-8"))
    fake = _StubExtractor([golden["full_payload"]], local_backfill=False)
    out = await fake.extract(
        Document(id="d", text=golden["full_text"]))
    actual = out.model_dump()
    expected = golden["extraction_output"]

    # 核心结果逐字段一致（含 mentions/confidence/evidence/description）
    assert actual["result"] == expected["result"]
    for key in ("document_id", "domain_code", "model_name", "attempts"):
        assert actual[key] == expected[key]
    for key in ("input_tokens", "output_tokens", "cached_input_tokens"):
        assert actual["usage"][key] == expected["usage"][key]

    # 本工单新增的旁路字段在关闭态必须为 None；01 的 usage.cache_hits /
    # call_metrics 是后加工单新增字段，不属于 262319b 的 03 语义对比面。
    assert actual["backfill"] is None
    assert actual["usage"]["cache_hits"] == 0
    assert actual["call_metrics"] is not None


class _ChunkFullFake:
    """262319b 风格的「全量 schema」假抽取器：每块自报 spans/confidence。"""

    domain = SimpleNamespace(code="general")
    model_name = "fake-262319b"

    async def extract(self, document):
        from moray.schema import EntityMention, Span

        t = document.text
        ents, rels = [], []
        if "阿里巴巴" in t:
            i = t.index("阿里巴巴")
            ents.append(Entity(
                name="阿里巴巴", type="ORG", aliases=[],
                mentions=[EntityMention(surface="阿里巴巴",
                                        span=Span(start=i, end=i + 4,
                                                  text="阿里巴巴"))],
                confidence=0.9, attributes={}, description="电商公司"))
        if "马云" in t:
            i = t.index("马云")
            ents.append(Entity(
                name="马云", type="PER", aliases=[],
                mentions=[EntityMention(surface="马云",
                                        span=Span(start=i, end=i + 2,
                                                  text="马云"))],
                confidence=0.7, attributes={}, description=""))
            if "阿里巴巴" in t:
                rels.append(Relation(head="马云", predicate="创立", tail="阿里巴巴",
                                     evidence_span=None, confidence=0.8))
        if "腾讯" in t:
            i = t.index("腾讯")
            ents.append(Entity(
                name="腾讯", type="ORG", aliases=["鹅厂"],
                mentions=[EntityMention(surface="腾讯",
                                        span=Span(start=i, end=i + 2,
                                                  text="腾讯"))],
                confidence=0.5, attributes={}, description="社交公司"))
        if "深圳" in t:
            i = t.index("深圳")
            ents.append(Entity(
                name="深圳", type="LOC", aliases=[],
                mentions=[EntityMention(surface="深圳",
                                        span=Span(start=i, end=i + 2,
                                                  text="深圳"))],
                confidence=0.6, attributes={}, description=""))
            if "腾讯" in t:
                rels.append(Relation(head="腾讯", predicate="总部位于", tail="深圳",
                                     evidence_span=None, confidence=0.7))
        return ExtractionOutput(
            document_id=document.id, domain_code="general",
            result=ExtractionResult(entities=ents, relations=rels),
            model_name=self.model_name,
            usage=Usage(input_tokens=3, output_tokens=2),
            attempts=1,
        )


async def test_switch_off_chunked_output_matches_262319b_golden():
    from moray.extraction import Chunk

    golden = json.loads(_GOLDEN.read_text(encoding="utf-8"))
    text = golden["chunked_text"]
    s1 = text.index("。") + 1
    chunks = [
        Chunk(id="d#c0", text=text[:s1], start=0, end=s1, index=0),
        Chunk(id="d#c1", text=text[s1:], start=s1, end=len(text), index=1),
    ]
    out = await ChunkedExtractionPipeline(
        _ChunkFullFake(), concurrency=1).run(Document(id="d", text=text), chunks)
    actual = out.model_dump()
    expected = golden["chunked_output"]
    assert actual["result"] == expected["result"]
    assert actual["attempts"] == expected["attempts"]
    assert actual["model_name"] == expected["model_name"]
    for key in ("input_tokens", "output_tokens", "cached_input_tokens"):
        assert actual["usage"][key] == expected["usage"][key]
    assert actual["backfill"] is None


# ---------------------------------------------------------------------------
# N9：没有可核对 span 时，span_ratio 是「未测」而不是空转的 1.0
# ---------------------------------------------------------------------------
def test_rule_review_span_ratio_is_unknown_when_nothing_checked():
    review = rule_review(
        "A。", ExtractionResult(entities=[Entity(name="A", type="ORG")]))
    assert review.verdict == ReviewVerdict.PASS
    assert review.span_ratio is None


@pytest.mark.asyncio
async def test_dashboard_summary_span_ratio_unknown_stays_none():
    """零 span 运行落 NULL；全批次未测时聚合也返回 None，不伪装成 0/1。"""
    from moray.observability.dashboard import Dashboard

    captured = {}

    class _Pool:
        async def fetchrow(self, query, *params):
            captured["query"] = query
            return {
                "runs": 1, "passed": 1, "avg_span_ratio": None,
                "avg_block_confidence": None, "entities_grounded": 0,
                "entities_total": 0, "avg_relation_evidence_ratio": None,
                "ambiguous_mentions": 0, "avg_review_iters": 1.0,
                "total_open_issues": 0, "total_entities": 1,
                "total_relations": 0, "auto_merged": 0, "queued": 0,
                "created": 0, "input_tokens": 0, "output_tokens": 0,
                "cached_input_tokens": 0, "cache_hits": 0,
                "avg_latency_ms": 0.0, "chunk_calls": 0,
                "total_attempts": 0, "truncated_chunks": 0,
                "truncation_signals": 0, "reworked_chunks": 0, "wait_ms": 0,
                "chunk_latency_ms": 0, "prefill_ms": 0, "decode_ms": 0,
                "chunk_latency_measured_runs": 0,
                "prefill_measured_runs": 0, "decode_measured_runs": 0,
            }

    s = await Dashboard(_Pool()).summary()
    assert "coalesce(avg(span_ratio),0)" not in captured["query"]
    assert s["avg_span_ratio"] is None


# ---------------------------------------------------------------------------
# N10：naive_merge 默认值必须保持 262319b 旧语义
# ---------------------------------------------------------------------------
def _merge_inputs():
    from moray.schema import Span

    a = ExtractionResult(
        entities=[
            Entity(name="甲", type="ORG", aliases=["甲甲"], confidence=0.4,
                   attributes={"k": "a"}),
            Entity(name="乙", type="PER", confidence=0.6),
        ],
        relations=[Relation(head="甲", predicate="投", tail="乙", confidence=0.4)],
    )
    b = ExtractionResult(
        entities=[
            Entity(name="甲", type="ORG", aliases=["甲总"], confidence=0.9,
                   attributes={"k": "b", "x": 1}, description="甲集团简介"),
        ],
        relations=[Relation(
            head="甲", predicate="投", tail="乙",
            evidence_span=Span(start=0, end=4, text="甲投乙。"), confidence=0.9)],
    )
    return a, b


def test_naive_merge_default_keeps_pre_change_semantics():
    # 这份 golden 的 naive_merge 快照在 test_pipeline_parts 里
    legacy = json.loads(
        (Path(__file__).parent / "golden"
         / "switch_off_naive_merge_262319b.json").read_text(encoding="utf-8"))
    a, b = _merge_inputs()
    assert naive_merge([a, b]).model_dump() == legacy["legacy_merged"]
    # 显式打开才合并后见证据/置信度
    improved = naive_merge([a, b], merge_relation_duplicates=True)
    assert improved.relations[0].evidence_span is not None
    assert improved.relations[0].confidence == 0.9


# ---------------------------------------------------------------------------
# N11：内核 confidence 默认 None 是刻意变更；full 模式契约仍为 0.0
# ---------------------------------------------------------------------------
def test_core_confidence_default_is_none_and_full_contract_stays_zero():
    assert Entity(name="X", type="ORG").confidence is None
    assert Relation(head="X", predicate="r", tail="Y").confidence is None

    full = build_extraction_model(GENERAL_DOMAIN, slim=False)
    dyn = full.model_validate({
        "entities": [{"name": "华为", "type": "ORG"}],
        "relations": [],
    })
    assert dyn.entities[0].confidence == 0.0  # full 模型契约未变
    assert to_core_result(dyn).entities[0].confidence == 0.0


# ---------------------------------------------------------------------------
# N12：关系端点可以用唯一归属的别名解析；歧义别名不猜
# ---------------------------------------------------------------------------
def test_relation_endpoint_resolves_unique_alias_literal():
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
    ev = result.relations[0].evidence_span
    assert ev is not None
    assert text[ev.start:ev.end] == ev.text == text
    assert report.relations_with_evidence == 1


def test_relation_endpoint_ambiguous_alias_is_not_guessed():
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
    assert result.relations[0].evidence_span is None
    assert len(report.relations_without_evidence) == 1


# ---------------------------------------------------------------------------
# N14：重叠块里同一处歧义不能被重复计数
# ---------------------------------------------------------------------------
def test_overlapping_chunks_dedupe_ambiguous_mentions():
    full = "前缀。苹果公司发布。"
    c0_text, c1_text = full[:7], full[3:]
    logical = ExtractionResult(entities=[
        Entity(name="苹果", type="ORG"),
        Entity(name="苹果", type="MISC"),
    ])
    _, r0 = backfill_provenance(logical, c0_text)
    _, r1 = backfill_provenance(logical, c1_text)
    merged = merge_backfill_reports([
        BackfillReport.from_dict(r0.to_dict(), chunk_start=0, chunk_id="d#c0"),
        BackfillReport.from_dict(r1.to_dict(), chunk_start=3, chunk_id="d#c1"),
    ])
    assert len(merged.ambiguous_mentions) == 1
    am = merged.ambiguous_mentions[0]
    assert (am.surface, am.start, am.end) == ("苹果", 3, 5)
    assert am.chunk_ids == ["d#c0", "d#c1"]
