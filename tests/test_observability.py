"""可观测中间件测试：用假的 next_handler 驱动，不联网。"""

from types import SimpleNamespace

import pytest

from moray.observability import (
    AuditMiddleware,
    CostAccountingMiddleware,
    ExtractionMetricsMiddleware,
    MetricsCollector,
)
from agentscope.message import Msg, UserMsg


class _FakeAgent:
    name = "Extractor"

    def __init__(self, source_text=None):
        if source_text is not None:
            self._audit_source_text = source_text


# --------------------------------------------------------------------------
# AuditMiddleware.on_model_call：应记录 token 与耗时，并原样返回响应
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_audit_records_model_usage():
    collector = MetricsCollector()
    mw = AuditMiddleware(collector)
    collector.start("Extractor")

    async def fake_next():
        return SimpleNamespace(usage=SimpleNamespace(
            input_tokens=100, output_tokens=50,
            cache_input_tokens=0, cache_creation_input_tokens=0))

    agent = _FakeAgent()
    resp = await mw.on_model_call(
        agent, {"messages": [1, 2], "tools": [{"x": 1}]}, fake_next
    )
    rec = collector.current()
    assert resp.usage.input_tokens == 100
    assert len(rec.model_calls) == 1
    assert rec.input_tokens == 100 and rec.output_tokens == 50
    assert rec.model_calls[0].messages_in == 2
    assert rec.model_calls[0].tools_offered == 1
    assert rec.model_calls[0].latency_s >= 0


@pytest.mark.asyncio
async def test_audit_records_failed_model_call():
    collector = MetricsCollector()
    mw = AuditMiddleware(collector)
    collector.start("x")

    async def boom():
        raise RuntimeError("rate limited")

    with pytest.raises(RuntimeError):
        await mw.on_model_call(_FakeAgent(), {}, boom)
    rec = collector.current()
    assert rec.model_calls[0].ok is False
    assert "rate limited" in rec.model_calls[0].error


# --------------------------------------------------------------------------
# CostAccountingMiddleware.on_reply：应透传事件并记录总耗时
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cost_reply_passthrough_and_timing():
    collector = MetricsCollector()
    mw = CostAccountingMiddleware(collector)

    async def fake_next():
        for ev in ["a", "b", "c"]:
            yield ev

    events = [ev async for ev in mw.on_reply(_FakeAgent(), {"inputs": None}, fake_next)]
    assert events == ["a", "b", "c"]
    rec = collector.records[-1]
    assert rec.duration_s >= 0
    assert collector._active is None  # finish 已被调用


# --------------------------------------------------------------------------
# ExtractionMetricsMiddleware：从最终结构化 Msg 算实体数/低置信/质量分
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_extraction_metrics_counts_and_scores():
    collector = MetricsCollector()
    collector.start("Extractor")
    mw = ExtractionMetricsMiddleware(collector)

    text = "华为在深圳。"
    final = Msg(name="Extractor", role="assistant", content=[])
    final.structured_output = {
        "entities": [
            # span 准确、高置信
            {"name": "华为", "type": "ORG", "confidence": 0.95,
             "mentions": [{"surface": "华为",
                           "span": {"start": 0, "end": 2, "text": "华为"}}]},
            # 低置信
            {"name": "深圳", "type": "LOC", "confidence": 0.4,
             "mentions": [{"surface": "深圳",
                           "span": {"start": 3, "end": 5, "text": "深圳"}}]},
        ],
        "relations": [],
    }

    async def fake_next():
        yield UserMsg(name="u", content="x")  # 非最终 Msg，应被忽略
        yield final

    agent = _FakeAgent(source_text=text)
    events = [ev async for ev in mw.on_reply(agent, {}, fake_next)]
    assert len(events) == 2  # 必须透传所有事件，不能吞

    rec = collector.current()
    assert rec.n_entities == 2
    assert rec.low_confidence_entities == 1
    assert rec.span_ratio == 1.0
    # 0.5*1(span) + 0.3*0.5(一半低置信) + 0.2*1(非空) = 0.85
    assert rec.quality_score == pytest.approx(0.85, abs=1e-6)


def test_collector_totals():
    c = MetricsCollector()
    r = c.start("a")
    r.model_calls.append(
        SimpleNamespace(input_tokens=10, output_tokens=5, cached_input_tokens=4)
    )
    r.n_entities = 3
    r.n_relations = 1
    c.finish()
    totals = c.totals()
    assert totals == {"runs": 1, "input_tokens": 10, "output_tokens": 5,
                      "cached_input_tokens": 4, "cache_hit_rate": 0.4,
                      "entities": 3, "relations": 1}


@pytest.mark.asyncio
async def test_slim_output_without_confidence_is_not_counted_as_low_confidence():
    """瘦身输出没有 confidence 字段 = 未知，不等于「低置信」；块级信号另走 backfill。"""
    collector = MetricsCollector()
    collector.start("Extractor")
    mw = ExtractionMetricsMiddleware(collector)

    text = "华为在深圳。"
    final = Msg(name="Extractor", role="assistant", content=[])
    final.structured_output = {
        "entities": [
            {"name": "华为", "type": "ORG", "aliases": [], "attributes": {}},
            {"name": "深圳", "type": "LOC", "aliases": [], "attributes": {}},
        ],
        "relations": [],
    }

    async def fake_next():
        yield final

    agent = _FakeAgent(source_text=text)
    [ev async for ev in mw.on_reply(agent, {}, fake_next)]
    rec = collector.current()
    assert rec.n_entities == 2
    assert rec.low_confidence_entities == 0
    # span 检查没有 mention -> ratio=1；未知置信度不扣分；非空得分
    assert rec.quality_score == pytest.approx(1.0, abs=1e-6)
