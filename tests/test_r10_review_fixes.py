"""工单 03 第 10 轮评审（r10）发现的回归用例。

只钉住本轮要修的、可离线验证的行为：

* S1（should）被更长实体名吸收的字面作为关系端点时，不得误报
  「端点在正文中未命中」而触发模型返工；
* S2（should）重叠块合并后，同一处全局 span 被多个逻辑实体拥有时必须
  撤销归属并留下歧义信号，而不是同时写进两个实体；
* N1（nit）关系证据候选要能逐值与 ``Relation.evidence_span`` 对齐；
* N2（nit）跨块合并后全局只能有一条 ``selected=True``；
* N3（nit）``unmatched_aliases`` 的去重键要带类型，同名异型不合并；
* N4（nit）关系证据候选序列化有上限，内存报告仍保留全部；
* N5（nit）质量中间件零 span 记「未测」，且回填不重复扫描；
* N8（nit）``extraction/__init__`` 无重复导入；
* N9（nit）slim 归并身份键与回填一致（去首尾空白），关闭态保持精确键；
* N10（nit）``naive_merge`` 合并关系时不得改写入参 partial。
"""

from types import SimpleNamespace

import pytest

from moray.extraction.document import Document, ExtractionOutput, Usage
from moray.extraction.local_backfill import (
    BackfillReport,
    backfill_provenance,
    merge_backfill_reports,
)
from moray.extraction.pipeline import naive_merge
from moray.orchestration.specialists import (
    ReviewVerdict,
    rule_review,
    run_extraction_team,
)
from moray.schema import Entity, ExtractionResult, Relation


# ---------------------------------------------------------------------------
# S1：被长名吸收的字面作为关系端点时，不得报「端点在正文中未命中」
# ---------------------------------------------------------------------------
def _absorbed_endpoint_case():
    text = "阿里巴巴发布新品。"
    logical = ExtractionResult(
        entities=[
            Entity(name="阿里巴巴", type="ORG"),
            Entity(name="阿里", type="ORG"),
        ],
        relations=[Relation(head="阿里", predicate="发布", tail="阿里巴巴")],
    )
    result, report = backfill_provenance(logical, text)
    return text, result, report


def test_relation_endpoint_absorbed_literal_is_grounded():
    text, result, report = _absorbed_endpoint_case()
    # 回填按最长匹配把 阿里 的命中给了 阿里巴巴：短名实体没有 mention，
    # 但字面确实在正文出现过（seen_literals）。
    assert "阿里" in report.seen_literals
    assert result.entities[1].mentions == []

    review = rule_review(text, result, backfill=report.to_dict())

    assert review.verdict == ReviewVerdict.PASS, review.issues
    assert not any("端点在正文中未命中" in issue for issue in review.issues)


@pytest.mark.asyncio
async def test_absorbed_endpoint_does_not_trigger_model_rework():
    text, result, report = _absorbed_endpoint_case()
    calls = {"n": 0}

    sink: dict = {}

    async def extractor(_text, _feedback):
        calls["n"] += 1
        # 真实抽取器会把本轮回填信号写进 sink，供规则审校当轮消费。
        sink["backfill"] = report.to_dict()
        return result

    reviewed = await run_extraction_team(
        text, extractor=extractor, max_iters=3, backfill_sink=sink)

    # 回归探针：修复前这里会 3 次返工并 accepted=False。
    assert calls["n"] == 1
    assert reviewed.accepted is True, reviewed.review.issues


# ---------------------------------------------------------------------------
# S2：重叠块合并后，同一全局 span 不得同时归属两个实体
# ---------------------------------------------------------------------------
class _OverlapTypeConflictFake:
    """两块正文重叠且内容相同，模型因上下文给出不同的类型。"""

    domain = SimpleNamespace(code="general")
    model_name = "fake-r10"
    local_backfill = True

    async def extract(self, document):
        entity_type = "ORG" if document.id.endswith("c0") else "MISC"
        logical = ExtractionResult(
            entities=[Entity(name="苹果", type=entity_type)])
        result, report = backfill_provenance(logical, document.text)
        return ExtractionOutput(
            document_id=document.id, domain_code="general",
            result=result, model_name=self.model_name, usage=Usage(),
            attempts=1, backfill=report.to_dict(),
        )


async def _run_overlap_type_conflict():
    from moray.extraction.chunking import Chunk
    from moray.extraction.pipeline import ChunkedExtractionPipeline

    body = "苹果发布了新品。"
    chunks = [
        Chunk(id="d#c0", text=body, start=0, end=len(body), index=0),
        Chunk(id="d#c1", text=body, start=0, end=len(body), index=1),
    ]
    return await ChunkedExtractionPipeline(
        _OverlapTypeConflictFake(), concurrency=1).run(
            Document(id="d", text=body), chunks)


@pytest.mark.asyncio
async def test_overlap_same_span_owned_by_two_entities_is_unattributed():
    out = await _run_overlap_type_conflict()

    owners = [e for e in out.result.entities if e.name == "苹果"]
    assert {e.type for e in owners} == {"ORG", "MISC"}
    # 同一处原文只能有一个归属；冲突时两边都不写 mention。
    assert all(e.mentions == [] for e in owners)

    ambiguous = out.backfill["ambiguous_mentions"]
    assert len(ambiguous) == 1
    entry = ambiguous[0]
    assert (entry["surface"], entry["start"], entry["end"]) == (
        "苹果", 0, 2)
    assert set(entry["candidates"]) == {
        '["ORG", "苹果"]', '["MISC", "苹果"]'}


@pytest.mark.asyncio
async def test_overlap_same_entity_keeps_single_span_mention():
    """同一实体在重叠块各命中一次：不是歧义，不得误删。"""

    class _SameEntityFake:
        domain = SimpleNamespace(code="general")
        model_name = "fake-r10"
        local_backfill = True

        async def extract(self, document):
            logical = ExtractionResult(
                entities=[Entity(name="苹果", type="ORG")])
            result, report = backfill_provenance(logical, document.text)
            return ExtractionOutput(
                document_id=document.id, domain_code="general",
                result=result, model_name=self.model_name, usage=Usage(),
                attempts=1, backfill=report.to_dict(),
            )

    from moray.extraction.chunking import Chunk
    from moray.extraction.pipeline import ChunkedExtractionPipeline

    body = "苹果发布了新品。"
    chunks = [
        Chunk(id="d#c0", text=body, start=0, end=len(body), index=0),
        Chunk(id="d#c1", text=body, start=0, end=len(body), index=1),
    ]
    out = await ChunkedExtractionPipeline(
        _SameEntityFake(), concurrency=1).run(
            Document(id="d", text=body), chunks)

    # naive_merge 会给同一实体保留两条 mention（overlap 重复），但它们
    # 归属同一实体，不是歧义信号。
    assert out.backfill["ambiguous_mentions"] == []
    assert [e.type for e in out.result.entities] == ["ORG"]


# ---------------------------------------------------------------------------
# N1：selected 候选的边界必须与真正写入的 evidence span 逐值对齐
# ---------------------------------------------------------------------------
def test_selected_candidate_bounds_match_evidence_span_after_trim():
    text = "Alice met Bob in Paris. Carol met Bob in London."
    logical = ExtractionResult(
        entities=[
            Entity(name="Bob", type="PER"),
            Entity(name="London", type="LOC"),
        ],
        relations=[Relation(head="Bob", predicate="lives_in", tail="London")],
    )
    result, report = backfill_provenance(logical, text)

    span = result.relations[0].evidence_span
    # 第二句以空格开头（index 23），证据 span 必须裁到正文起点 24。
    assert (span.start, span.end) == (24, 48)
    key = next(iter(report.relation_evidence_candidates))
    selected = [c for c in report.relation_evidence_candidates[key]
                if c.get("selected")]
    assert len(selected) == 1
    assert (selected[0]["evidence_start"], selected[0]["evidence_end"]) == (
        span.start, span.end)
    # 原始句边界仍保留，便于排查「为什么要裁」。
    assert (selected[0]["sent_start"], selected[0]["sent_end"]) == (23, 48)


# ---------------------------------------------------------------------------
# N2：跨块合并后全局只有一条 selected
# ---------------------------------------------------------------------------
def _single_relation_report(text, chunk_id, chunk_start):
    logical = ExtractionResult(
        entities=[
            Entity(name="Bob", type="PER"),
            Entity(name="Paris", type="LOC"),
        ],
        relations=[Relation(head="Bob", predicate="lives_in", tail="Paris")],
    )
    _result, report = backfill_provenance(logical, text)
    return BackfillReport.from_dict(
        report.to_dict(), chunk_start=chunk_start, chunk_id=chunk_id)


def test_merged_candidates_have_exactly_one_selected():
    text = "Bob lives in Paris."
    r0 = _single_relation_report(text, "d#c0", 0)
    r1 = _single_relation_report(text, "d#c1", 0)
    merged = merge_backfill_reports([r0, r1])

    key = next(iter(merged.relation_evidence_candidates))
    cands = merged.relation_evidence_candidates[key]
    assert len(cands) == 2
    assert len([c for c in cands if c.get("selected")]) == 1


# ---------------------------------------------------------------------------
# N3：unmatched_aliases 去重键带类型
# ---------------------------------------------------------------------------
def test_unmatched_aliases_keep_type_so_same_name_diff_type_do_not_collapse():
    logical = ExtractionResult(entities=[
        Entity(name="苹果", type="ORG", aliases=["Acme"]),
        Entity(name="苹果", type="MISC", aliases=["Acme"]),
    ])
    _result, r0 = backfill_provenance(logical, "苹果发布了新品。")
    assert len(r0.unmatched_aliases) == 2
    assert {u["type"] for u in r0.unmatched_aliases} == {"ORG", "MISC"}

    merged = merge_backfill_reports([
        BackfillReport.from_dict(r0.to_dict(), chunk_start=0, chunk_id="d#c0"),
        BackfillReport.from_dict(r0.to_dict(), chunk_start=9, chunk_id="d#c1"),
    ])
    assert len(merged.unmatched_aliases) == 2


# ---------------------------------------------------------------------------
# N4：关系证据候选序列化有上限，内存报告不丢
# ---------------------------------------------------------------------------
def test_relation_evidence_candidates_are_capped_in_serialized_signal():
    from moray.extraction import local_backfill as lb

    text = "华为深圳" * 30
    logical = ExtractionResult(
        entities=[
            Entity(name="华为", type="ORG"),
            Entity(name="深圳", type="LOC"),
        ],
        relations=[Relation(head="华为", predicate="位于", tail="深圳")],
    )
    _result, report = backfill_provenance(logical, text)
    key = next(iter(report.relation_evidence_candidates))
    assert len(report.relation_evidence_candidates[key]) == 900

    serialized = report.to_dict()
    cands = serialized["relation_evidence_candidates"][key]
    assert len(cands) == lb.MAX_SERIALIZED_RELATION_EVIDENCE_CANDIDATES
    assert len([c for c in cands if c.get("selected")]) == 1


class _MwAgent:
    """最小 agent 替身：只暴露中间件需要的两个审计属性。"""

    name = "Extractor"

    def __init__(self, *, source_text: str, local_backfill: bool):
        self._audit_source_text = source_text
        self._audit_local_backfill = local_backfill


def _slim_msg():
    from agentscope.message import Msg

    final = Msg(name="Extractor", role="assistant", content=[])
    final.structured_output = {
        "entities": [
            {"name": "华为", "type": "ORG", "aliases": [], "attributes": {}},
            {"name": "幽灵公司", "type": "ORG", "aliases": [], "attributes": {}},
        ],
        "relations": [],
    }
    return final


# ---------------------------------------------------------------------------
# N5：中间件零 span 记未测 + 回填只算一次
# ---------------------------------------------------------------------------
def test_cached_backfill_runs_one_scan_per_result(monkeypatch):
    from moray.extraction import local_backfill as lb

    calls = {"n": 0}
    real = lb.backfill_provenance

    def counting(result, text):
        calls["n"] += 1
        return real(result, text)

    monkeypatch.setattr(lb, "backfill_provenance", counting)
    owner = SimpleNamespace()
    result = ExtractionResult(entities=[Entity(name="华为", type="ORG")])
    filled_a, report_a = lb.cached_backfill_provenance(
        owner, result, "华为在深圳。")
    filled_b, report_b = lb.cached_backfill_provenance(
        owner, result, "华为在深圳。")

    assert calls["n"] == 1
    assert report_b is report_a
    assert filled_b.entities[0].mentions


@pytest.mark.asyncio
async def test_middleware_zero_span_is_unmeasured_and_reuses_extractor_cache(
        monkeypatch):
    from moray.extraction import local_backfill as lb
    from moray.observability import (
        ExtractionMetricsMiddleware,
        MetricsCollector,
    )

    calls = {"n": 0}
    real = lb.backfill_provenance

    def counting(result, text):
        calls["n"] += 1
        return real(result, text)

    monkeypatch.setattr(lb, "backfill_provenance", counting)

    collector = MetricsCollector()
    collector.start("Extractor")
    mw = ExtractionMetricsMiddleware(collector)

    async def fake_next():
        yield _slim_msg()

    agent = _MwAgent(source_text="华为在深圳。", local_backfill=True)
    [ev async for ev in mw.on_reply(agent, {}, fake_next)]
    rec = collector.current()

    # 零 span ≠ 100% 一致（未知记 None），确定性回填信号另行记录。
    assert rec.span_ratio is None
    assert rec.block_confidence == pytest.approx(0.5)
    assert calls["n"] == 1

    # 抽取器随后对同一份结果做回填时直接复用中间件算好的产物。
    result = ExtractionResult.model_validate(_slim_msg().structured_output)
    _filled, report = lb.cached_backfill_provenance(
        agent, result, "华为在深圳。")
    assert calls["n"] == 1
    assert report.block_confidence == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# N8：extraction/__init__ 无重复导入
# ---------------------------------------------------------------------------
def test_extraction_package_init_has_no_duplicate_imports():
    import inspect
    import moray.extraction as pkg

    src = inspect.getsource(pkg)
    for line in ("from .chunking import",
                 "from .document import",
                 "from .extractor import"):
        assert src.count(line) == 1, f"重复导入：{line}"


# ---------------------------------------------------------------------------
# N9：slim 归并身份键与回填一致（去首尾空白）；关闭态保持精确键
# ---------------------------------------------------------------------------
def test_naive_merge_identity_key_can_ignore_surrounding_whitespace():
    p0 = ExtractionResult(entities=[Entity(name=" 腾讯", type="ORG")])
    p1 = ExtractionResult(entities=[Entity(name="腾讯", type="ORG")])

    slim = naive_merge([p0, p1], strip_entity_name=True)
    assert len(slim.entities) == 1
    assert slim.entities[0].name == " 腾讯"

    legacy = naive_merge([p0, p1])
    assert len(legacy.entities) == 2


# ---------------------------------------------------------------------------
# N10：naive_merge 合并关系时不改写入参 partial
# ---------------------------------------------------------------------------
def test_naive_merge_does_not_mutate_input_relations():
    first = Relation(head="腾讯", predicate="发布", tail="混元")
    second = Relation(
        head="腾讯", predicate="发布", tail="混元", confidence=0.9,
        evidence_span={"start": 0, "end": 2, "text": "腾讯"},
    )
    p0 = ExtractionResult(relations=[first])
    p1 = ExtractionResult(relations=[second])

    merged = naive_merge(
        [p0, p1], merge_relation_duplicates=True, fill_description=True)

    assert len(merged.relations) == 1
    assert merged.relations[0].confidence == pytest.approx(0.9)
    # 入参对象保持原样，调用方可继续复用 partials。
    assert first.confidence is None
    assert first.evidence_span is None
