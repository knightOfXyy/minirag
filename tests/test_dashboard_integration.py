"""质量看板 pipeline_runs 的 DB 集成测试（需要 moray-pgvector）。"""

import os

import pytest

from moray.kb import close_pool, init_pool
from moray.observability import Dashboard, RunRecorder

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DB_TESTS") != "1",
    reason="需要 PostgreSQL；设置 RUN_DB_TESTS=1 且启动 moray-pgvector 后运行",
)


@pytest.fixture()
async def dash():
    pool = await init_pool()
    d = Dashboard(pool)
    await d.init_schema()
    await pool.execute("TRUNCATE pipeline_runs RESTART IDENTITY")
    yield d, pool
    await close_pool(pool)


async def _rec(dash, **kw):
    defaults = dict(
        document_id="d", n_entities=5, n_relations=2, span_ratio=1.0,
        review_passed=True, review_iters=1,
        n_auto_merged=1, n_queued=0, n_created=4,
        input_tokens=100, output_tokens=50, latency_ms=1000)
    defaults.update(kw)
    return await dash.record(RunRecorder(**defaults))


@pytest.mark.asyncio
async def test_record_and_recent(dash):
    d, pool = dash
    await _rec(d, document_id="a")
    await _rec(d, document_id="b")
    rows = await d.recent()
    assert {r["document_id"] for r in rows} == {"a", "b"}


@pytest.mark.asyncio
async def test_summary_aggregates(dash):
    d, _ = dash
    await _rec(d, document_id="a", review_passed=True, span_ratio=1.0,
               n_auto_merged=2, n_queued=0, input_tokens=100, output_tokens=100,
               cached_input_tokens=80)
    await _rec(d, document_id="b", review_passed=False, span_ratio=0.5,
               review_iters=3, open_issues=4,
               n_auto_merged=1, n_queued=3, input_tokens=300, output_tokens=200,
               cached_input_tokens=60)

    s = await d.summary()
    assert s["runs"] == 2
    assert s["passed"] == 1
    assert s["pass_rate"] == 0.5
    assert s["avg_span_ratio"] == 0.75
    assert s["total_open_issues"] == 4
    # 自动合并 3 次，转人工 3 次 -> 自动化率 0.5
    assert s["auto_merged"] == 3 and s["queued"] == 3
    assert s["auto_merge_rate"] == 0.5
    assert s["input_tokens"] == 400 and s["output_tokens"] == 300
    # 提示缓存：(80+60)/400 = 0.35
    assert s["cached_input_tokens"] == 140
    assert s["cache_hit_rate"] == 0.35


@pytest.mark.asyncio
async def test_summary_empty_is_safe(dash):
    d, _ = dash
    s = await d.summary()
    assert s["runs"] == 0
    assert s["pass_rate"] == 0.0
    assert s["auto_merge_rate"] is None  # 没有消歧决策时分母为 0
    assert s["cache_hit_rate"] == 0.0   # 没有 token 时不能除零


@pytest.mark.asyncio
async def test_summary_domain_filter(dash):
    d, _ = dash
    await _rec(d, document_id="a", domain_code="general")
    await _rec(d, document_id="b", domain_code="finance")
    assert (await d.summary(domain_code="finance"))["runs"] == 1
    assert (await d.summary(domain_code="general"))["runs"] == 1


@pytest.mark.asyncio
async def test_summary_includes_backfill_signals(dash):
    """看板消费确定性回填命中率（开关关闭时为 NULL 聚合）。"""
    d, _ = dash
    await _rec(d, document_id="a", block_confidence=0.5,
               entities_grounded=1, entities_total=2,
               relation_evidence_ratio=0.5, ambiguous_mentions=1)
    await _rec(d, document_id="b")
    s = await d.summary()
    assert s["avg_block_confidence"] == 0.5   # NULL 不参与平均（b 未测，不算 0）
    assert (s["entities_grounded"], s["entities_total"]) == (1, 2)
    assert s["ambiguous_mentions"] == 1
    assert s["avg_relation_evidence_ratio"] == 0.5
