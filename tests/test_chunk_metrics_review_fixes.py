"""工单 01 第 1 轮评审修复的离线回归（不联网、不依赖 DB）。

覆盖：
  · F4 短文档的截断信号也进入文档级度量/看板；
  · F5 永久失败块在 API/批量路径落一行失败观测；
  · F7 API 与批量看板行带 source，可按来源过滤；
  · F10 API 传输层与作业 handler 产出同一套分块字段；
  · F12 观测库故障不拖垮 /extract。
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
from moray.jobs.handlers import build_document_handler
from moray.kb import IngestReport
from moray.observability import RunRecorder
from moray.orchestration import run_chunked_team
from moray.schema import ExtractionResult

_GOOD = {"entities": [], "relations": []}
_BAD = {"entities": [{"name": "x", "type": "NOT_A_TYPE"}], "relations": []}
LONG_TEXT = "甲公司与乙公司在上海签约共建数据中心，推动产业协同发展。" * 8


class _FakeDashboard:
    def __init__(self, *, fail: bool = False):
        self.rows: list[RunRecorder] = []
        self.fail = fail

    async def record(self, recorder: RunRecorder) -> int:
        if self.fail:
            raise RuntimeError("观测库不可用")
        self.rows.append(recorder)
        return len(self.rows)


class _FakeIngestor:
    async def ingest(self, output):
        return IngestReport()


class _ProbeExtractor(Extractor):
    """按脚本返回假回复；attrs 带 finish_reason/token/分段计时。"""

    def __init__(self, replies, *, max_retries=1):
        super().__init__("general", max_retries=max_retries)
        self._script = list(replies)

    def _build_agent(self):
        return SimpleNamespace()

    async def _call_structured(self, agent, content):
        reply, attrs = self._script.pop(0)
        return SimpleNamespace(
            structured_output=reply,
            usage=SimpleNamespace(
                input_tokens=attrs.get("input_tokens", 100),
                output_tokens=attrs.get("output_tokens", 10),
                cache_input_tokens=attrs.get("cache_input_tokens", 0)),
            finish_reason=attrs.get("finish_reason", "stop"),
            timing=SimpleNamespace(
                prefill_s=attrs.get("prefill_s"),
                decode_s=attrs.get("decode_s")))


class _ShortProvider:
    """短文档 provider：extract 支持 metrics_sink，把每次调用度量带出去。"""

    def __init__(self, extractor):
        self.extractor = extractor

    async def extract(self, domain_code, usage_sink=None, metrics_sink=None):
        async def _fn(text, feedback=None):
            out = await self.extractor.extract(
                Document(id="short-doc", text=text, domain_code=domain_code))
            if usage_sink is not None:
                usage_sink.add(out.usage)
            if metrics_sink is not None:
                metrics_sink.append(out.call_metrics)
            return out.result

        return _fn


@pytest.mark.asyncio
async def test_short_document_truncation_signal_reaches_document_metrics():
    """F4：短文档首轮 length、返工成功，也要留下 truncation_signals=1。"""
    extractor = _ProbeExtractor([
        (_BAD, {"finish_reason": "length", "output_tokens": 200}),
        (_GOOD, {"finish_reason": "stop"}),
    ])
    provider = _ShortProvider(extractor)

    async def extractor_fn(text, feedback=None):
        raise AssertionError("短文档应使用带 metrics_sink 的 provider 闭包")

    reviewed = await run_chunked_team(
        "短文本", extractor=extractor_fn, provider=provider,
        chunk_threshold_units=4000)
    dm = reviewed.document_metrics
    assert dm is not None
    s = dm.summary()
    assert s["n_chunks"] == 1
    assert s["chunk_calls"] == 1
    assert s["truncated_chunks"] == 0
    assert s["truncation_signals"] == 1
    assert s["total_attempts"] == 2

    rec = RunRecorder.from_pipeline(
        document_id="short-doc", reviewed=reviewed,
        ingest_report=IngestReport())
    assert rec.truncation_signals == 1
    assert rec.prefill_ms is None and rec.decode_ms is None


class _FailingExtractor:
    """模拟某块连续两次 length 仍校验失败：块级度量里已有截断信号。"""

    def __init__(self):
        self.domain = SimpleNamespace(code="general")

    async def extract(self, document):
        metrics = ChunkCallMetrics(
            chunk_id=document.id, attempts=2, finish_reason="length",
            input_tokens=200, output_tokens=400, cached_input_tokens=0,
            wait_s=0.0, prefill_s=None, decode_s=None,
            attempt_finish_reasons=["length", "length"],
            truncated_attempts=2, error="校验失败")
        raise ExtractionError("永久失败", metrics=metrics)


class _FailingProvider:
    async def get_extractor(self, domain_code):
        return _FailingExtractor(), SimpleNamespace(code="general")

    async def extract(self, domain_code, usage_sink=None, metrics_sink=None):
        async def _fn(text, feedback=None):
            raise ExtractionError("永久失败")
        return _fn


@pytest.mark.asyncio
async def test_api_failure_records_document_metrics_with_truncation(monkeypatch):
    """F5：整篇失败（含截断）也要写一行 pipeline_runs，再原样抛错。"""
    from httpx import ASGITransport, AsyncClient

    monkeypatch.setenv("MORAY_CHUNK_THRESHOLD_UNITS", "10")
    monkeypatch.setenv("MORAY_CHUNK_MAX_UNITS", "40")
    monkeypatch.setenv("MORAY_CHUNK_OVERLAP_UNITS", "0")
    monkeypatch.setenv("MORAY_CHUNK_CONCURRENCY", "1")
    dash = _FakeDashboard()
    deps = AppDeps(pool=None, store=None, kb=None, ingestor=_FakeIngestor(),
                   dashboard=dash, extractor_provider=_FailingProvider())
    app = create_app(deps)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.post("/extract", json={"text": LONG_TEXT,
                                            "document_id": "fail1"})
    assert r.status_code == 500
    assert len(dash.rows) == 1
    row = dash.rows[0]
    assert row.document_id == "fail1"
    assert row.source == "api"
    assert row.review_passed is False
    assert row.truncated_chunks >= 1
    assert row.truncation_signals >= 1
    assert row.chunk_metrics_json[0]["truncated"] is True


@pytest.mark.asyncio
async def test_extract_survives_dashboard_record_failure():
    """F12：观测库写失败只记 warning，不能让实时抽取 500。"""
    from httpx import ASGITransport, AsyncClient

    async def extractor_fn(text, feedback=None):
        return ExtractionResult(entities=[], relations=[])

    deps = AppDeps(pool=None, store=None, kb=None, ingestor=_FakeIngestor(),
                   dashboard=_FakeDashboard(fail=True),
                   extractor_fn=extractor_fn)
    app = create_app(deps)
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://t") as ac:
        r = await ac.post("/extract", json={"text": "短文本",
                                            "document_id": "ok1"})
    assert r.status_code == 200, r.text
    assert r.json()["document_id"] == "ok1"


@pytest.mark.asyncio
async def test_batch_handler_failure_records_document_metrics(monkeypatch, tmp_path):
    """F5（批量侧）：永久失败也写看板行，再原样抛给 BatchRunner。"""
    monkeypatch.setenv("MORAY_CHUNK_THRESHOLD_UNITS", "10")
    monkeypatch.setenv("MORAY_CHUNK_MAX_UNITS", "40")
    monkeypatch.setenv("MORAY_CHUNK_OVERLAP_UNITS", "0")
    monkeypatch.setenv("MORAY_CHUNK_CONCURRENCY", "1")
    f = tmp_path / "fail.txt"
    f.write_text(LONG_TEXT, encoding="utf-8")
    dash = _FakeDashboard()
    handler = await build_document_handler(
        store=None, ingestor=_FakeIngestor(),
        extractor_provider=_FailingProvider(), dashboard=dash)
    with pytest.raises(ExtractionError):
        await handler({"document_id": "job-fail", "document_ref": str(f),
                       "domain_code": "general"})
    assert len(dash.rows) == 1
    assert dash.rows[0].source == "job"
    assert dash.rows[0].truncated_chunks >= 1
    assert dash.rows[0].review_passed is False


# 8 个块：c1 校验返工；c3 首次 length 后救回；其余一次过
_SCRIPT = {
    1: [(_BAD, {"finish_reason": "stop", "input_tokens": 100,
                "output_tokens": 30, "prefill_s": 0.11, "decode_s": 0.21}),
        (_GOOD, {"finish_reason": "stop", "input_tokens": 130,
                 "output_tokens": 25, "prefill_s": 0.12, "decode_s": 0.22})],
    3: [(_BAD, {"finish_reason": "length", "input_tokens": 99,
                "output_tokens": 200, "prefill_s": 0.14, "decode_s": 0.50}),
        (_GOOD, {"finish_reason": "stop", "input_tokens": 120,
                 "output_tokens": 24, "prefill_s": 0.15, "decode_s": 0.24,
                 "cache_input_tokens": 40})],
}


def _script_for(idx: int):
    if idx in _SCRIPT:
        return list(_SCRIPT[idx])
    return [(_GOOD, {"finish_reason": "stop", "input_tokens": 100 + idx,
                     "output_tokens": 20, "prefill_s": 0.10,
                     "decode_s": 0.20})]


class _ChunkScriptExtractor(Extractor):
    def _build_agent(self):
        return SimpleNamespace()

    async def _call_structured(self, agent, content):
        idx = int(self._doc_id.rsplit("#c", 1)[1])
        reply, attrs = self._script[idx].pop(0)
        return SimpleNamespace(
            structured_output=reply,
            usage=SimpleNamespace(
                input_tokens=attrs["input_tokens"],
                output_tokens=attrs["output_tokens"],
                cache_input_tokens=attrs.get("cache_input_tokens", 0)),
            finish_reason=attrs["finish_reason"],
            timing=SimpleNamespace(
                prefill_s=attrs["prefill_s"], decode_s=attrs["decode_s"]))

    async def extract(self, document):
        idx = int(document.id.rsplit("#c", 1)[1])
        self._doc_id = document.id
        self._script = {idx: _script_for(idx)}
        self.max_retries = 1
        return await super().extract(document)


class _ChunkScriptProvider:
    async def get_extractor(self, domain_code):
        return (_ChunkScriptExtractor("general", max_retries=1),
                SimpleNamespace(code="general"))

    async def extract(self, domain_code, usage_sink=None, metrics_sink=None):
        async def _fn(text, feedback=None):
            raise AssertionError("分块路径不应调用闭包 extractor")

        return _fn


@pytest.mark.asyncio
async def test_api_and_batch_handler_share_same_chunk_field_set_offline(
        monkeypatch, tmp_path):
    """F10：不依赖 DB，也在 API 传输层与作业 handler 两条真实入口验证字段一致。"""
    from httpx import ASGITransport, AsyncClient

    monkeypatch.setenv("MORAY_CHUNK_THRESHOLD_UNITS", "10")
    monkeypatch.setenv("MORAY_CHUNK_MAX_UNITS", "40")
    monkeypatch.setenv("MORAY_CHUNK_OVERLAP_UNITS", "0")
    monkeypatch.setenv("MORAY_CHUNK_CONCURRENCY", "1")
    provider = _ChunkScriptProvider()
    api_dash = _FakeDashboard()
    deps = AppDeps(pool=None, store=None, kb=None, ingestor=_FakeIngestor(),
                   dashboard=api_dash, extractor_provider=provider)
    app = create_app(deps)
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://t") as ac:
        r = await ac.post("/extract", json={"text": LONG_TEXT,
                                            "document_id": "api-offline"})
    assert r.status_code == 200, r.text
    api_cm = r.json()["chunk_metrics"]

    f = tmp_path / "long.txt"
    f.write_text(LONG_TEXT, encoding="utf-8")
    job_dash = _FakeDashboard()
    handler = await build_document_handler(
        store=None, ingestor=_FakeIngestor(), extractor_provider=provider,
        dashboard=job_dash, model_name="test-batch")
    job_result = await handler({
        "document_id": "job-offline", "document_ref": str(f),
        "domain_code": "general"})
    job_cm = job_result["chunk_metrics"]

    assert set(api_cm["summary"]) == set(job_cm["summary"])
    assert set(api_cm["chunks"][0]) == set(job_cm["chunks"][0])
    assert api_cm["summary"]["chunk_calls"] == 8
    assert api_cm["summary"]["total_attempts"] == 10
    assert api_cm["summary"]["truncation_signals"] == 1
    # wait_s 是真实排队计时，两个进程入口不可能逐字相同；其余字段必须一致。
    api_summary = {k: v for k, v in api_cm["summary"].items() if k != "wait_s"}
    job_summary = {k: v for k, v in job_cm["summary"].items() if k != "wait_s"}
    assert api_summary == job_summary

    assert api_dash.rows[0].source == "api"
    assert job_dash.rows[0].source == "job"
    assert api_dash.rows[0].truncation_signals == 1
    assert job_dash.rows[0].truncation_signals == 1


