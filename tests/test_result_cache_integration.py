"""分块结果内容级缓存 × 断点续跑/失败重试 的 DB 集成测试（需要 moray-pgvector）。

复用作业基建（JobStore + BatchRunner + build_document_handler + Ingestor），
验证：作业失败回 pending 重跑、同一文档重复提交时，已完成分块命中缓存、不再调
LLM；命中用量只记 cache_hits，不产生真实 token。

    RUN_DB_TESTS=1 .venv/bin/python -m pytest tests/test_result_cache_integration.py -q
"""

import os
from pathlib import Path

import pytest

from moray.extraction.cache import ContentResultCache, InMemoryCacheBackend
from moray.jobs import BatchRunner, JobStatus, JobStore
from moray.jobs.handlers import build_document_handler
from moray.kb import EntityStore, Ingestor, close_pool, init_pool

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DB_TESTS") != "1",
    reason="需要 PostgreSQL；设置 RUN_DB_TESTS=1 且启动 moray-pgvector 后运行",
)


class _CountingExtractor:
    """满足 pipeline 契约；对每个不同块正文计一次“真实调用”。"""

    def __init__(self, domain):
        self.domain = domain
        self.model_name = "fake-model"
        self.calls = 0
        self.seen: set[str] = set()

    async def extract(self, document):
        from moray.extraction.document import ExtractionOutput, Usage
        from moray.schema import Entity, EntityMention, ExtractionResult, Span

        self.calls += 1
        self.seen.add(document.text)
        name = document.text[:6]
        n = len(name)
        res = ExtractionResult(entities=[Entity(
            name=name, type="ORG",
            mentions=[EntityMention(surface=name,
                                    span=Span(start=0, end=n, text=name))])])
        return ExtractionOutput(
            document_id=document.id, domain_code=self.domain.code,
            result=res, model_name=self.model_name,
            usage=Usage(input_tokens=3, output_tokens=2), attempts=1)


class _StaticProvider:
    """像 DynamicExtractorProvider，但每次返回同一个可计数抽取器。"""

    def __init__(self, real):
        self._real = real

    async def get_extractor(self, domain_code):
        return self._real, self._real.domain

    async def extract(self, domain_code, usage_sink=None, backfill_sink=None):
        from moray.extraction.document import Document

        real, _ = await self.get_extractor(domain_code)

        async def _fn(text, feedback=None):
            out = await real.extract(
                Document(id=f"{domain_code}-dyn", text=text,
                         source="dynamic", domain_code=domain_code))
            if usage_sink is not None:
                usage_sink.add(out.usage)
            if backfill_sink is not None:
                backfill_sink["backfill"] = out.backfill
            return out.result

        return _fn


def _long_text() -> str:
    # 足够超过测试里强制的小阈值，切成多块
    return "".join(f"第{n}段记载某某独特事件内容{n}。" for n in range(1, 60))


@pytest.fixture()
async def env(tmp_path, monkeypatch):
    # 强制走分块路径（handler 从 env 读分块设置）
    monkeypatch.setenv("MORAY_CHUNK_THRESHOLD_UNITS", "20")
    monkeypatch.setenv("MORAY_CHUNK_MAX_UNITS", "40")
    monkeypatch.setenv("MORAY_CHUNK_OVERLAP_UNITS", "0")
    monkeypatch.setenv("MORAY_CHUNK_CONCURRENCY", "2")
    pool = await init_pool()
    store = EntityStore(pool)
    await store.init_schema()
    jobs = JobStore(pool)
    await jobs.init_schema()
    await pool.execute("TRUNCATE extraction_jobs RESTART IDENTITY")
    ingestor = Ingestor(store, embedder=object())  # 本路径不真正算向量也能入实体
    doc = tmp_path / "long.txt"
    doc.write_text(_long_text(), encoding="utf-8")
    try:
        yield pool, store, jobs, ingestor, str(doc)
    finally:
        await close_pool(pool)


async def _make_handler(ingestor, provider, cache):
    # embedder 占位对象没有 embed_one；本测试关心缓存/作业语义，给 Ingestor 换
    # 一个确定性假 embedder，避免拉本地 ONNX 模型。
    class _FakeEmb:
        dim = 512

        def embed_one(self, text):
            base = (text or "x").encode("utf-8")
            return [float(base[i % len(base)]) / 255.0 for i in range(512)]

    ingestor.embedder = _FakeEmb()
    return await build_document_handler(
        store=ingestor.store, ingestor=ingestor,
        extractor_provider=provider, source="it",
        model_name="fake-model", result_cache=cache)


@pytest.mark.asyncio
async def test_retry_rerun_completed_chunks_hit_cache(env):
    _pool, store, jobs, ingestor, doc_path = env
    text = Path(doc_path).read_text(encoding="utf-8")

    # 领域用内置 general（resolver 回退常量，版本 0.0）
    from moray.schema import get_domain

    real = _CountingExtractor(get_domain("general"))
    provider = _StaticProvider(real)
    cache = ContentResultCache(InMemoryCacheBackend())
    handler = await _make_handler(ingestor, provider, cache)

    await jobs.create_job(job_id="j1", document_id="d1",
                          document_ref=doc_path, domain_code="general")

    # 第一次：所有分块真实抽取
    s1 = await BatchRunner(jobs, handler, max_attempts=3).run_once(recover=False)
    assert s1.succeeded == 1
    n_chunks = real.calls
    assert n_chunks >= 2
    summary1 = (await jobs.list_jobs(JobStatus.SUCCEEDED))[0]["result_summary"]
    assert summary1["usage"]["cache_hits"] == 0
    assert summary1["usage"]["input_tokens"] == 3 * n_chunks

    # 同一文档重复提交（幂等创建只拿到旧行；这里手工重置模拟重跑/续跑）
    await jobs.reset_failed()
    await jobs.pool.execute(
        "UPDATE extraction_jobs SET status='pending' WHERE job_id='j1'")
    s2 = await BatchRunner(jobs, handler, max_attempts=3).run_once(recover=False)
    assert s2.succeeded == 1
    assert real.calls == n_chunks                 # 没有任何新的真实模型调用
    summary2 = (await jobs.list_jobs(JobStatus.SUCCEEDED))[0]["result_summary"]
    assert summary2["usage"]["cache_hits"] == n_chunks
    # 命中不计真实 token
    assert summary2["usage"]["input_tokens"] == 0
    assert summary2["usage"]["output_tokens"] == 0
    # 最终实体数与首跑一致（结果语义不变）
    assert summary2["entities_written"] == summary1["entities_written"]


@pytest.mark.asyncio
async def test_failed_then_retry_mixes_uncached_and_cached(env):
    """首轮处理到一半失败（缓存里已有部分块）→ 重试：已完成块命中、其余真抽。"""
    _pool, store, jobs, ingestor, doc_path = env

    from moray.schema import domain_fingerprint, get_domain

    domain = get_domain("general")
    real = _CountingExtractor(domain)
    real.model_name = "fake-model"      # 与预热键一致（生产里模型名本就固定）
    provider = _StaticProvider(real)
    cache = ContentResultCache(InMemoryCacheBackend())

    # 用“上一轮”的独立抽取器预热一半的块，模拟失败前已完成并写缓存的分块。
    # 分块参数必须与 handler 实际从 env 读取的一致（见 env fixture），否则块正文
    # 对不上会全部 miss。
    from moray.config import chunk_settings as _chunk_settings
    from moray.extraction.chunking import Chunker

    _cs = _chunk_settings()
    chunks = Chunker(
        max_units=_cs["max_units"], overlap_units=_cs["overlap_units"]
    ).chunk_text(Path(doc_path).read_text(encoding="utf-8"))
    prior = _CountingExtractor(domain)
    half = len(chunks) // 2
    for c in chunks[:half]:
        await cache.get_or_extract(
            domain_fingerprint(domain), "fake-model", c.text,
            lambda c=c: _uncached_call(prior, c))
    assert prior.calls == half

    # 本轮真正使用的抽取器从零开始计数
    handler = await _make_handler(ingestor, provider, cache)
    await jobs.create_job(job_id="j2", document_id="d2",
                          document_ref=doc_path, domain_code="general")
    s = await BatchRunner(jobs, handler, max_attempts=3).run_once(recover=False)
    assert s.succeeded == 1
    # 已缓存的一半不调模型，剩余块真实抽取
    assert real.calls == len(chunks) - half
    summary = (await jobs.list_jobs(JobStatus.SUCCEEDED))[0]["result_summary"]
    assert summary["usage"]["cache_hits"] == half
    assert summary["usage"]["input_tokens"] == 3 * real.calls


async def _uncached_call(real, chunk):
    from moray.extraction.document import Document

    return await real.extract(
        Document(id=chunk.id, text=chunk.text, source="chunk",
                 domain_code="general"))


async def _sample_output(text="同一段块正文"):
    from moray.extraction.document import ExtractionOutput, Usage
    from moray.schema import Entity, EntityMention, ExtractionResult, Span

    name = text[:6]
    n = len(name)
    return ExtractionOutput(
        document_id="chunk", domain_code="general",
        result=ExtractionResult(entities=[Entity(
            name=name, type="ORG",
            mentions=[EntityMention(surface=name,
                                    span=Span(start=0, end=n, text=name))])]),
        model_name="m", usage=Usage(input_tokens=1, output_tokens=1), attempts=1)


# ---------------------------------------------------------------------------
# PostgreSQL 后端：复用作业库同一连接池，跨 worker 进程共享缓存内容
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_postgres_backend_roundtrip_and_corruption_degrades(env):
    import asyncio

    from moray.extraction.cache import (
        PostgresCacheBackend,
        build_cache_key,
    )

    pool, _store, _jobs, _ingestor, _doc = env
    backend = PostgresCacheBackend(pool)
    await pool.execute("DROP TABLE IF EXISTS extraction_result_cache")
    await backend.init_schema()
    cache = ContentResultCache(backend)
    calls = {"n": 0}

    async def extract():
        calls["n"] += 1
        return await _sample_output()

    o1, h1 = await cache.get_or_extract("fp", "m", "同一段块正文", extract)
    o2, h2 = await cache.get_or_extract("fp", "m", "同一段块正文", extract)
    assert (h1, h2) == (False, True)
    assert calls["n"] == 1
    assert o1.result.model_dump() == o2.result.model_dump()

    # 直接把载荷改坏：内容缓存按未命中降级，不抛异常、结果仍可得
    await pool.execute(
        "UPDATE extraction_result_cache SET payload=$2 WHERE cache_key=$1",
        build_cache_key("fp", "m", "坏数据"), "{bad")
    # 先写一条合法记录，再改成损坏
    o3, _ = await cache.get_or_extract(
        "fp", "m", "坏数据", lambda: asyncio.sleep(0, result=o1))
    await pool.execute(
        "UPDATE extraction_result_cache SET payload=$2 WHERE cache_key=$1",
        build_cache_key("fp", "m", "坏数据"), "{bad")
    calls_before = calls["n"]
    o4, hit = await cache.get_or_extract(
        "fp", "m", "坏数据", extract)
    assert hit is False
    assert calls["n"] == calls_before + 1
    assert o4.result.model_dump() == o1.result.model_dump()


