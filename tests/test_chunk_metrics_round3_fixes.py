"""工单 01 第 3 轮评审修复的离线回归（不联网、不依赖 DB）。

覆盖：
  · S1 短文档永久失败时，真实 DynamicExtractorProvider 闭包也必须把
    token 成本与截断信号交给 usage/metrics sink，失败看板行不能是 0；
  · S1 失败路径的 DocumentMetrics 里 chunk_id 是真实 document_id（nit5）；
  · S2 DB 集成测试的字段集合必须与 DocumentMetrics.to_dict() 逐字一致，
    不依赖 RUN_DB_TESTS 也能在 CI 里守住（防止字段漂移被 skip 掩盖）。
"""

from types import SimpleNamespace

import pytest

from moray.api.app import AppDeps, create_app
from moray.extraction import (
    ChunkCallMetrics,
    Document,
    ExtractionError,
    Extractor,
)
from moray.extraction.chunk_metrics import DocumentMetrics
from moray.extraction.document import Usage
from moray.extraction.provider import DynamicExtractorProvider
from moray.jobs.handlers import build_document_handler
from moray.kb import IngestReport

SHORT_TEXT = "甲公司与乙公司在上海签约。"


class _FakeDashboard:
    def __init__(self):
        self.rows = []

    async def record(self, recorder):
        self.rows.append(recorder)
        return len(self.rows)


class _FakeIngestor:
    async def ingest(self, output):
        return IngestReport()


class _StubResolver:
    """provider.get_extractor 被替换掉，resolver 只需满足构造签名。"""

    async def resolve_with_version(self, domain_code):  # pragma: no cover
        raise AssertionError("get_extractor 已在用例里替换")


class _FailingExtractor(Extractor):
    """模拟短文档连续 3 次 length 仍校验失败：异常上已有块级度量。"""

    def __init__(self):
        super().__init__("general", max_retries=2)

    async def extract(self, document):  # type: ignore[override]
        raise ExtractionError(
            "永久失败",
            metrics=ChunkCallMetrics(
                chunk_id=document.id, attempts=3, finish_reason="length",
                input_tokens=900, output_tokens=1800,
                cached_input_tokens=0, wait_s=0.0,
                prefill_s=None, decode_s=None, latency_s=2.5,
                attempt_finish_reasons=["length", "length", "length"],
                truncated_attempts=3, error="校验失败"))


def _failing_provider():
    provider = DynamicExtractorProvider(_StubResolver())
    extractor = _FailingExtractor()

    async def _get_extractor(domain_code):
        return extractor, SimpleNamespace(code="general")

    provider.get_extractor = _get_extractor  # type: ignore[assignment]
    return provider


@pytest.mark.asyncio
async def test_provider_closure_hands_failure_metrics_to_sinks():
    """S1：闭包失败时 usage/metrics sink 不能还是空的。"""
    usage = Usage()
    sink: list = []
    provider = _failing_provider()
    fn = await provider.extract(
        "general", usage_sink=usage, metrics_sink=sink)

    with pytest.raises(ExtractionError):
        await fn(SHORT_TEXT)

    assert (usage.input_tokens, usage.output_tokens) == (900, 1800)
    assert len(sink) == 1
    assert sink[0].truncated_attempts == 3
    assert sink[0].input_tokens == 900


@pytest.mark.asyncio
async def test_api_short_document_failure_keeps_cost_and_truncation():
    """S1：/extract 短文档永久失败的看板行必须有真实 token 与截断信号。"""
    from httpx import ASGITransport, AsyncClient

    dash = _FakeDashboard()
    deps = AppDeps(pool=None, store=None, kb=None, ingestor=_FakeIngestor(),
                   dashboard=dash, extractor_provider=_failing_provider())
    app = create_app(deps)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.post("/extract", json={"text": SHORT_TEXT,
                                            "document_id": "short-fail"})
    assert r.status_code == 500
    assert len(dash.rows) == 1
    row = dash.rows[0]
    assert row.review_passed is False
    assert (row.input_tokens, row.output_tokens) == (900, 1800)
    assert row.truncated_chunks == 1
    assert row.truncation_signals == 1
    assert row.chunk_calls == 1
    assert row.total_attempts == 3
    # nit5：失败块的 chunk_id 也必须是真实 document_id，不能是 "general-dyn"
    assert row.chunk_metrics_json[0]["chunk_id"] == "short-fail"


@pytest.mark.asyncio
async def test_batch_short_document_failure_keeps_cost_and_truncation(tmp_path):
    """S1（批量侧）：handler 短文档永久失败的看板行同样不能丢成本/信号。"""
    f = tmp_path / "short.txt"
    f.write_text(SHORT_TEXT, encoding="utf-8")
    dash = _FakeDashboard()
    handler = await build_document_handler(
        store=None, ingestor=_FakeIngestor(),
        extractor_provider=_failing_provider(), dashboard=dash)

    with pytest.raises(ExtractionError):
        await handler({"document_id": "job-short-fail",
                       "document_ref": str(f), "domain_code": "general"})

    assert len(dash.rows) == 1
    row = dash.rows[0]
    assert (row.input_tokens, row.output_tokens) == (900, 1800)
    assert row.truncation_signals == 1
    assert row.chunk_calls == 1
    assert row.chunk_metrics_json[0]["chunk_id"] == "job-short-fail"


def test_chunk_field_sets_match_serialization_offline():
    """S2：DB 套件的字段集合必须与序列化结果一致（CI 不跑 DB 也要能红）。

    直接复用 tests/test_chunk_metrics_integration.py 里的期望集合，避免两处
    常量各写一份又漂移——r2 加 latency_s 时正是这两个集合没同步，导致
    RUN_DB_TESTS=1 一开就红、而 CI 永远 skip 看不出来。
    """
    from test_chunk_metrics_integration import CHUNK_KEYS, SUMMARY_KEYS

    dm = DocumentMetrics(document_id="d", n_chunks=1)
    dm.add(ChunkCallMetrics(
        chunk_id="d#c0000", attempts=1, finish_reason="stop",
        input_tokens=10, output_tokens=5, cached_input_tokens=0,
        wait_s=0.0, prefill_s=None, decode_s=None, latency_s=0.4))
    payload = dm.to_dict()

    assert set(payload["summary"]) == SUMMARY_KEYS
    assert set(payload["chunks"][0]) == CHUNK_KEYS
