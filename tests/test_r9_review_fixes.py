"""工单 03 第 9 轮评审（r9）发现的回归用例。

只钉住本轮要修的、可离线验证的行为：

* S1（should）看板汇总对「未测」的回填列不得 coalesce 成 0%；
* N1（nit）RunRecorder 的回填字段只声明一次；
* N2（nit）只有空白差异的别名不是「幻觉别名」；
* N3（nit）实体字面被更长实体吸收时，不得误报「未在正文中找到出处」；
* N4（nit）重叠块歧义去重键不能依赖候选顺序；
* N5（nit）质量中间件只在抽取器显式开启回填时才计算，不再自创第三套
  slim 判定；
* N6（nit）full 动态关系模型的字段集合必须跟内核 Relation 同步；
* N10（nit）关闭态 confidence=None 是刻意扩展，补差分外的固定用例；
* N11（nit）from_dict 显式传 chunk_start=0 不能被字典里的旧值覆盖；
* N12（nit）空字符串 source_text 在 pipeline/extractor 中语义一致。
"""

from types import SimpleNamespace

import pytest
from agentscope.message import Msg

from moray.extraction.local_backfill import (
    AmbiguousMention,
    BackfillReport,
    backfill_provenance,
    merge_backfill_reports,
)
from moray.observability import (
    Dashboard,
    ExtractionMetricsMiddleware,
    MetricsCollector,
    RunRecorder,
)
from moray.orchestration.specialists import ReviewVerdict, rule_review
from moray.schema import (
    GENERAL_DOMAIN,
    Entity,
    ExtractionResult,
    Relation,
    build_extraction_model,
)


# ---------------------------------------------------------------------------
# S1：全 NULL 的回填列聚合必须是「未测」，不是 0
# ---------------------------------------------------------------------------
class _NullBackfillPool:
    """返回「所有回填列都是 NULL」的聚合行，并捕获 SQL。"""

    def __init__(self):
        self.query = ""

    async def fetchrow(self, query, *params):
        self.query = query
        return {
            "runs": 2, "passed": 1, "avg_span_ratio": None,
            "avg_block_confidence": None,
            "entities_grounded": None, "entities_total": None,
            "avg_relation_evidence_ratio": None, "ambiguous_mentions": 0,
            "avg_review_iters": 1.0, "total_open_issues": 0,
            "total_entities": 0, "total_relations": 0,
            "auto_merged": 0, "queued": 0, "created": 0,
            "input_tokens": 0, "output_tokens": 0,
            "cached_input_tokens": 0, "cache_hits": 0,
            "avg_latency_ms": 0.0,
            "chunk_calls": 0, "total_attempts": 0,
            "truncated_chunks": 0, "truncation_signals": 0,
            "reworked_chunks": 0, "wait_ms": 0,
            "chunk_latency_ms": None, "prefill_ms": None, "decode_ms": None,
            "chunk_latency_measured_runs": 0,
            "prefill_measured_runs": 0, "decode_measured_runs": 0,
        }


@pytest.mark.asyncio
async def test_dashboard_all_unmeasured_backfill_stays_null():
    pool = _NullBackfillPool()
    s = await Dashboard(pool).summary()

    assert s["avg_block_confidence"] is None, "未知≠0：全 NULL 不能聚合成 0%"
    assert s["entities_grounded"] is None
    assert s["entities_total"] is None
    assert s["avg_relation_evidence_ratio"] is None

    q = pool.query
    for col in ("avg_block_confidence", "entities_grounded",
                "entities_total", "avg_relation_evidence_ratio"):
        assert f"coalesce(avg({col}),0)" not in q
        assert f"coalesce(sum({col}),0)" not in q
        assert f"coalesce(sum({col}), 0)" not in q


# ---------------------------------------------------------------------------
# N1：回填字段只声明一次
# ---------------------------------------------------------------------------
def test_run_recorder_backfill_fields_declared_once():
    import inspect
    import re

    # dataclass 的 __annotations__/__dataclass_fields__ 都是 dict，重复声明
    # 会被静默覆盖；只能查类源码里每个字段注解出现的次数。
    src = inspect.getsource(RunRecorder)
    for name in ("block_confidence", "entities_grounded", "entities_total",
                 "relation_evidence_ratio", "ambiguous_mentions"):
        assert len(re.findall(rf"^\s+{name}:", src, flags=re.M)) == 1, (
            f"{name} 在 RunRecorder 源码里重复声明")


# ---------------------------------------------------------------------------
# N2：只有空白差异的别名不应被当成幻觉
# ---------------------------------------------------------------------------
def test_whitespace_only_alias_is_not_a_hallucination():
    text = "混元发布了新品。"
    _result, report = backfill_provenance(
        ExtractionResult(entities=[
            Entity(name=" 腾讯", type="ORG", aliases=["腾讯"]),
            Entity(name="混元", type="MISC"),
        ]),
        text,
    )
    assert report.unmatched_aliases == []


# ---------------------------------------------------------------------------
# N3：被长实体吸收的短实体字面不算「没有出处」
# ---------------------------------------------------------------------------
def test_entity_literal_absorbed_by_longer_entity_is_not_missing():
    text = "阿里巴巴发布新品。"
    logical = ExtractionResult(entities=[
        Entity(name="阿里巴巴", type="ORG"),
        Entity(name="阿里", type="ORG"),
    ])
    result, report = backfill_provenance(logical, text)
    assert "阿里" in report.seen_literals
    review = rule_review(text, result, backfill=report.to_dict())
    assert review.verdict == ReviewVerdict.PASS, review.issues
    assert not any("未在正文中找到出处" in issue for issue in review.issues)


# ---------------------------------------------------------------------------
# N4：重叠块同一处歧义按候选集合去重，不依赖实体输出顺序
# ---------------------------------------------------------------------------
def test_ambiguous_mentions_deduped_regardless_of_candidate_order():
    a = BackfillReport(
        chunk_id="c0", chunk_start=0,
        ambiguous_mentions=[AmbiguousMention(
            surface="同名", start=0, end=2,
            candidates=['["ORG", "腾讯"]', '["ORG", "网易"]'],
            chunk_id="c0")],
    )
    b = BackfillReport(
        chunk_id="c1", chunk_start=0,
        ambiguous_mentions=[AmbiguousMention(
            surface="同名", start=0, end=2,
            candidates=['["ORG", "网易"]', '["ORG", "腾讯"]'],
            chunk_id="c1")],
    )
    merged = merge_backfill_reports([a, b])
    assert len(merged.ambiguous_mentions) == 1
    assert merged.ambiguous_mentions[0].chunk_ids == ["c0", "c1"]


# ---------------------------------------------------------------------------
# N5：中间件回填只由显式开关驱动
# ---------------------------------------------------------------------------
class _MwAgent:
    name = "Extractor"

    def __init__(self, *, source_text: str, local_backfill: bool):
        self._audit_source_text = source_text
        self._audit_local_backfill = local_backfill


def _slim_msg():
    final = Msg(name="Extractor", role="assistant", content=[])
    final.structured_output = {
        "entities": [
            {"name": "华为", "type": "ORG", "aliases": [], "attributes": {}},
            {"name": "幽灵公司", "type": "ORG", "aliases": [], "attributes": {}},
        ],
        "relations": [],
    }
    return final


@pytest.mark.asyncio
async def test_middleware_does_not_backfill_when_switch_is_off():
    collector = MetricsCollector()
    collector.start("Extractor")
    mw = ExtractionMetricsMiddleware(collector)

    async def fake_next():
        yield _slim_msg()

    agent = _MwAgent(source_text="华为在深圳。", local_backfill=False)
    [ev async for ev in mw.on_reply(agent, {}, fake_next)]
    rec = collector.current()
    assert rec.block_confidence is None
    assert rec.entities_total is None


@pytest.mark.asyncio
async def test_middleware_backfills_when_extractor_switch_is_on():
    collector = MetricsCollector()
    collector.start("Extractor")
    mw = ExtractionMetricsMiddleware(collector)

    async def fake_next():
        yield _slim_msg()

    agent = _MwAgent(source_text="华为在深圳。", local_backfill=True)
    [ev async for ev in mw.on_reply(agent, {}, fake_next)]
    rec = collector.current()
    assert rec.entities_total == 2
    assert rec.entities_grounded == 1
    assert rec.block_confidence == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# N6：full 动态关系模型与内核 Relation 字段同步
# ---------------------------------------------------------------------------
def test_full_dynamic_relation_fields_match_core_relation():
    model = build_extraction_model(GENERAL_DOMAIN)
    dyn_rel = model.model_fields["relations"].annotation.__args__[0]
    assert set(dyn_rel.model_fields) == set(Relation.model_fields)


# ---------------------------------------------------------------------------
# N10：关闭态 confidence=None 分支是刻意扩展
# ---------------------------------------------------------------------------
def test_off_mode_confidence_none_branch_is_intentional():
    text = "腾讯发布了混元。"
    result = ExtractionResult(
        entities=[Entity(name="腾讯", type="ORG"), Entity(name="混元", type="MISC")],
        relations=[Relation(head="腾讯", predicate="发布", tail="混元")],
    )
    review = rule_review(text, result, backfill=None)
    assert review.verdict == ReviewVerdict.NEEDS_FIX
    assert review.issues == [
        "关系 腾讯-[发布]->混元 无原文证据（置信度未知）"]


# ---------------------------------------------------------------------------
# N11：from_dict 显式传 0 不能被字典旧值覆盖
# ---------------------------------------------------------------------------
def test_from_dict_explicit_zero_chunk_start_wins():
    report = BackfillReport.from_dict(
        {"chunk_start": 999}, chunk_start=0, chunk_id="c0")
    assert report.chunk_start == 0


def test_from_dict_default_still_reads_fallback_chunk_start():
    report = BackfillReport.from_dict({"chunk_start": 999})
    assert report.chunk_start == 999


# ---------------------------------------------------------------------------
# N12：空 source_text 不回退到带反馈的 text
# ---------------------------------------------------------------------------
class _EmptySourceTextFake:
    domain = SimpleNamespace(code="general")
    model_name = "fake-r9"
    local_backfill = True

    async def extract(self, document):
        from moray.extraction.document import ExtractionOutput, Usage

        # 模拟「抽取器块内只看到正文，但报告里仍有一条未被本块采集的别名」。
        report = BackfillReport(
            chunk_id=document.id, chunk_start=0,
            entities_total=1, entities_grounded=1,
            seen_literals=["腾讯"],
            unmatched_aliases=[{"entity": "腾讯", "literal": "鹅厂"}],
        )
        return ExtractionOutput(
            document_id=document.id, domain_code="general",
            result=ExtractionResult(entities=[
                Entity(name="腾讯", type="ORG", aliases=["鹅厂"])]),
            model_name=self.model_name, usage=Usage(), attempts=1,
            backfill=report.to_dict(),
        )


@pytest.mark.asyncio
async def test_empty_source_text_is_not_replaced_by_feedback_body():
    from moray.extraction.chunking import Chunk
    from moray.extraction.document import Document
    from moray.extraction.pipeline import ChunkedExtractionPipeline

    body = "腾讯发布了新品。上次问题：鹅厂 未出现"
    chunks = [Chunk(id="d#c0", text=body, start=0, end=len(body), index=0)]
    out = await ChunkedExtractionPipeline(
        _EmptySourceTextFake(), concurrency=1).run(
            Document(id="d", text=body, source_text=""), chunks)

    assert out.backfill["unmatched_aliases"] == [
        {"entity": "腾讯", "literal": "鹅厂"}]
