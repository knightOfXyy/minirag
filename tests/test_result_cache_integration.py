"""分块结果内容级缓存 × 断点续跑/失败重试 的 DB 集成测试（需要 moray-pgvector）。

复用作业基建（JobStore + BatchRunner + build_document_handler + Ingestor），
验证：作业失败回 pending 重跑、同一文档重复提交时，已完成分块命中缓存、不再调
LLM；命中用量只记 cache_hits，不产生真实 token。

    RUN_DB_TESTS=1 .venv/bin/python -m pytest tests/test_result_cache_integration.py -q
"""

import asyncio
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
    # 只清数据、不删表：DROP TABLE 会破坏同库其它使用者（N8）。
    await backend.init_schema()
    await pool.execute("TRUNCATE extraction_result_cache")
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


@pytest.mark.asyncio
async def test_postgres_two_cache_instances_singleflight_one_real_call(env):
    """两个缓存实例（模拟两个 worker 进程）并发同键：只发生一次真实抽取（S1）。

    跨进程共享同一个 postgres 后端时，先 INSERT 占位的那一方是 leader，另一方
    轮询等它的 payload，而不是各自再调一次模型。
    """
    import asyncio

    from moray.extraction.cache import PostgresCacheBackend

    pool, *_ = env
    backend_a = PostgresCacheBackend(pool)
    backend_b = PostgresCacheBackend(pool)
    await backend_a.init_schema()
    await pool.execute("TRUNCATE extraction_result_cache")
    # 两个实例各有自己的进程内 _inflight；只共享后端。
    cache_a = ContentResultCache(backend_a)
    cache_b = ContentResultCache(backend_b)
    calls = {"n": 0}

    async def extract():
        calls["n"] += 1
        await asyncio.sleep(0.05)      # 模拟真实模型耗时，让另一个实例先进入等待
        return await _sample_output()

    results = await asyncio.gather(
        cache_a.get_or_extract("fp", "m", "跨进程同块", extract),
        cache_b.get_or_extract("fp", "m", "跨进程同块", extract),
    )
    assert calls["n"] == 1
    assert sorted(hit for _, hit in results) == [False, True]
    assert results[0][0].result.model_dump() == results[1][0].result.model_dump()


@pytest.mark.asyncio
async def test_postgres_stale_claim_taken_over_after_lease(env):
    """PG 占位行租约过期后等待方可接管；未过期时抢不到（S2）。"""
    from moray.extraction.cache import (
        ContentResultCache,
        PostgresCacheBackend,
        build_cache_key,
    )

    pool, *_ = env
    backend = PostgresCacheBackend(pool)
    await backend.init_schema()
    await pool.execute("TRUNCATE extraction_result_cache")
    key = build_cache_key("fp", "m", "PG 占位残留")
    assert await backend.try_claim(key, 0.05) is True
    assert await backend.try_claim(key, 0.05) is False   # 租约有效，不能被抢
    await asyncio.sleep(0.1)                              # 等租约过期

    cache = ContentResultCache(
        backend, remote_wait_seconds=5.0,
        poll_seconds=0.02, claim_lease_seconds=0.05)
    calls = {"n": 0}

    async def extract():
        calls["n"] += 1
        return await _sample_output()

    loop = asyncio.get_running_loop()
    started = loop.time()
    out, hit = await cache.get_or_extract("fp", "m", "PG 占位残留", extract)
    elapsed = loop.time() - started
    assert hit is False
    assert calls["n"] == 1                 # 只多一次真实调用
    assert elapsed < 1.0                   # 一个租约内接管，不等满 5s
    assert out.result.model_dump() == (await _sample_output()).result.model_dump()


@pytest.mark.asyncio
async def test_postgres_slow_healthy_leader_renews_lease_not_stolen(env):
    """健康但慢的 PG leader 持续续租：等待方不抢占、只一次真调（S2）。"""
    from moray.extraction.cache import (
        ContentResultCache,
        PostgresCacheBackend,
    )

    pool, *_ = env
    backend = PostgresCacheBackend(pool)
    await backend.init_schema()
    await pool.execute("TRUNCATE extraction_result_cache")
    cache_a = ContentResultCache(backend, claim_lease_seconds=0.2)
    cache_b = ContentResultCache(
        backend, claim_lease_seconds=0.2,
        poll_seconds=0.02, remote_wait_seconds=5.0)
    calls = {"n": 0}

    async def slow():
        calls["n"] += 1
        await asyncio.sleep(0.6)           # 超过两个租约
        return await _sample_output()

    leader = asyncio.create_task(
        cache_a.get_or_extract("fp", "m", "PG 慢 leader", slow))
    await asyncio.sleep(0.02)
    out, hit = await cache_b.get_or_extract("fp", "m", "PG 慢 leader", slow)
    leader_out, _ = await leader
    assert calls["n"] == 1
    assert hit is True
    assert out.result.model_dump() == leader_out.result.model_dump()


class _FailAfter:
    """前 k 次真实抽取成功并写缓存，之后每次都失败（模拟作业跑到一半挂掉）。"""

    def __init__(self, domain, fail_after: int):
        self.domain = domain
        self.model_name = "fake-model"
        self.fail_after = fail_after
        self.calls = 0

    async def extract(self, document):
        from moray.extraction.document import ExtractionOutput, Usage
        from moray.schema import Entity, EntityMention, ExtractionResult, Span

        self.calls += 1
        if self.calls > self.fail_after:
            raise RuntimeError("模拟第 k 块之后模型不可用")
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


@pytest.mark.asyncio
async def test_job_fails_midway_then_restarts_with_new_pg_cache_hits_done_chunks(
        env, monkeypatch):
    """作业真跑到一半失败 → 用同一 pool 新建缓存实例（模拟 worker 重启）重跑：
    已完成块命中、未完成块真抽，不用手工预热（S2）。"""
    pool, store, jobs, ingestor, doc_path = env
    monkeypatch.setenv("MORAY_CHUNK_CONCURRENCY", "1")   # 顺序执行，失败点确定

    from moray.config import chunk_settings
    from moray.extraction.cache import PostgresCacheBackend
    from moray.extraction.chunking import Chunker
    from moray.schema import get_domain

    domain = get_domain("general")
    cs = chunk_settings()
    chunks = Chunker(
        max_units=cs["max_units"], overlap_units=cs["overlap_units"]
    ).chunk_text(Path(doc_path).read_text(encoding="utf-8"))
    assert len(chunks) >= 4

    pg = PostgresCacheBackend(pool)
    await pg.init_schema()
    await pool.execute("TRUNCATE extraction_result_cache")
    cache1 = ContentResultCache(pg)
    done_chunks = 2

    failing = _FailAfter(domain, fail_after=done_chunks)
    handler1 = await _make_handler(ingestor, _StaticProvider(failing), cache1)
    await jobs.create_job(job_id="j-fail", document_id="d-fail",
                          document_ref=doc_path, domain_code="general")
    s1 = await BatchRunner(jobs, handler1, max_attempts=1).run_once(recover=False)
    assert s1.failed == 1

    # 模拟 worker 重启：同一个 postgres pool，全新 cache 实例（进程内 _inflight 为空）
    cache2 = ContentResultCache(PostgresCacheBackend(pool))
    retry = _CountingExtractor(domain)
    handler2 = await _make_handler(ingestor, _StaticProvider(retry), cache2)
    await jobs.reset_failed()
    s2 = await BatchRunner(jobs, handler2, max_attempts=1).run_once(recover=False)
    assert s2.succeeded == 1

    assert retry.calls == len(chunks) - done_chunks
    summary = (await jobs.list_jobs(JobStatus.SUCCEEDED))[0]["result_summary"]
    assert summary["usage"]["cache_hits"] == done_chunks
    assert summary["usage"]["input_tokens"] == 3 * retry.calls


@pytest.mark.asyncio
async def test_postgres_ttl_purges_expired_rows(env):
    """PG 后端按 TTL 清理旧行，长跑不会无限增长（N6）。"""
    from moray.extraction.cache import PostgresCacheBackend

    pool, *_ = env
    # purge_interval_seconds=0 让每次写入都清理，便于验证 TTL 语义本身（N7）。
    backend = PostgresCacheBackend(
        pool, ttl_hours=1, purge_interval_seconds=0)
    await backend.init_schema()
    await pool.execute("TRUNCATE extraction_result_cache")
    await backend.set("k-old", '{"v":1}')
    await pool.execute(
        "UPDATE extraction_result_cache SET created_at = now() - interval '2 hours' "
        "WHERE cache_key='k-old'")
    await backend.set("k-new", '{"v":1}')       # 写入时顺带清理过期行
    assert await backend.get("k-old") is None
    assert await backend.get("k-new") == '{"v":1}'


