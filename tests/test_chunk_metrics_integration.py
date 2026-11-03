"""工单 01 验收：实时 API 路径与批量作业路径产出同一套分块度量字段。

需要 moray-pgvector；无 DB 自动 skip：
    RUN_DB_TESTS=1 .venv/bin/python -m pytest tests/test_chunk_metrics_integration.py

两条路径都走 run_chunked_team(provider=...) 的分块分支，假 provider 注入
脚本化的模型回复（确定性、不联网）；最后比对：
  · /extract 响应的 chunk_metrics 与作业结果的 chunk_metrics 字段集一致；
  · 两条路径都把同名列写进 pipeline_runs（看板数据面）。
"""

import os
from types import SimpleNamespace

import pytest

from moray.api.app import AppDeps, create_app
from moray.jobs import JobStore
from moray.jobs.handlers import build_document_handler
from moray.kb import EntityStore, Ingestor, KnowledgeBase, close_pool, init_pool
from moray.observability import Dashboard
from moray.extraction import Chunker, Document, Extractor

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DB_TESTS") != "1",
    reason="需要 PostgreSQL；设置 RUN_DB_TESTS=1 且启动 moray-pgvector 后运行",
)

# 足够长：保证按 40 单位切块得到 4 块且超过分块阈值
LONG_TEXT = "甲公司与乙公司在上海签约共建数据中心，推动产业协同发展。" * 8

_GOOD = {"entities": [], "relations": []}
_BAD = {"entities": [{"name": "x", "type": "NOT_A_TYPE"}], "relations": []}

# 8 个块的脚本：c1 校验返工一次；c3 首次 length 截断后返工救回；其余一次过
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


class _ScriptedExtractor(Extractor):
    """按块 id（doc#c000X）取脚本；X 块的回复序列由 _SCRIPT 决定。"""

    def _build_agent(self):
        return SimpleNamespace()

    async def _call_structured(self, agent, content):
        idx = int(self._doc_id.rsplit("#c", 1)[1])
        item = self._script[idx].pop(0)
        reply, attrs = item
        return SimpleNamespace(
            structured_output=reply,
            usage=SimpleNamespace(
                input_tokens=attrs["input_tokens"],
                output_tokens=attrs["output_tokens"],
                cache_input_tokens=attrs.get("cache_input_tokens", 0)),
            finish_reason=attrs["finish_reason"],
            timing=SimpleNamespace(
                prefill_s=attrs["prefill_s"], decode_s=attrs["decode_s"]))

    async def extract(self, document):  # type: ignore[override]
        # pipeline 复用同一个 Extractor 实例，每次 extract 按块 id 重置该块脚本
        idx = int(document.id.rsplit("#c", 1)[1])
        self._doc_id = document.id
        self._script = {idx: _script_for(idx)}
        self.max_retries = 1
        # warmup 探测走的文档 id 是「（预热）」而非分块 id，直接给一次成功返回
        if "#c" not in document.id:
            self._doc_id = "x#c0000"
            self._script = {0: _script_for(0)}
        return await super().extract(document)


class _ScriptedProvider:
    """镜像真实 DynamicExtractorProvider：get_extractor 给 pipeline 用，
    extract() 返回闭包（API/handler 挂 usage_sink 的契约）。"""

    @staticmethod
    def _new():
        return _ScriptedExtractor("general", max_retries=1)

    async def get_extractor(self, domain_code):
        return self._new(), SimpleNamespace(code="general")

    async def extract(self, domain_code, usage_sink=None, metrics_sink=None):
        extractor = self._new()

        async def _fn(text, feedback=None):
            out = await extractor.extract(
                Document(id="closure", text=text or "",
                         source="test", domain_code=domain_code))
            if usage_sink is not None:
                usage_sink.add(out.usage)
            if metrics_sink is not None and out.call_metrics is not None:
                metrics_sink.append(out.call_metrics)
            return out.result

        return _fn


# 两条路径必须逐字一致的摘要/明细字段集
SUMMARY_KEYS = {
    "document_id", "n_chunks", "chunk_calls", "total_attempts",
    "truncated_chunks", "truncation_signals", "rework_rate",
    "reworked_chunks", "wait_s", "latency_s", "prefill_s", "decode_s",
    "input_tokens", "output_tokens", "cached_input_tokens",
}
CHUNK_KEYS = {
    "chunk_id", "attempts", "finish_reason", "truncated",
    "input_tokens", "output_tokens", "cached_input_tokens",
    "wait_s", "latency_s", "prefill_s", "decode_s", "reworked",
    "truncated_attempts", "attempt_finish_reasons", "error",
}


class _FakeEmbedder:
    dim = 32

    def embed(self, texts):
        import numpy as np
        single = isinstance(texts, str)
        items = [texts] if single else texts
        rng = np.random.default_rng(0)
        out = rng.normal(size=(len(items), self.dim)).astype(np.float32)
        return out[0] if single else out

    def embed_one(self, text):
        return self.embed(text).tolist()


@pytest.fixture()
async def deps():
    pool = await init_pool()
    store = EntityStore(pool)
    await store.init_schema()
    await pool.execute("TRUNCATE entities RESTART IDENTITY CASCADE")
    await pool.execute("TRUNCATE extraction_jobs RESTART IDENTITY")
    emb = _FakeEmbedder()
    kb = KnowledgeBase(store, emb)
    ingestor = Ingestor(store, emb)
    jobs = JobStore(pool)
    await jobs.init_schema()
    dash = Dashboard(pool)
    await dash.init_schema()
    await pool.execute("TRUNCATE pipeline_runs RESTART IDENTITY")
    d = AppDeps(pool=pool, store=store, kb=kb, ingestor=ingestor, job_store=jobs,
                dashboard=dash, extractor_provider=_ScriptedProvider())
    yield d
    await close_pool(pool)


@pytest.mark.asyncio
async def test_api_path_emits_chunk_metrics_and_dashboard_row(deps, monkeypatch):
    """实时 API：响应带 chunk_metrics，且同一套字段落进 pipeline_runs。"""
    from httpx import ASGITransport, AsyncClient

    # run_chunked_team 在 app.py 内被调用时使用 config.chunk_settings，
    # 这里通过环境覆盖让两条路径都强制走分块（与 _force kwargs 等价）
    monkeypatch.setenv("MORAY_CHUNK_THRESHOLD_UNITS", "10")
    monkeypatch.setenv("MORAY_CHUNK_MAX_UNITS", "40")
    monkeypatch.setenv("MORAY_CHUNK_OVERLAP_UNITS", "0")
    monkeypatch.setenv("MORAY_CHUNK_CONCURRENCY", "1")
    app = create_app(deps)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.post("/extract", json={"text": LONG_TEXT, "document_id": "api1"})
    assert r.status_code == 200, r.text
    body = r.json()
    cm = body["chunk_metrics"]
    assert cm is not None
    assert set(cm["summary"].keys()) == SUMMARY_KEYS
    assert len(cm["chunks"]) == 8
    for c in cm["chunks"]:
        assert set(c.keys()) == CHUNK_KEYS
    s = cm["summary"]
    assert s["chunk_calls"] == 8
    assert s["total_attempts"] == 10
    assert s["reworked_chunks"] == 2
    # c3 第一次 length 但返工救回 -> 最终 truncated=0、信号=1
    assert s["truncated_chunks"] == 0
    assert s["truncation_signals"] == 1
    assert s["cached_input_tokens"] == 40
    assert s["prefill_s"] > 0 and s["decode_s"] > 0

    # 看板行（API 路径也落数据面）
    row = await deps.pool.fetchrow(
        "SELECT chunk_calls, truncated_chunks, truncation_signals, "
        "reworked_chunks, total_attempts, prefill_ms, decode_ms, chunk_metrics "
        "FROM pipeline_runs WHERE document_id=$1", "api1")
    assert row["chunk_calls"] == 8
    assert row["truncation_signals"] == 1
    assert row["reworked_chunks"] == 2
    assert row["total_attempts"] == 10
    assert row["prefill_ms"] > 0 and row["decode_ms"] > 0
    assert len(row["chunk_metrics"]) == 8

    # 看板聚合端点也暴露新字段
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r2 = await ac.get("/metrics/dashboard")
    agg = r2.json()
    assert agg["chunk_calls"] == 8
    assert agg["truncation_signals"] == 1
    assert agg["chunk_rework_rate"] == 0.25


@pytest.mark.asyncio
async def test_batch_handler_emits_identical_chunk_metrics_fields(deps, tmp_path):
    """批量作业：handler 结果与看板行的分块字段和 API 路径同一套。"""
    f = tmp_path / "long.txt"
    f.write_text(LONG_TEXT, encoding="utf-8")
    job = {"document_id": "job1", "document_ref": str(f), "domain_code": "general"}

    import moray.config as _cfg
    # 直接用小预算构造 handler 等价：handler 内部读 chunk_settings()，用 monkeypatch
    _orig = _cfg.chunk_settings

    def _small():
        return {"threshold_units": 10, "max_units": 40,
                "overlap_units": 0, "concurrency": 1}

    _cfg.chunk_settings = _small  # type: ignore
    try:
        handler = await build_document_handler(
            store=deps.store, ingestor=deps.ingestor,
            extractor_provider=_ScriptedProvider(),
            dashboard=deps.dashboard, model_name="test-batch")
        result = await handler(job)
    finally:
        _cfg.chunk_settings = _orig  # type: ignore

    cm = result["chunk_metrics"]
    assert cm is not None
    assert set(cm["summary"].keys()) == SUMMARY_KEYS
    assert len(cm["chunks"]) == 8
    for c in cm["chunks"]:
        assert set(c.keys()) == CHUNK_KEYS
    s = cm["summary"]
    assert s["chunk_calls"] == 8 and s["total_attempts"] == 10
    assert s["truncation_signals"] == 1 and s["truncated_chunks"] == 0
    assert s["reworked_chunks"] == 2

    row = await deps.pool.fetchrow(
        "SELECT chunk_calls, truncation_signals, reworked_chunks, total_attempts "
        "FROM pipeline_runs WHERE document_id=$1", "job1")
    assert (row["chunk_calls"], row["truncation_signals"],
            row["reworked_chunks"], row["total_attempts"]) == (8, 1, 2, 10)
