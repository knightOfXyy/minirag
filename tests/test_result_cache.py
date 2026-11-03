"""分块抽取结果内容级缓存的单测（离线、确定性、不联网）。

缓存是纯加速层：键覆盖领域配置指纹、模型名、分块正文；命中不调模型、
不把命中计成真实 token；同进程并发同键只抽一次（单飞）；后端损坏自动降级。
"""

import asyncio

import pytest

from moray.extraction.cache import (
    ContentResultCache,
    InMemoryCacheBackend,
    build_cache_key,
)
from moray.extraction.document import ExtractionOutput, Usage
from moray.schema import Entity, EntityMention, ExtractionResult, Span


def _output(name: str = "华为", *, inp: int = 10, out: int = 4) -> ExtractionOutput:
    return ExtractionOutput(
        document_id="chunk",
        domain_code="general",
        result=ExtractionResult(
            entities=[
                Entity(
                    name=name,
                    type="ORG",
                    mentions=[
                        EntityMention(surface=name, span=Span(start=0, end=len(name), text=name))
                    ],
                )
            ]
        ),
        model_name="fake-model",
        usage=Usage(input_tokens=inp, output_tokens=out),
        attempts=1,
    )


# ---------------------------------------------------------------------------
# 键：内容/领域指纹/模型 任一变化即换键
# ---------------------------------------------------------------------------
def test_key_changes_when_content_changes():
    k1 = build_cache_key("fp1", "model-a", "正文甲")
    k2 = build_cache_key("fp1", "model-a", "正文乙")
    assert k1 != k2


def test_key_changes_when_domain_fingerprint_changes():
    k1 = build_cache_key("fp1", "model-a", "同一段正文")
    k2 = build_cache_key("fp2", "model-a", "同一段正文")
    assert k1 != k2


def test_key_changes_when_model_changes():
    k1 = build_cache_key("fp1", "model-a", "同一段正文")
    k2 = build_cache_key("fp1", "model-b", "同一段正文")
    assert k1 != k2


def test_key_stable_for_identical_inputs():
    assert (
        build_cache_key("fp", "m", "正文")
        == build_cache_key("fp", "m", "正文")
    )


def test_prompt_template_version_is_part_of_key(monkeypatch):
    """提示模板/生成参数版本变化也要换键（N11）：改 _LEAD_IN 不失效的风险用版本号兜住。"""
    import moray.extraction.cache as cache_mod

    before = build_cache_key("fp", "m", "正文")
    monkeypatch.setattr(cache_mod, "_PROMPT_TEMPLATE_VERSION", 2)
    after = build_cache_key("fp", "m", "正文")
    assert before != after


# ---------------------------------------------------------------------------
# 命中/未命中
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_miss_calls_extractor_then_hit_does_not():
    cache = ContentResultCache(InMemoryCacheBackend())
    calls = {"n": 0}

    async def extract():
        calls["n"] += 1
        return _output()

    o1, hit1 = await cache.get_or_extract(
        domain_fingerprint="fp1", model_identity="m", chunk_text="正文",
        extract=extract)
    o2, hit2 = await cache.get_or_extract(
        domain_fingerprint="fp1", model_identity="m", chunk_text="正文",
        extract=extract)

    assert hit1 is False and hit2 is True
    assert calls["n"] == 1
    # 命中还原出逐字段一致的结果
    assert o1.result.model_dump() == o2.result.model_dump()
    assert o2.model_dump() == o1.model_dump()


@pytest.mark.asyncio
async def test_hit_returns_independent_copy():
    """命中返回的对象与缓存里的不是同一份：调用方改写不污染缓存。"""
    cache = ContentResultCache(InMemoryCacheBackend())

    async def extract():
        return _output()

    out, _ = await cache.get_or_extract(
        domain_fingerprint="fp", model_identity="m", chunk_text="正文",
        extract=extract)
    out.result.entities[0].name = "被改写"
    again, hit = await cache.get_or_extract(
        domain_fingerprint="fp", model_identity="m", chunk_text="正文",
        extract=extract)
    assert hit is True
    assert again.result.entities[0].name == "华为"


@pytest.mark.asyncio
async def test_content_change_invalidates_and_recalls_extractor():
    cache = ContentResultCache(InMemoryCacheBackend())
    calls = {"n": 0}

    async def extract():
        calls["n"] += 1
        return _output()

    await cache.get_or_extract("fp", "m", "正文一", extract)
    out, hit = await cache.get_or_extract("fp", "m", "正文二", extract)
    assert hit is False
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_domain_fingerprint_change_invalidates():
    cache = ContentResultCache(InMemoryCacheBackend())
    calls = {"n": 0}

    async def extract():
        calls["n"] += 1
        return _output()

    await cache.get_or_extract("fp-v1", "m", "同正文", extract)
    _, hit = await cache.get_or_extract("fp-v2", "m", "同正文", extract)
    assert hit is False
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_model_change_invalidates():
    cache = ContentResultCache(InMemoryCacheBackend())
    calls = {"n": 0}

    async def extract():
        calls["n"] += 1
        return _output()

    await cache.get_or_extract("fp", "model-a", "同正文", extract)
    _, hit = await cache.get_or_extract("fp", "model-b", "同正文", extract)
    assert hit is False
    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# 单飞：并发同键只抽一次
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_concurrent_same_key_extracts_once():
    cache = ContentResultCache(InMemoryCacheBackend())
    calls = {"n": 0}

    async def extract():
        calls["n"] += 1
        return _output()

    import asyncio

    results = await asyncio.gather(
        *[cache.get_or_extract(
            "fp", "m", "并发同正文", extract) for _ in range(8)])
    assert calls["n"] == 1
    assert [r.result.model_dump() for r, _ in results] == (
        [results[0][0].result.model_dump()] * 8)
    # 等待方拿到的是独立副本（N1）：改写一个不影响其它并发结果与缓存。
    assert results[1][0] is not results[0][0]
    results[1][0].result.entities[0].name = "被等待方改写"
    again, hit = await cache.get_or_extract(
        "fp", "m", "并发同正文", extract)
    assert hit is True
    assert again.result.entities[0].name == "华为"
    assert results[0][0].result.entities[0].name == "华为"


# ---------------------------------------------------------------------------
# 跨进程单飞：不依赖 PostgreSQL 的可抢占后端（S1，默认 CI 必跑）
# ---------------------------------------------------------------------------
class _ClaimableFakeBackend:
    """dict + 带租约的占位行，完整实现 ClaimableBackend 契约。

    内存后端不实现 try_claim（只在单进程内单飞）；这里补一个不依赖 PG 的
    等价物，让 try_claim / _wait_remote / release / renew_claim 这些新分支
    在默认 CI 里也有覆盖。
    """

    def __init__(self):
        self._store: dict[str, str] = {}
        # key -> (expires_at, owner)；owner 是 leader 令牌，旧 leader 不能拿它
        # 给新 leader 续租/删占位（should-1）。
        self._claims: dict[str, tuple[float, str]] = {}
        self.gets = 0
        self.claims = 0
        self.renews = 0
        self.releases = 0
        # >0 时下一次 renew_claim 抛错，模拟 DB 抖动（nit-3）。
        self.renew_errors = 0

    async def get(self, key):
        await asyncio.sleep(0)
        self.gets += 1
        if key in self._store:
            return self._store[key]
        if key in self._claims:
            return ""          # 空载荷 = leader 占位行
        return None

    async def set(self, key, value):
        await asyncio.sleep(0)
        self._store[key] = value
        self._claims.pop(key, None)

    async def try_claim(self, key, lease_seconds, owner):
        await asyncio.sleep(0)
        now = asyncio.get_running_loop().time()
        current = self._claims.get(key)
        if current is not None and current[0] > now:
            return False       # 占位仍有效：健康 leader 在续租
        self.claims += 1
        self._claims[key] = (now + lease_seconds, owner)
        return True

    async def renew_claim(self, key, lease_seconds, owner):
        await asyncio.sleep(0)
        if self.renew_errors:
            self.renew_errors -= 1
            raise RuntimeError("模拟一次续租 DB 抖动")
        now = asyncio.get_running_loop().time()
        current = self._claims.get(key)
        if current is None or current[0] <= now:
            return False
        if current[1] != owner:
            return False       # 旧 leader 不能给新 leader 续租
        self.renews += 1
        self._claims[key] = (now + lease_seconds, owner)
        return True

    async def release(self, key, owner):
        await asyncio.sleep(0)
        current = self._claims.get(key)
        if current is None or current[1] != owner:
            return             # 只有占位 owner 才能删，旧 leader 无权删新 leader
        self.releases += 1
        self._claims.pop(key, None)

    async def discard(self, key, expected_value):
        await asyncio.sleep(0)
        if self._store.get(key) == expected_value:
            self._store.pop(key, None)


def test_inmemory_backend_is_not_claimable():
    """内存后端只做进程内单飞，不冒充跨进程契约（N6）。"""
    assert not hasattr(InMemoryCacheBackend(), "try_claim")


@pytest.mark.asyncio
async def test_claimable_two_instances_concurrent_same_key_one_call():
    """两个缓存实例（模拟两个 worker）共享可抢占后端并发同键：只真抽一次（S1）。"""
    backend = _ClaimableFakeBackend()
    cache_a = ContentResultCache(backend)
    cache_b = ContentResultCache(backend)
    calls = {"n": 0}

    async def extract():
        calls["n"] += 1
        await asyncio.sleep(0.05)      # 模拟模型耗时，让另一个实例进入等待
        return _output()

    results = await asyncio.gather(
        cache_a.get_or_extract("fp", "m", "跨进程同块", extract),
        cache_b.get_or_extract("fp", "m", "跨进程同块", extract))
    assert calls["n"] == 1
    assert sorted(hit for _, hit in results) == [False, True]
    assert results[0][0].result.model_dump() == results[1][0].result.model_dump()


@pytest.mark.asyncio
async def test_claimable_leader_failure_hands_over_to_waiter():
    """leader 抛错并释放占位后，等待方接管并成功抽取（S1）。"""
    backend = _ClaimableFakeBackend()
    cache_a = ContentResultCache(backend)
    cache_b = ContentResultCache(backend, poll_seconds=0.01)
    calls = {"n": 0}
    started = asyncio.Event()

    async def failing():
        calls["n"] += 1
        started.set()
        await asyncio.sleep(0.05)
        raise RuntimeError("模拟 leader 崩溃")

    async def succeeding():
        calls["n"] += 1
        return _output()

    leader = asyncio.create_task(
        cache_a.get_or_extract("fp", "m", "leader 会挂", failing))
    await started.wait()
    waiter = asyncio.create_task(
        cache_b.get_or_extract("fp", "m", "leader 会挂", succeeding))
    await asyncio.sleep(0.01)            # 让等待方先进入轮询

    with pytest.raises(RuntimeError):
        await leader
    out, hit = await waiter
    assert hit is False                  # 等待方自己真抽，不是缓存命中
    assert out.result.entities[0].name == "华为"
    assert calls["n"] == 2
    assert backend.releases >= 1

    # 接管方回写后，第三个调用方应命中
    _, hit_again = await cache_b.get_or_extract(
        "fp", "m", "leader 会挂", succeeding)
    assert hit_again is True


@pytest.mark.asyncio
async def test_stale_claim_taken_over_after_lease_not_full_wait():
    """占位行残留（leader 崩溃未回写）→ 一个租约后接管，不等满等待窗口（S1/S2）。"""
    backend = _ClaimableFakeBackend()
    cache = ContentResultCache(
        backend, remote_wait_seconds=5.0,
        poll_seconds=0.01, claim_lease_seconds=0.05)
    key = build_cache_key("fp", "m", "占位残留")
    assert await backend.try_claim(key, 0.01, "dead-leader")   # 死掉的 leader，只占位不回写
    await asyncio.sleep(0.02)                   # 让租约过期
    calls = {"n": 0}

    async def extract():
        calls["n"] += 1
        return _output()

    loop = asyncio.get_running_loop()
    started = loop.time()
    out, hit = await cache.get_or_extract("fp", "m", "占位残留", extract)
    elapsed = loop.time() - started
    assert hit is False
    assert calls["n"] == 1                       # 只多这一次真实调用
    assert out.result.entities[0].name == "华为"
    assert elapsed < 1.0                         # 远小于 remote_wait_seconds=5


@pytest.mark.asyncio
async def test_slow_healthy_leader_renews_lease_and_is_not_duplicated():
    """慢但健康的 leader 持续续租：等待方不抢占，只发生一次真调；轮询指数退避（S2/N9）。"""
    backend = _ClaimableFakeBackend()
    cache_a = ContentResultCache(backend, claim_lease_seconds=0.2)
    cache_b = ContentResultCache(
        backend, claim_lease_seconds=0.2,
        poll_seconds=0.01, remote_wait_seconds=5.0)
    calls = {"n": 0}

    async def slow():
        calls["n"] += 1
        await asyncio.sleep(0.5)             # 超过两个租约
        return _output()

    leader = asyncio.create_task(
        cache_a.get_or_extract("fp", "m", "慢 leader", slow))
    await asyncio.sleep(0.02)                # 确保 leader 先抢到占位
    out, hit = await cache_b.get_or_extract("fp", "m", "慢 leader", slow)
    leader_out, _ = await leader

    assert calls["n"] == 1                   # 没有第二次真调
    assert hit is True
    assert backend.claims == 1               # 等待方从未抢到第二份占位
    assert backend.renews >= 2               # leader 确实在续租
    assert out.result.model_dump() == leader_out.result.model_dump()
    # 指数退避：0.5s 内约 6~8 次读；固定 50ms 会是 10 次，固定 10ms 会是 50 次。
    assert backend.gets < 20


# ---------------------------------------------------------------------------
# 损坏/不可读后端：降级为正常抽取，不抛、不丢结果
# ---------------------------------------------------------------------------
class _BrokenBackend:
    async def get(self, key):
        raise RuntimeError("磁盘不可读")

    async def set(self, key, value):
        raise RuntimeError("磁盘不可写")


@pytest.mark.asyncio
async def test_broken_backend_degrades_to_normal_extraction():
    cache = ContentResultCache(_BrokenBackend())
    calls = {"n": 0}

    async def extract():
        calls["n"] += 1
        return _output()

    o1, hit1 = await cache.get_or_extract("fp", "m", "正文", extract)
    o2, hit2 = await cache.get_or_extract("fp", "m", "正文", extract)
    assert calls["n"] == 2          # 写不进去 → 每次都真抽
    assert hit1 is False and hit2 is False
    assert o1.result.model_dump() == o2.result.model_dump()


class _ClaimBrokenBackend:
    """get/set 正常，只有占位接口抛错：模拟 DB 占位表暂时不可用。"""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.releases = 0

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value):
        self.store[key] = value

    async def try_claim(self, key, lease_seconds, owner):
        raise RuntimeError("占位表暂时不可用")

    async def renew_claim(self, key, lease_seconds, owner):
        return False

    async def release(self, key, owner):
        self.releases += 1

    async def discard(self, key, expected_value):
        pass


@pytest.mark.asyncio
async def test_claim_backend_error_extracts_at_once_without_owning_claim():
    """should-1：占位接口报错不是“没抢到”，不能去等满 remote_wait_seconds，
    也不能在失败时 release（本协程从没真正持有占位）。"""
    backend = _ClaimBrokenBackend()
    cache = ContentResultCache(backend, remote_wait_seconds=30.0)
    calls = {"n": 0}

    async def extract():
        calls["n"] += 1
        return _output()

    loop = asyncio.get_running_loop()
    started = loop.time()
    out, hit = await cache.get_or_extract("fp", "m", "占位接口坏了", extract)
    elapsed = loop.time() - started
    assert hit is False and calls["n"] == 1
    assert elapsed < 1.0               # 没有把 30s 等待窗口等满
    assert backend.releases == 0       # 没持有占位 → 不误删别人的
    assert out.result.entities[0].name == "华为"


class _CorruptPayloadBackend:
    def __init__(self):
        self.store = {}

    async def get(self, key):
        return "{这不是合法json"

    async def set(self, key, value):
        self.store[key] = value


@pytest.mark.asyncio
async def test_corrupt_payload_is_treated_as_miss():
    cache = ContentResultCache(_CorruptPayloadBackend())
    calls = {"n": 0}

    async def extract():
        calls["n"] += 1
        return _output()

    out, hit = await cache.get_or_extract("fp", "m", "正文", extract)
    assert hit is False
    assert calls["n"] == 1
    assert out.result.entities[0].name == "华为"


@pytest.mark.asyncio
async def test_concurrent_workers_on_corrupt_payload_reextract_once():
    """nit-3：坏载荷也走占位 —— 两个 worker 同时遇到同一条坏行只重抽一次。"""
    import asyncio

    backend = _ClaimableFakeBackend()
    key = build_cache_key("fp", "m", "坏载荷单飞")
    backend._store[key] = "{这不是合法json"
    cache_a = ContentResultCache(backend)
    cache_b = ContentResultCache(backend, poll_seconds=0.005)
    calls = {"n": 0}

    async def extract():
        calls["n"] += 1
        await asyncio.sleep(0.05)      # 让另一个实例进入等待/轮询
        return _output()

    results = await asyncio.gather(
        cache_a.get_or_extract("fp", "m", "坏载荷单飞", extract),
        cache_b.get_or_extract("fp", "m", "坏载荷单飞", extract))
    assert calls["n"] == 1
    assert sorted(hit for _, hit in results) == [False, True]


@pytest.mark.asyncio
async def test_concurrent_singleflight_failure_propagates_and_clears():
    """抽取者失败时等待者收到同一异常，且键被释放后可再次正常抽取（不永久卡单飞）。"""
    import asyncio

    cache = ContentResultCache(InMemoryCacheBackend())
    attempt = {"n": 0}

    async def flaky():
        attempt["n"] += 1
        await asyncio.sleep(0)          # 真实抽取在模型调用处必然让出事件循环
        if attempt["n"] == 1:
            raise RuntimeError("模型超时")
        return _output()

    key_text = "同键失败后重试"
    # 制造同键并发：全部都在第一次抽取上汇合
    async def call():
        return await cache.get_or_extract("fp", "m", key_text, flaky)

    results = await asyncio.gather(
        *[call() for _ in range(4)], return_exceptions=True)
    assert all(isinstance(r, RuntimeError) for r in results)
    assert attempt["n"] == 1                 # 仍只真实调用一次
    # 单飞已释放：下一次调用重新抽取并成功、随后可命中
    out, hit = await cache.get_or_extract("fp", "m", key_text, flaky)
    assert hit is False and attempt["n"] == 2
    out2, hit2 = await cache.get_or_extract("fp", "m", key_text, flaky)
    assert hit2 is True and attempt["n"] == 2


@pytest.mark.asyncio
async def test_single_call_failure_leaves_no_unretrieved_future_exception():
    """单块失败且无并发等待者时，不能留下没人取异常的 future（S6）。"""
    import asyncio
    import gc

    cache = ContentResultCache(InMemoryCacheBackend())
    unhandled = []
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _l, ctx: unhandled.append(ctx))

    async def boom():
        raise RuntimeError("模型超时")

    try:
        with pytest.raises(RuntimeError):
            await cache.get_or_extract("fp", "m", "单块失败", boom)
        gc.collect()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous)

    messages = [str(ctx.get("message", "")) for ctx in unhandled]
    assert not any("never retrieved" in m for m in messages), messages


class _SlowGetBackend:
    """get 会真实让出事件循环，模拟 postgres 网络往返；统计并发读峰值。"""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.active = 0
        self.max_active = 0

    async def get(self, key):
        import asyncio

        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.02)
            return self.store.get(key)
        finally:
            self.active -= 1

    async def set(self, key, value):
        self.store[key] = value


@pytest.mark.asyncio
async def test_backend_reads_are_not_serialized_by_global_inflight_lock():
    """不同键的缓存读不能排在同一把全局锁里（S5）：并发读峰值应 > 1。"""
    import asyncio

    backend = _SlowGetBackend()
    cache = ContentResultCache(backend)

    async def extract():
        return _output()

    await asyncio.gather(
        cache.get_or_extract("fp", "m", "键A", extract),
        cache.get_or_extract("fp", "m", "键B", extract),
    )
    assert backend.max_active >= 2


def test_memory_backend_evicts_least_recently_used():
    """进程内后端必须有上限，长跑 worker 不能只增不减（N6）。"""
    import asyncio

    backend = InMemoryCacheBackend(max_entries=2)
    asyncio.run(backend.set("a", "1"))
    asyncio.run(backend.set("b", "2"))
    assert asyncio.run(backend.get("a")) == "1"   # 访问把 a 提到最近使用
    asyncio.run(backend.set("c", "3"))            # 淘汰最久未用的 b
    assert asyncio.run(backend.get("b")) is None
    assert asyncio.run(backend.get("a")) == "1"
    assert asyncio.run(backend.get("c")) == "3"


class _RecordingPool:
    """只记录 SQL 的假 asyncpg pool（不需要真 PostgreSQL）。"""

    def __init__(self):
        self.executed: list[str] = []

    async def execute(self, sql, *args):
        self.executed.append(sql)
        return None

    async def fetchval(self, sql, *args):
        self.executed.append(sql)
        return None


@pytest.mark.asyncio
async def test_postgres_purge_is_throttled_not_on_every_write():
    """TTL 清理不能每次写入都 DELETE：一篇 30 块文档不该打出 30 条清理（N7）。"""
    from moray.extraction.cache import PostgresCacheBackend

    pool = _RecordingPool()
    backend = PostgresCacheBackend(pool, ttl_hours=1)
    for i in range(5):
        await backend.set(f"k{i}", "v")
    deletes = [sql for sql in pool.executed if "DELETE" in sql]
    assert len(deletes) == 1          # 首次清理后，间隔期内不再重复


# ---------------------------------------------------------------------------
# 端到端：接入 ChunkedExtractionPipeline
# ---------------------------------------------------------------------------
from moray.extraction.pipeline import ChunkedExtractionPipeline  # noqa: E402
from moray.extraction.chunking import Chunk, Chunker  # noqa: E402
from moray.schema import DomainSpec, TypeSpec  # noqa: E402


class _CountingRealExtractor:
    """满足 pipeline 契约并带 domain_fingerprint；对同一段正文确定性产出。"""

    def __init__(self, domain, model_name="fake-model"):
        self.domain = domain
        self.model_name = model_name
        self.calls = 0

    async def extract(self, document):
        # 真实模型调用必然让出事件循环；sleep(0) 让同键并发都进入单飞关键区。
        import asyncio

        await asyncio.sleep(0)
        self.calls += 1
        name = document.text[:6]
        res = ExtractionResult(entities=[Entity(
            name=name, type="ORG",
            mentions=[EntityMention(surface=name,
                                    span=Span(start=0, end=len(name), text=name))])])
        return ExtractionOutput(
            document_id=document.id, domain_code=self.domain.code,
            result=res, model_name=self.model_name,
            usage=Usage(input_tokens=3, output_tokens=2), attempts=1)


def _long_text():
    return "".join(f"第{n}段记载某某事件{n}。" for n in range(1, 40))


def _chunks(text):
    return Chunker(max_units=40, overlap_units=0).chunk_text(text)


def _pipeline(real, cache=None):
    return ChunkedExtractionPipeline(
        real, concurrency=2, result_cache=cache)


@pytest.mark.asyncio
async def test_pipeline_second_run_makes_no_calls_and_matches_exactly():
    text = _long_text()
    real = _CountingRealExtractor(
        DomainSpec(code="general", name="通用",
                   types=(TypeSpec("ORG", "机构", "机构"),)))
    cache = ContentResultCache(InMemoryCacheBackend())
    doc1 = __import__("moray.extraction.document", fromlist=["Document"]).Document(
        id="docA", text=text, source="t", domain_code="general")

    out1 = await _pipeline(real, cache).run(doc1, _chunks(text))
    calls_first = real.calls
    out2 = await _pipeline(real, cache).run(doc1, _chunks(text))

    assert calls_first >= 2                       # 第一次确实抽了多块
    assert real.calls == calls_first              # 第二次零模型调用
    assert out2.result.model_dump() == out1.result.model_dump()
    # 实际模型尝试次数：首跑=真抽块数，全命中=0，而不是块数（N4）
    assert out1.attempts == len(_chunks(text))
    assert out2.attempts == 0
    # 全局偏移逐字段一致，且都能对回原文
    for e in out2.result.entities:
        for m in e.mentions:
            assert m.span is not None
            assert text[m.span.start:m.span.end] == m.span.text
    assert out2.usage.cache_hits == len(_chunks(text))
    # 命中不产生任何真实 token
    assert out2.usage.input_tokens == 0
    assert out2.usage.output_tokens == 0


@pytest.mark.asyncio
async def test_pipeline_cache_hit_still_emits_one_metric_record_per_chunk():
    """缓存命中不打破文档级"每块一条"度量口径（01 × 05 合并语义）。

    命中块：finish_reason=cache_hit、attempts=0、token 全 0，用量只记 cache_hits；
    chunk_calls 仍等于块数，看板不会因命中率上升而丢失分块维度。
    """
    from moray.extraction import FINISH_CACHE_HIT

    text = _long_text()
    real = _CountingRealExtractor(
        DomainSpec(code="general", name="通用",
                   types=(TypeSpec("ORG", "机构", "机构"),)))
    cache = ContentResultCache(InMemoryCacheBackend())
    Document = __import__("moray.extraction.document", fromlist=["Document"]).Document
    doc = Document(id="docM", text=text, source="t", domain_code="general")
    chunks = _chunks(text)

    out1 = await _pipeline(real, cache).run(doc, chunks)
    dm1 = out1.document_metrics
    assert dm1 is not None
    assert len(dm1.chunks) == len(chunks)
    assert dm1.summary()["cache_hit_chunks"] == 0
    assert all(r.attempts >= 1 and r.input_tokens > 0 for r in dm1.chunks)

    out2 = await _pipeline(real, cache).run(doc, chunks)
    dm2 = out2.document_metrics
    assert dm2 is not None
    # 每块一条记录仍然成立，块 id 覆盖完整
    assert len(dm2.chunks) == len(chunks)
    assert {r.chunk_id for r in dm2.chunks} == {c.id for c in chunks}
    s = dm2.summary()
    assert s["chunk_calls"] == len(chunks)
    assert s["cache_hit_chunks"] == len(chunks)
    assert s["total_attempts"] == 0            # 命中块没有模型尝试
    assert s["truncated_chunks"] == 0 and s["rework_rate"] == 0.0
    # 命中不产生真实 token（成本口径不变），只记 cache_hits
    assert s["input_tokens"] == 0 and s["output_tokens"] == 0
    assert out2.usage.cache_hits == len(chunks)
    for rec in dm2.chunks:
        assert rec.finish_reason == FINISH_CACHE_HIT
        assert rec.cache_hit is True
        assert rec.attempts == 0
        assert rec.input_tokens == rec.output_tokens == 0
        assert rec.truncated is False and rec.error == ""


@pytest.mark.asyncio
async def test_warm_prefix_is_skipped_when_all_chunks_are_cached():
    """warm_prefix 与结果缓存同开时，全命中的第二次不能凭空发一次真实调用（N3）。"""
    class _WarmExtractor(_CountingRealExtractor):
        def __init__(self, domain):
            super().__init__(domain)
            self.warm_calls = 0

        async def warm_prefix(self):
            self.warm_calls += 1
            return Usage(input_tokens=5, output_tokens=1)

    text = _long_text()
    chunks = _chunks(text)
    real = _WarmExtractor(
        DomainSpec(code="general", name="通用",
                   types=(TypeSpec("ORG", "机构", "机构"),)))
    cache = ContentResultCache(InMemoryCacheBackend())
    Document = __import__("moray.extraction.document", fromlist=["Document"]).Document
    doc = Document(id="d", text=text, source="t")

    def pipe():
        return ChunkedExtractionPipeline(
            real, concurrency=2, warm_prefix=True, result_cache=cache)

    await pipe().run(doc, chunks)
    assert real.warm_calls == 1                  # 首跑有未命中，预热一次
    calls_first = real.calls

    out2 = await pipe().run(doc, chunks)
    assert real.warm_calls == 1                  # 全命中 → 不再预热
    assert real.calls == calls_first             # 也没有任何真实抽取
    assert out2.usage.input_tokens == 0          # 预热 token 未混进来
    assert out2.usage.cache_hits == len(chunks)
    assert out2.attempts == 0


@pytest.mark.asyncio
async def test_cache_hit_restores_backfill_and_cache_savings():
    """03 × 05：缓存命中仍还原回填信号，并把省下的 token 记 cache_savings。

    05 r2 把顶层 ``reused_*_tokens``（旧名）收进 ``Usage.cache_savings`` 嵌套命名空间
    （N2），语义不变：命中只记 cache_hits，省下的量单独可查、不计真实成本。
    """
    from moray.extraction.document import Document

    class _BackfillExtractor(_CountingRealExtractor):
        async def extract(self, document):
            out = await super().extract(document)
            out.backfill = {
                "entities_total": 1, "entities_grounded": 1,
                "matched_literals": {document.text[:6]: 1},
            }
            return out

    text = _long_text()
    real = _BackfillExtractor(
        DomainSpec(code="general", name="通用",
                   types=(TypeSpec("ORG", "机构", "机构"),)))
    cache = ContentResultCache(InMemoryCacheBackend())
    doc = Document(id="docB", text=text, source="t", domain_code="general")
    chunks = _chunks(text)

    out1 = await _pipeline(real, cache).run(doc, chunks)
    assert out1.backfill is not None
    calls_first = real.calls

    out2 = await _pipeline(real, cache).run(doc, chunks)
    assert real.calls == calls_first
    # 回填信号随缓存产物一起还原，不因命中而消失
    assert out2.backfill is not None
    assert out2.backfill["entities_total"] == len(chunks)
    assert out2.backfill["entities_grounded"] == len(chunks)
    # 命中只记 cache_hits + cache_savings，真实 token 仍为 0
    assert out2.usage.cache_hits == len(chunks)
    assert out2.usage.cache_savings.input_tokens == 3 * len(chunks)
    assert out2.usage.cache_savings.output_tokens == 2 * len(chunks)
    assert out2.usage.input_tokens == 0
    assert out2.usage.output_tokens == 0


@pytest.mark.asyncio
async def test_pipeline_cache_on_failed_chunk_still_counts_tokens_and_metrics():
    """缓存开启时失败块语义不回退（01 的失败块口径不被 05 覆盖）。

    该块已花的 input/output/cached token 仍要计入文档用量；失败度量要进
    document_metrics（宽容模式）或挂到异常上（默认整篇抛错）。
    """
    from moray.extraction import ChunkCallMetrics, ExtractionError

    text = _long_text()
    chunks = _chunks(text)
    failing_id = chunks[0].id

    class _FailOne(_CountingRealExtractor):
        async def extract(self, document):
            if document.id == failing_id:
                raise ExtractionError(
                    f"块 {document.id} 截断失败", metrics=ChunkCallMetrics(
                        chunk_id=document.id, attempts=2,
                        finish_reason="length",
                        input_tokens=100, output_tokens=50,
                        cached_input_tokens=10,
                        wait_s=0.0, prefill_s=None, decode_s=None,
                        attempt_finish_reasons=["length", "length"],
                        truncated_attempts=2, error="length"))
            return await super().extract(document)

    def build():
        real = _FailOne(DomainSpec(
            code="general", name="通用",
            types=(TypeSpec("ORG", "机构", "机构"),)))
        return real, ContentResultCache(InMemoryCacheBackend())

    Doc = __import__("moray.extraction.document", fromlist=["Document"]).Document

    # 宽容模式：其余块照常归并，失败块的 token 与度量都不丢
    real, cache = build()
    out = await _pipeline(real, cache).run(
        Doc(id="docF", text=text, source="t", domain_code="general"),
        chunks, raise_on_chunk_error=False)
    dm = out.document_metrics
    assert dm is not None and len(dm.chunks) == len(chunks)
    failed = [c for c in dm.chunks if c.error]
    assert len(failed) == 1 and failed[0].chunk_id == failing_id
    assert failed[0].truncated is True and failed[0].attempts == 2
    # 失败块已花 token 进文档用量：其它块各 3/2 = 3*(n-1) 再叠加 100/50
    n_ok = len(chunks) - 1
    assert out.usage.input_tokens == 3 * n_ok + 100
    assert out.usage.output_tokens == 2 * n_ok + 50
    assert dm.summary()["truncated_chunks"] == 1

    # 默认整篇抛错语义不变，且异常上仍挂着完整文档级度量
    real2, cache2 = build()
    with pytest.raises(ExtractionError) as excinfo:
        await _pipeline(real2, cache2).run(
            Doc(id="docF2", text=text, source="t", domain_code="general"),
            chunks)
    dm2 = getattr(excinfo.value, "document_metrics", None)
    assert dm2 is not None
    assert dm2.summary()["truncated_chunks"] == 1


@pytest.mark.asyncio
async def test_pipeline_without_cache_keeps_original_behavior():
    text = _long_text()
    real = _CountingRealExtractor(
        DomainSpec(code="general", name="通用",
                   types=(TypeSpec("ORG", "机构", "机构"),)))
    doc = __import__("moray.extraction.document", fromlist=["Document"]).Document(
        id="docA", text=text, source="t", domain_code="general")
    chunks = _chunks(text)
    out = await _pipeline(real, None).run(doc, chunks)
    assert real.calls == len(chunks)             # 每块都真抽
    assert out.usage.cache_hits == 0
    assert out.usage.input_tokens == 3 * len(chunks)
    assert out.usage.output_tokens == 2 * len(chunks)


@pytest.mark.asyncio
async def test_pipeline_content_edit_invalidates_cache():
    text1 = _long_text()
    real = _CountingRealExtractor(
        DomainSpec(code="general", name="通用",
                   types=(TypeSpec("ORG", "机构", "机构"),)))
    cache = ContentResultCache(InMemoryCacheBackend())
    Document = __import__("moray.extraction.document", fromlist=["Document"]).Document
    await _pipeline(real, cache).run(
        Document(id="d", text=text1, source="t"), _chunks(text1))
    calls_after_first = real.calls

    # 改正文中一个字（同长度替换，分块边界不变，便于精确断言）
    text2 = text1.replace("第2段", "第２段", 1)
    chunks2 = _chunks(text2)
    assert len(chunks2) == len(_chunks(text1))
    out2 = await _pipeline(real, cache).run(
        Document(id="d", text=text2, source="t"), chunks2)
    # 只有含被改字符的那一块真抽，其余块全部命中
    assert real.calls == calls_after_first + 1
    assert out2.usage.cache_hits == len(chunks2) - 1


@pytest.mark.asyncio
async def test_pipeline_domain_or_model_change_invalidates_cache():
    text = _long_text()
    chunks = _chunks(text)
    Document = __import__("moray.extraction.document", fromlist=["Document"]).Document
    doc = Document(id="d", text=text, source="t")

    def domain(code):
        return DomainSpec(code=code, name="通用",
                          types=(TypeSpec("ORG", "机构", "机构"),))

    real = _CountingRealExtractor(domain("general"))
    cache = ContentResultCache(InMemoryCacheBackend())
    await _pipeline(real, cache).run(doc, chunks)
    n1 = real.calls

    # 换模型名：指纹同、模型不同 → 全部重抽
    real.model_name = "another-model"
    await _pipeline(real, cache).run(doc, chunks)
    assert real.calls == n1 + len(chunks)

    # 切领域（指纹变化）：同样全部重抽
    real2 = _CountingRealExtractor(
        DomainSpec(code="finance", name="金融",
                   types=(TypeSpec("ORG", "机构", "机构"),)),
        model_name="another-model")
    await _pipeline(real2, cache).run(doc, chunks)
    assert real2.calls == len(chunks)


# ---------------------------------------------------------------------------
# 编排入口 run_chunked_team：缓存贯穿断点续跑/重试/重复提交
# ---------------------------------------------------------------------------
class _TeamProvider:
    def __init__(self, real):
        self._real = real

    async def get_extractor(self, domain_code):
        return self._real, self._real.domain


@pytest.mark.asyncio
async def test_chunked_team_resume_second_submission_uses_cache():
    """同一长文第二次抽取（续跑/重试/重复提交）：全命中、零真实 token。"""
    from moray.orchestration import run_chunked_team

    text = _long_text()
    real = _CountingRealExtractor(
        DomainSpec(code="general", name="通用",
                   types=(TypeSpec("ORG", "机构", "机构"),)))
    provider = _TeamProvider(real)
    cache = ContentResultCache(InMemoryCacheBackend())

    kwargs = dict(
        extractor=None, provider=provider, domain_code="general",
        chunk_threshold_units=20, chunk_max_units=40, chunk_overlap_units=0,
        chunk_concurrency=2, result_cache=cache)

    async def closure(text, feedback):
        raise AssertionError("chunk path 不应走整篇闭包")

    u1 = Usage()
    out1 = await run_chunked_team(text, **kwargs, usage=u1)
    n_chunks = len(_chunks(text))
    assert real.calls == n_chunks

    u2 = Usage()
    out2 = await run_chunked_team(text, **kwargs, usage=u2)
    assert real.calls == n_chunks                 # 第二次没有新模型调用
    assert out2.result.model_dump() == out1.result.model_dump()
    assert u2.cache_hits == n_chunks
    assert u2.input_tokens == 0 and u2.output_tokens == 0


@pytest.mark.asyncio
async def test_chunked_team_without_cache_arg_keeps_old_behavior():
    """不传 result_cache：分块路径逐字节保持旧行为（每块真抽）。"""
    from moray.orchestration import run_chunked_team

    text = _long_text()
    real = _CountingRealExtractor(
        DomainSpec(code="general", name="通用",
                   types=(TypeSpec("ORG", "机构", "机构"),)))
    provider = _TeamProvider(real)
    u = Usage()
    await run_chunked_team(
        text, extractor=None, provider=provider, domain_code="general",
        chunk_threshold_units=20, chunk_max_units=40, chunk_overlap_units=0,
        chunk_concurrency=2, usage=u)
    n_chunks = len(_chunks(text))
    assert real.calls == n_chunks
    assert u.cache_hits == 0
    assert u.input_tokens == 3 * n_chunks


@pytest.mark.asyncio
async def test_domain_enum_edit_changes_fingerprint_and_invalidates():
    """改实体类型枚举（领域配置版本变化）→ 指纹变化 → 全部重抽。

    直接验证领域指纹这一缓存键要素：枚举增删必须换指纹，这是 schema 版本
    失效的独立证据（不依赖 DB updated_at）。
    """
    from moray.schema import domain_fingerprint as fp

    base = DomainSpec(code="g", name="通用",
                      types=(TypeSpec("ORG", "机构", "机构"),))
    with_new_enum = DomainSpec(
        code="g", name="通用",
        types=(TypeSpec("ORG", "机构", "机构"),
               TypeSpec("PRODUCT", "产品", "产品")))
    assert fp(base) != fp(with_new_enum)

    text = _long_text()
    chunks = _chunks(text)
    Document = __import__("moray.extraction.document", fromlist=["Document"]).Document
    doc = Document(id="d", text=text, source="t")
    cache = ContentResultCache(InMemoryCacheBackend())

    real = _CountingRealExtractor(base)
    await _pipeline(real, cache).run(doc, chunks)
    n1 = real.calls

    real2 = _CountingRealExtractor(with_new_enum)
    await _pipeline(real2, cache).run(doc, chunks)
    assert real2.calls == len(chunks)     # 指纹不同 → 没有任何命中
    assert real.calls == n1               # 旧领域结果也未被污染


@pytest.mark.asyncio
async def test_cached_usage_excluded_from_real_cost_fields():
    """命中缓存的分块只增加 cache_hits，不增加任何真实 token 计量字段。

    看板/成本统计汇总的是 input_tokens/output_tokens/cached_input_tokens
    （都是真实 LLM 计量）；cache_hits 是独立的“省了多少次调用”计数，成本
    不会把命中误算成真实 token。
    """
    text = _long_text()
    real = _CountingRealExtractor(
        DomainSpec(code="general", name="通用",
                   types=(TypeSpec("ORG", "机构", "机构"),)))
    cache = ContentResultCache(InMemoryCacheBackend())
    Document = __import__("moray.extraction.document", fromlist=["Document"]).Document
    doc = Document(id="d", text=text, source="t")
    pipe = _pipeline(real, cache)

    out1 = await pipe.run(doc, _chunks(text))
    n_chunks = len(_chunks(text))
    assert out1.usage.input_tokens == 3 * n_chunks
    assert out1.usage.output_tokens == 2 * n_chunks

    out2 = await pipe.run(doc, _chunks(text))
    # 第二次：真实 token 增量为 0，只有 cache_hits
    assert out2.usage.input_tokens == 0
    assert out2.usage.output_tokens == 0
    assert out2.usage.cached_input_tokens == 0
    assert out2.usage.cache_hits == n_chunks
    # 命中时缓存里还原出来的用量嵌在独立命名空间，不计真实成本（S4/N2）
    assert out2.usage.cache_savings.input_tokens == 3 * n_chunks
    assert out2.usage.cache_savings.output_tokens == 2 * n_chunks
    # 序列化（作业结果/API 响应/看板都走 model_dump）带上这一列
    dumped = out2.usage.model_dump()
    assert dumped["cache_hits"] == n_chunks
    assert dumped["input_tokens"] == 0
    # 没有与真实 token 同级的 reused_*_tokens：任何 *_tokens 求和都不会误算
    assert not [k for k in dumped if k.endswith("_tokens") and "saved" in k]
    assert "reused_input_tokens" not in dumped
    assert dumped["cache_savings"] == {
        "input_tokens": 3 * n_chunks, "output_tokens": 2 * n_chunks}
    # 提示缓存命中率仍是真实 token 口径，不被内容缓存命中污染（分母 0 → 0）
    assert dumped["cache_hit_rate"] == 0.0


@pytest.mark.asyncio
async def test_pipeline_concurrent_same_chunk_text_extracts_once():
    """多个协程并发抽取【同一内容】的分块时只发生一次真实模型调用（单飞）。

    用同一段正文造多个不同位置的块：键不含位置，故同内容块应去重为一次调用；
    偏移换算仍各自按块位置平移，结果位置正确。
    """
    body = "华为技术有限公司位于深圳。"
    # 同内容、不同 start 的块（位置只影响换算，不进键）
    same_chunks = [
        Chunk(id=f"d#c{i:04d}", text=body, start=10 * i,
              end=10 * i + len(body), index=i)
        for i in range(6)
    ]
    real = _CountingRealExtractor(
        DomainSpec(code="general", name="通用",
                   types=(TypeSpec("ORG", "机构", "机构"),)))
    cache = ContentResultCache(InMemoryCacheBackend())
    Document = __import__("moray.extraction.document", fromlist=["Document"]).Document
    doc = Document(id="d", text=body, source="t")

    out = await _pipeline(real, cache).run(doc, same_chunks)
    assert real.calls == 1                 # 同内容只真抽一次
    assert out.usage.cache_hits == 5
    # 每个块都按自己的 start 换算：实体名一致、全局偏移各不相同
    offsets = sorted(m.span.start for e in out.result.entities
                     for m in e.mentions if m.span)
    assert offsets == [10 * i for i in range(6)]


@pytest.mark.asyncio
async def test_domain_fingerprint_computed_once_per_pipeline(monkeypatch):
    """每个分块都重建一次领域模型是纯浪费（N2）：一次 pipeline.run 只算一次指纹。"""
    import moray.extraction.pipeline as pipeline_mod

    calls = {"n": 0}
    original = pipeline_mod.domain_fingerprint

    def counting(domain, *, slim=False):
        calls["n"] += 1
        return original(domain, slim=slim)

    monkeypatch.setattr(pipeline_mod, "domain_fingerprint", counting)

    text = _long_text()
    real = _CountingRealExtractor(
        DomainSpec(code="general", name="通用",
                   types=(TypeSpec("ORG", "机构", "机构"),)))
    cache = ContentResultCache(InMemoryCacheBackend())
    Document = __import__("moray.extraction.document", fromlist=["Document"]).Document
    pipe = _pipeline(real, cache)
    await pipe.run(Document(id="d", text=text, source="t"), _chunks(text))
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_non_domain_spec_fallback_includes_full_identity():
    """非 DomainSpec 的假对象也不能只退化成 code：不同 domain 内容必须换键（N3）。"""
    from types import SimpleNamespace

    class _FakeExtractor:
        def __init__(self, domain):
            self.domain = domain
            self.model_name = "fake-model"
            self.calls = 0

        async def extract(self, document):
            self.calls += 1
            return ExtractionOutput(
                document_id=document.id, domain_code=self.domain.code,
                result=ExtractionResult(), model_name=self.model_name,
                usage=Usage(input_tokens=1), attempts=1)

    text = _long_text()
    chunks = _chunks(text)
    Document = __import__("moray.extraction.document", fromlist=["Document"]).Document
    cache = ContentResultCache(InMemoryCacheBackend())

    a = _FakeExtractor(SimpleNamespace(code="g", types=("ORG",)))
    b = _FakeExtractor(SimpleNamespace(code="g", types=("ORG", "PER")))
    await _pipeline(a, cache).run(Document(id="d", text=text, source="t"), chunks)
    await _pipeline(b, cache).run(Document(id="d", text=text, source="t"), chunks)
    assert b.calls == len(chunks)      # 同一 code 但配置内容不同 → 不命中


# ---------------------------------------------------------------------------
# 第 3 轮评审回归：取消不钉死、等待方不误删占位、attempts 旧口径
# （对应 reviews/05-r3-minirag-review.md 的 blocker-1 / should-1 / should-2）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cancelled_call_in_remote_wait_does_not_wedge_key():
    """blocker-1：在 _wait_remote 里被取消后，同键新调用必须还能完成。

    旧实现从登记 _inflight 到进入 try 之间没有异常保护：取消会留下一个永远
    pending 的 future，之后同键调用全被当等待方永久挂住。这里复现该窗口。
    """
    import asyncio

    backend = _ClaimableFakeBackend()
    key = build_cache_key("fp", "m", "取消不钉死")
    assert await backend.try_claim(key, 30.0, "other-worker")  # 别的 worker 持有健康占位
    cache = ContentResultCache(
        backend, remote_wait_seconds=30.0,
        poll_seconds=0.005, claim_lease_seconds=30.0)
    calls = {"n": 0}

    async def extract():
        calls["n"] += 1
        return _output()

    task = asyncio.create_task(
        cache.get_or_extract("fp", "m", "取消不钉死", extract))
    await asyncio.sleep(0.05)                      # 让它在 _wait_remote 轮询
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # 出问题的实现会在这里留下 pending future
    assert key not in cache._inflight

    await backend.release(key, "other-worker")     # 原 leader 失败/结束
    out, hit = await asyncio.wait_for(
        cache.get_or_extract("fp", "m", "取消不钉死", extract), timeout=1.0)
    assert hit is False
    assert calls["n"] == 1
    assert out.result.entities[0].name == "华为"


@pytest.mark.asyncio
async def test_waiter_self_extract_failure_does_not_release_other_claim():
    """should-1：等待超时后自抽失败，不能 release 掉别人仍存活的占位。

    旧实现无条件 release，第三个 worker 立刻能抢占 → 同键出现第二次真调。
    """
    import asyncio

    backend = _ClaimableFakeBackend()
    key = build_cache_key("fp", "m", "别删别人的占位")
    assert await backend.try_claim(key, 30.0, "worker-q")  # Q 的占位健康且未过期
    cache = ContentResultCache(
        backend, remote_wait_seconds=0.05,
        poll_seconds=0.005, claim_lease_seconds=30.0)

    async def boom():
        raise RuntimeError("自抽失败")

    with pytest.raises(RuntimeError):
        await cache.get_or_extract("fp", "m", "别删别人的占位", boom)

    assert backend.releases == 0                   # 没碰别人的占位
    # Q 的占位仍在：第三个 worker 抢不到，不会发生第二次真调
    assert await backend.try_claim(key, 0.01, "worker-3") is False
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_cancelled_waiter_decrements_waiter_count():
    """nit-7：等待方被取消后 _waiters 计数必须回退，否则 leader 失败时会给
    没人 await 的 future 设置异常（Future exception was never retrieved）。"""
    import asyncio

    cache = ContentResultCache(InMemoryCacheBackend())
    release = asyncio.Event()

    async def slow():
        await release.wait()
        return _output()

    key = build_cache_key("fp", "m", "等待者取消")
    leader = asyncio.create_task(
        cache.get_or_extract("fp", "m", "等待者取消", slow))
    await asyncio.sleep(0)          # 让 leader 登记 _inflight 并进入 slow()
    waiter = asyncio.create_task(
        cache.get_or_extract("fp", "m", "等待者取消", slow))
    await asyncio.sleep(0)          # 让 waiter 登记为等待者
    assert cache._waiters.get(key, 0) == 1

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert cache._waiters.get(key, 0) == 0

    release.set()
    out, hit = await leader
    assert hit is False and out.result.entities[0].name == "华为"


class _RetryCountExtractor(_CountingRealExtractor):
    """每个块都上报 attempts=2（模拟一次校验重试后成功）。"""

    async def extract(self, document):
        out = await super().extract(document)
        out.attempts = 2
        return out


@pytest.mark.asyncio
async def test_pipeline_without_cache_attempts_stays_chunk_count():
    """should-2：缓存关闭时 attempts 口径必须与旧实现一致（= 块数）。

    旧实现是 attempts=total；r2 改成累加真实尝试次数后，3 块其中一块重试
    会从 3 变 4，违反验收 6「缓存关闭时行为与无缓存完全一致」。
    """
    text = _long_text()
    chunks = _chunks(text)
    Document = __import__("moray.extraction.document", fromlist=["Document"]).Document
    real = _RetryCountExtractor(
        DomainSpec(code="general", name="通用",
                   types=(TypeSpec("ORG", "机构", "机构"),)))
    out = await _pipeline(real, None).run(
        Document(id="d", text=text, source="t"), chunks)
    assert real.calls == len(chunks)
    assert out.attempts == len(chunks)       # 旧口径：块数，而不是 2×块数


@pytest.mark.asyncio
async def test_pipeline_with_cache_attempts_counts_real_attempts():
    """should-2 的另一半：开了缓存才用「真实尝试次数」累加（命中块计 0）。

    口径写死在这里，避免下轮再含糊：关闭 = 块数；开启 = 真实尝试数。
    """
    text = _long_text()
    chunks = _chunks(text)
    Document = __import__("moray.extraction.document", fromlist=["Document"]).Document
    real = _RetryCountExtractor(
        DomainSpec(code="general", name="通用",
                   types=(TypeSpec("ORG", "机构", "机构"),)))
    cache = ContentResultCache(InMemoryCacheBackend())
    doc = Document(id="d", text=text, source="t")
    out = await _pipeline(real, cache).run(doc, chunks)
    assert out.attempts == 2 * len(chunks)
    # 全命中的第二次仍为 0（命中块没有模型尝试）
    out2 = await _pipeline(real, cache).run(doc, chunks)
    assert out2.attempts == 0


@pytest.mark.asyncio
async def test_waiter_takes_over_after_lease_expires_mid_wait():
    """nit-8：进来时占位有效、等待期间租约才过期 —— 必须走 _wait_remote 的
    接管分支（不是入口处的二次 _safe_claim），一个租约后接管且只真调一次。"""
    import asyncio

    backend = _ClaimableFakeBackend()
    key = build_cache_key("fp", "m", "等待期租约过期")
    assert await backend.try_claim(key, 0.08, "stale-leader")  # 进来时仍有效
    cache = ContentResultCache(
        backend, remote_wait_seconds=5.0,
        poll_seconds=0.01, claim_lease_seconds=0.05)
    calls = {"n": 0}

    async def extract():
        calls["n"] += 1
        return _output()

    loop = asyncio.get_running_loop()
    started = loop.time()
    out, hit = await cache.get_or_extract("fp", "m", "等待期租约过期", extract)
    elapsed = loop.time() - started
    assert hit is False and calls["n"] == 1
    assert elapsed >= 0.05                       # 至少等一个租约才接管
    assert elapsed < 1.0                         # 远小于 remote_wait_seconds
    assert out.result.entities[0].name == "华为"


# ---------------------------------------------------------------------------
# 第 3 轮 nit：模型身份/模板版本进键、预热探测并发、Protocol 可运行期断言
# ---------------------------------------------------------------------------
def test_extractor_model_identity_includes_provider_and_base_url(monkeypatch):
    """nit-1：同一 chat_model 挂在两个 provider/base_url 下不能共用缓存键。"""
    from moray.extraction import extractor as extractor_mod

    settings = {"provider": "ark", "base_url": "https://a.example",
                "chat_model": "same-model"}
    monkeypatch.setattr(extractor_mod, "get_settings", lambda: dict(settings))
    a = extractor_mod.Extractor("general")
    settings["base_url"] = "https://b.example"
    b = extractor_mod.Extractor("general")

    assert a.model_name == b.model_name == "same-model"
    assert a.model_identity != b.model_identity
    assert "a.example" in a.model_identity


@pytest.mark.asyncio
async def test_pipeline_uses_model_identity_when_available():
    """nit-1：pipeline 进键的是 model_identity（provider+base_url+model），
    而不是裸 chat_model。"""
    text = _long_text()
    chunks = _chunks(text)
    Document = __import__("moray.extraction.document", fromlist=["Document"]).Document
    real = _CountingRealExtractor(
        DomainSpec(code="general", name="通用",
                   types=(TypeSpec("ORG", "机构", "机构"),)))
    real.model_identity = "ark|https://a.example|same-model"
    cache = ContentResultCache(InMemoryCacheBackend())
    doc = Document(id="d", text=text, source="t")

    await _pipeline(real, cache).run(doc, chunks)
    n1 = real.calls
    # 模型名字不变，只是网关变了 → 必须换键重抽
    real.model_identity = "ark|https://b.example|same-model"
    await _pipeline(real, cache).run(doc, chunks)
    assert real.calls == n1 + len(chunks)


@pytest.mark.asyncio
async def test_prompt_lead_in_and_retry_prompt_change_invalidates_cache():
    """nit-2：_LEAD_IN / _retry_prompt 是真实 user 消息模板，改了必须失效，
    不能只靠人手递增 _PROMPT_TEMPLATE_VERSION。"""
    text = _long_text()
    chunks = _chunks(text)
    Document = __import__("moray.extraction.document", fromlist=["Document"]).Document
    real = _CountingRealExtractor(
        DomainSpec(code="general", name="通用",
                   types=(TypeSpec("ORG", "机构", "机构"),)))
    real._LEAD_IN = "请抽取下面正文中的实体与关系：\n\n"

    @staticmethod
    def _retry_prompt(error):
        return f"修正：{error}"

    real._retry_prompt = _retry_prompt
    cache = ContentResultCache(InMemoryCacheBackend())
    doc = Document(id="d", text=text, source="t")

    await _pipeline(real, cache).run(doc, chunks)
    n1 = real.calls
    real._LEAD_IN = "请抽取（v2）：\n\n"          # 只改模板，正文/领域/模型不变
    await _pipeline(real, cache).run(doc, chunks)
    assert real.calls == n1 + len(chunks)


class _ConcurrentProbeCache:
    """只实现 pipeline 用到的两个方法的假缓存，记录 has_cached 并发峰值。"""

    def __init__(self):
        self.active = 0
        self.max_active = 0

    async def has_cached(self, domain_fingerprint, model_identity, chunk_text,
                         *, sampling_fingerprint=""):
        import asyncio

        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.02)
            return True
        finally:
            self.active -= 1

    async def get_or_extract(self, domain_fingerprint, model_identity, chunk_text,
                             extract, *, sampling_fingerprint=""):
        return await extract(), True


@pytest.mark.asyncio
async def test_warm_prefix_probe_reads_are_concurrent():
    """nit-4：预热前的 has_cached 探测不能串行 N 次后端往返。"""
    text = _long_text()
    chunks = _chunks(text)
    Document = __import__("moray.extraction.document", fromlist=["Document"]).Document
    real = _CountingRealExtractor(
        DomainSpec(code="general", name="通用",
                   types=(TypeSpec("ORG", "机构", "机构"),)))
    probe = _ConcurrentProbeCache()
    pipe = ChunkedExtractionPipeline(
        real, concurrency=2, warm_prefix=True, result_cache=probe)
    await pipe.run(Document(id="d", text=text, source="t"), chunks)
    assert probe.max_active >= 2


def test_claimable_backend_protocol_is_runtime_checkable():
    """nit-10：导出并在运行期判定的 Protocol 必须 runtime_checkable。"""
    from moray.extraction.cache import ClaimableBackend

    assert isinstance(_ClaimableFakeBackend(), ClaimableBackend)
    assert not isinstance(InMemoryCacheBackend(), ClaimableBackend)


# ---------------------------------------------------------------------------
# 第 4 轮评审回归：取消互不连坐（blocker-1）、占位 owner 令牌（should-1）、
# 窗口末尾回写（nit-1）、续租抖动（nit-3）、坏后端 discard 契约（nit-5）
# （对应 reviews/05-r4-minirag-review.md）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_one_waiter_cancel_does_not_cancel_siblings_or_new_arrivals():
    """blocker-1：同键多个等待方共用 future 时，取消其中一个不能连坐其它等待方
    与取消后新到达者。

    旧实现所有等待方直接 await 同一个 future：CPython 取消 w1 会把该 future
    置为 cancelled，w2 立刻收到 CancelledError，w3 登记到已 cancelled 的
    future 上立即失败。
    """
    cache = ContentResultCache(InMemoryCacheBackend())
    key = build_cache_key("fp", "m", "等待方互不连坐")
    release = asyncio.Event()
    calls = {"n": 0}

    async def slow():
        calls["n"] += 1
        await release.wait()
        return _output()

    def call():
        return cache.get_or_extract("fp", "m", "等待方互不连坐", slow)

    leader = asyncio.create_task(call())
    await asyncio.sleep(0)                 # leader 登记 _inflight 并进入 extract
    w1 = asyncio.create_task(call())
    w2 = asyncio.create_task(call())
    await asyncio.sleep(0)                 # w1/w2 登记为等待方
    assert cache._waiters.get(key, 0) == 2

    w1.cancel()
    with pytest.raises(asyncio.CancelledError):
        await w1
    await asyncio.sleep(0.01)
    assert not w2.done(), "取消 w1 不应把 w2 一起取消"

    w3 = asyncio.create_task(call())       # 取消后新到达者
    await asyncio.sleep(0.01)
    assert not w3.done(), "取消后新到达者不应被已污染的 future 直接失败"

    release.set()
    out2, hit2 = await asyncio.wait_for(w2, timeout=1.0)
    out3, hit3 = await asyncio.wait_for(w3, timeout=1.0)
    out_leader, hit_leader = await leader
    assert (hit_leader, hit2, hit3) == (False, True, True)
    assert calls["n"] == 1
    assert out2.result.entities[0].name == "华为"
    assert out3.result.entities[0].name == "华为"


@pytest.mark.asyncio
async def test_cancelled_leader_lets_waiters_retry_and_finish():
    """blocker-1 的另一半：leader 被取消时，等待方收到非取消信号并重试，
    最终只多一次真实调用，键也不会被钉死。"""
    cache = ContentResultCache(InMemoryCacheBackend())
    key = build_cache_key("fp", "m", "leader 被取消")
    started = asyncio.Event()
    calls = {"n": 0}

    async def cancellable():
        calls["n"] += 1
        started.set()
        await asyncio.sleep(30)            # 等待被 cancel
        return _output()

    async def succeeding():
        calls["n"] += 1
        return _output("重试后")

    leader = asyncio.create_task(
        cache.get_or_extract("fp", "m", "leader 被取消", cancellable))
    await started.wait()
    waiter = asyncio.create_task(
        cache.get_or_extract("fp", "m", "leader 被取消", succeeding))
    await asyncio.sleep(0)
    assert cache._waiters.get(key, 0) == 1

    leader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leader
    out, hit = await asyncio.wait_for(waiter, timeout=1.0)
    assert hit is False                    # 等待方自己真抽，不是缓存命中
    assert out.result.entities[0].name == "重试后"
    assert calls["n"] == 2                 # leader 那次 + 重试一次，没有失败风暴
    assert key not in cache._inflight


@pytest.mark.asyncio
async def test_old_leader_claim_token_cannot_touch_new_leader_placeholder():
    """should-1：占位必须带 owner 令牌；旧 leader 续租/释放不能影响新 leader。

    旧实现只按 payload='' 匹配：A 的租约过期、B 接管后，A 的 renew 仍会成功、
    A 失败时的 release 会删掉 B 的占位，第三个 worker 立刻抢到 → 同一块两次真调。
    """
    backend = _ClaimableFakeBackend()
    key = build_cache_key("fp", "m", "旧 leader 不能干扰新 leader")
    old_owner, new_owner = "owner-old", "owner-new"
    assert await backend.try_claim(key, 0.05, old_owner)
    await asyncio.sleep(0.06)                              # A 的租约过期
    assert await backend.try_claim(key, 30.0, new_owner)   # B 接管

    assert await backend.renew_claim(key, 30.0, old_owner) is False
    await backend.release(key, old_owner)                  # A 失败：不能删 B 的占位
    assert backend._claims[key][1] == new_owner
    # 第三个 worker 抢不到，不会发生第二次真调
    assert await backend.try_claim(key, 30.0, "owner-third") is False


@pytest.mark.asyncio
async def test_stale_leader_failure_does_not_release_new_leader_claim():
    """should-1 端到端：旧 leader 抽取期间租约过期、被接管，随后旧 leader 失败，
    不能把新 leader 的占位一起删掉。"""
    backend = _ClaimableFakeBackend()
    backend.renew_errors = 10 ** 6          # 旧 leader 续租一直抖动 → 不延期
    cache_a = ContentResultCache(
        backend, claim_lease_seconds=0.05, remote_wait_seconds=5.0)
    key = build_cache_key("fp", "m", "旧 leader 失败不误删")
    started = asyncio.Event()

    async def failing():
        started.set()
        await asyncio.sleep(0.1)            # 跨过租约，期间无人续租成功
        raise RuntimeError("旧 leader 失败")

    leader = asyncio.create_task(
        cache_a.get_or_extract("fp", "m", "旧 leader 失败不误删", failing))
    await started.wait()
    await asyncio.sleep(0.06)               # 租约已过期
    assert await backend.try_claim(key, 30.0, "new-leader") is True

    with pytest.raises(RuntimeError):
        await leader
    await asyncio.sleep(0)
    assert backend._claims[key][1] == "new-leader", "旧 leader 不得删新 leader 占位"
    assert await backend.try_claim(key, 30.0, "third-worker") is False


class _LatePayloadBackend:
    """先返回占位行，等到 delay 后才出现载荷。

    模拟 leader 恰在等待窗口最后一次轮询之后、循环退出之前回写载荷（nit-1）。
    """

    def __init__(self, delay: float):
        import json

        self.delay = delay
        self.start: float | None = None
        self._payload = json.dumps(
            {"v": 1, "output": _output().model_dump(mode="json")},
            ensure_ascii=False)

    async def get(self, key):
        loop = asyncio.get_running_loop()
        if self.start is None:
            self.start = loop.time()
        if loop.time() - self.start >= self.delay:
            return self._payload
        return ""                           # leader 仍持有占位

    async def set(self, key, value):
        pass

    async def try_claim(self, key, lease_seconds, owner):
        return False                        # 别人一直是健康 leader

    async def renew_claim(self, key, lease_seconds, owner):
        return False

    async def release(self, key, owner):
        pass


@pytest.mark.asyncio
async def test_wait_window_tail_reads_payload_written_just_before_deadline():
    """nit-1：轮询因 deadline 退出前必须再读一次，不能忽略刚回写的载荷。"""
    backend = _LatePayloadBackend(delay=0.08)
    cache = ContentResultCache(
        backend, remote_wait_seconds=0.1,
        poll_seconds=0.05, max_poll_seconds=0.05)
    calls = {"n": 0}

    async def extract():
        calls["n"] += 1
        return _output("不该被调用")

    out, hit = await cache.get_or_extract("fp", "m", "窗口末尾回写", extract)
    assert hit is True
    assert calls["n"] == 0
    assert out.result.entities[0].name == "华为"


@pytest.mark.asyncio
async def test_transient_renew_error_does_not_stop_renewal():
    """nit-3：一次续租异常只是 DB 抖动，不能当成“占位已丢”永久停止续租。"""
    backend = _ClaimableFakeBackend()
    backend.renew_errors = 1
    cache_a = ContentResultCache(backend, claim_lease_seconds=0.15)
    cache_b = ContentResultCache(
        backend, claim_lease_seconds=0.15,
        poll_seconds=0.01, remote_wait_seconds=5.0)
    calls = {"n": 0}

    async def slow():
        calls["n"] += 1
        await asyncio.sleep(0.5)            # 超过两个租约，必须持续续租
        return _output()

    leader = asyncio.create_task(
        cache_a.get_or_extract("fp", "m", "续租抖动", slow))
    await asyncio.sleep(0.02)
    out, hit = await cache_b.get_or_extract("fp", "m", "续租抖动", slow)
    leader_out, _ = await leader
    assert calls["n"] == 1                 # 等待方没有接管 → 没有第二次真调
    assert hit is True
    assert out.result.model_dump() == leader_out.result.model_dump()
    assert backend.renews >= 1             # 抖动之后仍成功续租


def test_duck_domain_fingerprint_is_stable_across_object_instances():
    """nit-4：非 DomainSpec 的回退指纹不能吃 object.__repr__（含内存地址），
    否则每个进程算出不同键，缓存永远不命中。"""
    class _DuckDomain:
        """默认 object.__repr__：含内存地址，正是旧实现进键的东西。"""

        def __init__(self, **fields):
            self.__dict__.update(fields)

    def fingerprint(**fields):
        domain = _DuckDomain(**fields)
        return _pipeline(_CountingRealExtractor(domain), ContentResultCache(
            InMemoryCacheBackend()))._domain_fingerprint()

    a = fingerprint(code="general", types=("ORG",))
    b = fingerprint(code="general", types=("ORG",))
    assert a == b
    assert "0x" not in a
    # 文档化字段变了必须换指纹（否则会读到旧结果）
    assert fingerprint(code="general", types=("ORG", "PER")) != a


def test_corruptible_backend_protocol_declares_discard():
    """nit-5：discard 是 claimable 后端的可选能力，必须在协议里钉住，
    否则新后端漏实现时坏载荷会静默双抽。"""
    from moray.extraction.cache import ClaimableBackend, CorruptibleBackend

    fake = _ClaimableFakeBackend()
    assert isinstance(fake, ClaimableBackend)
    assert isinstance(fake, CorruptibleBackend)

    class _NoDiscard:
        async def try_claim(self, key, lease_seconds, owner):
            return True

        async def renew_claim(self, key, lease_seconds, owner):
            return True

        async def release(self, key, owner):
            pass

    assert isinstance(_NoDiscard(), ClaimableBackend)
    assert not isinstance(_NoDiscard(), CorruptibleBackend)


# ---------------------------------------------------------------------------
# 第 5 轮评审回归：占位只在异常路径释放（should-1）、采样开关进键（should-2）
# 与 8 条 nit（其中 nit-8 按评审判定继续挂账，不在本单扩大改动面）
# ---------------------------------------------------------------------------
class _SetFailingClaimBackend(_ClaimableFakeBackend):
    """占位接口正常、只有 set 抛错：模拟「抽到了但写不进缓存」。"""

    async def set(self, key, value):
        await asyncio.sleep(0)
        raise RuntimeError("模拟写缓存失败")


@pytest.mark.asyncio
async def test_write_failure_releases_own_claim_without_lease_wait():
    """should-1：写缓存失败后必须立刻释放自己的占位。

    旧实现只在 except 里 release，写失败被 _safe_set 吞掉后占位留到租约过期，
    同键后续请求会白等一个 claim_lease（默认 30s）再重复真调。
    """
    backend = _SetFailingClaimBackend()
    cache_a = ContentResultCache(backend)
    cache_b = ContentResultCache(
        backend, claim_lease_seconds=5.0, remote_wait_seconds=5.0,
        poll_seconds=0.005)
    calls = {"n": 0}

    async def extract():
        calls["n"] += 1
        return _output()

    key = build_cache_key("fp", "m", "写缓存失败")
    out1, hit1 = await cache_a.get_or_extract("fp", "m", "写缓存失败", extract)
    assert (out1.result.entities[0].name, hit1, calls["n"]) == ("华为", False, 1)
    assert key not in backend._claims, "写失败后占位应立刻释放，不能留到租约过期"

    loop = asyncio.get_running_loop()
    started = loop.time()
    out2, hit2 = await cache_b.get_or_extract("fp", "m", "写缓存失败", extract)
    elapsed = loop.time() - started
    assert hit2 is False and calls["n"] == 2
    assert out2.result.entities[0].name == "华为"
    assert elapsed < 0.5, f"同键后续请求白等了 {elapsed:.3f}s 租约"


class _SlowClaimOnceBackend(_ClaimableFakeBackend):
    """第一次 try_claim 先落占位再挂起，复现「在 try_claim 内被取消」。"""

    def __init__(self):
        super().__init__()
        self.claim_entered = asyncio.Event()
        self._slow = True

    async def try_claim(self, key, lease_seconds, owner):
        claimed = await super().try_claim(key, lease_seconds, owner)
        if self._slow:
            self._slow = False
            self.claim_entered.set()
            await asyncio.sleep(30)
        return claimed


@pytest.mark.asyncio
async def test_cancel_inside_try_claim_releases_placeholder():
    """should-1：在 try_claim 内被取消时，已落库的占位也必须释放。

    旧实现 owner 在 _safe_claim 内生成，取消发生在返回之前 → claim_owner 仍是
    None → except 不 release，占位留到租约过期。
    """
    backend = _SlowClaimOnceBackend()
    cache = ContentResultCache(
        backend, claim_lease_seconds=30.0, remote_wait_seconds=30.0,
        poll_seconds=0.005)
    calls = {"n": 0}

    async def extract():
        calls["n"] += 1
        return _output()

    key = build_cache_key("fp", "m", "占位中被取消")
    task = asyncio.create_task(
        cache.get_or_extract("fp", "m", "占位中被取消", extract))
    await asyncio.wait_for(backend.claim_entered.wait(), timeout=1.0)
    assert key in backend._claims          # 占位已落库，owner 令牌还没返回
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert key not in backend._claims, "取消后占位必须释放，不能留到租约过期"
    assert key not in cache._inflight

    loop = asyncio.get_running_loop()
    started = loop.time()
    out, hit = await cache.get_or_extract("fp", "m", "占位中被取消", extract)
    elapsed = loop.time() - started
    assert hit is False and calls["n"] == 1
    assert out.result.entities[0].name == "华为"
    assert elapsed < 0.5, f"取消后同键请求白等了 {elapsed:.3f}s"


def test_extractor_model_identity_covers_thinking_and_reasoning_effort(monkeypatch):
    """should-2：LLM_THINKING / LLM_REASONING_EFFORT 直接改请求体，必须进缓存键。

    它们是运行期 env（改 .env 重启即生效），漏进键会让同一分块长期返回
    「上一个思考档位」的结果，且没有任何日志提示。
    """
    from moray.extraction import extractor as extractor_mod

    settings = {"provider": "volcengine", "base_url": "https://ark.example",
                "chat_model": "same-model",
                "reasoning_effort": None, "thinking": None}
    monkeypatch.setattr(extractor_mod, "get_settings", lambda: dict(settings))
    base = extractor_mod.Extractor("general")

    settings["reasoning_effort"] = "high"
    high = extractor_mod.Extractor("general")
    settings["reasoning_effort"] = None
    settings["thinking"] = False
    no_think = extractor_mod.Extractor("general")

    assert base.model_identity != high.model_identity
    assert base.model_identity != no_think.model_identity
    assert high.model_identity != no_think.model_identity


class _BrokenRetryPromptExtractor(_CountingRealExtractor):
    """领域指纹算不出来（模板渲染抛错）的抽取器。"""

    @staticmethod
    def _retry_prompt(error):
        raise RuntimeError("模板渲染失败")


@pytest.mark.asyncio
async def test_pipeline_degrades_to_no_cache_when_fingerprint_fails(caplog):
    """nit-1：缓存层算指纹失败不能让「开缓存」比「不开缓存」更脆弱。"""
    text = _long_text()
    chunks = _chunks(text)
    Document = __import__("moray.extraction.document", fromlist=["Document"]).Document
    real = _BrokenRetryPromptExtractor(
        DomainSpec(code="general", name="通用",
                   types=(TypeSpec("ORG", "机构", "机构"),)))
    cache = ContentResultCache(InMemoryCacheBackend())
    caplog.set_level("WARNING")
    pipeline = _pipeline(real, cache)

    assert pipeline.result_cache is None
    assert any("降级为不缓存" in r.message for r in caplog.records)

    doc = Document(id="d", text=text, source="t")
    out1 = await pipeline.run(doc, chunks)
    out2 = await pipeline.run(doc, chunks)
    assert real.calls == 2 * len(chunks)   # 退化成普通抽取，每次真调
    assert out1.result.model_dump() == out2.result.model_dump()


class _NoDiscardClaimBackend:
    """满足 ClaimableBackend、但没有 discard 的后端（缺 CorruptibleBackend 能力）。"""

    def __init__(self):
        self._inner = _ClaimableFakeBackend()

    async def get(self, key):
        return await self._inner.get(key)

    async def set(self, key, value):
        await self._inner.set(key, value)

    async def try_claim(self, key, lease_seconds, owner):
        return await self._inner.try_claim(key, lease_seconds, owner)

    async def renew_claim(self, key, lease_seconds, owner):
        return await self._inner.renew_claim(key, lease_seconds, owner)

    async def release(self, key, owner):
        await self._inner.release(key, owner)


@pytest.mark.asyncio
async def test_discard_corrupt_uses_protocol_not_getattr(caplog):
    """nit-2：discard 是协议能力，生产路径必须按 isinstance 判定并留 warning。"""
    from moray.extraction.cache import CorruptibleBackend

    no_discard = _NoDiscardClaimBackend()
    assert not isinstance(no_discard, CorruptibleBackend)
    cache = ContentResultCache(no_discard)
    caplog.set_level("WARNING")
    await cache._discard_corrupt("chunk:deadbeef", "{坏载荷")
    assert any("discard" in r.message for r in caplog.records)

    # 实现了协议的后端：真的按“读到的坏值”删除，且不留 warning。
    backend = _ClaimableFakeBackend()
    key = build_cache_key("fp", "m", "坏值删除")
    backend._store[key] = "{坏载荷"
    caplog.clear()
    await ContentResultCache(backend)._discard_corrupt(key, "{坏载荷")
    assert key not in backend._store
    assert not [r for r in caplog.records if "discard" in r.message]


@pytest.mark.asyncio
async def test_stale_waiter_does_not_steal_new_generation_waiter_count():
    """nit-5：旧代际等待方的 stale finally 不能把新 leader 的计数减到 0。"""
    cache = ContentResultCache(InMemoryCacheBackend())
    key = "chunk:generation"
    loop = asyncio.get_running_loop()
    old_future = loop.create_future()
    new_future = loop.create_future()
    cache._inflight[key] = new_future
    cache._waiters[key] = 2

    cache._drop_waiter(key, old_future)          # 上一代际的 stale finally
    assert cache._waiters[key] == 2
    assert cache._inflight[key] is new_future

    cache._drop_waiter(key, new_future)
    assert cache._waiters[key] == 1
    cache._drop_waiter(key, new_future)
    assert key not in cache._waiters


class _RecordingArgsPool:
    """记录 SQL 与实参的假 asyncpg pool，供 SQL 占位符静态核对。"""

    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []

    async def execute(self, sql, *args):
        self.calls.append((sql, args))
        return None

    async def fetchval(self, sql, *args):
        self.calls.append((sql, args))
        return None


def _assert_placeholders_match_args(sql: str, args: tuple) -> None:
    import re

    numbered = sorted({int(n) for n in re.findall(r"\$(\d+)", sql)})
    assert numbered == list(range(1, len(args) + 1)), (sql, args)


@pytest.mark.asyncio
async def test_postgres_backend_sql_placeholders_match_args():
    """nit-7：PG SQL 在 CI 里零执行，至少要静态钉死占位符编号/个数与实参一致。"""
    from moray.extraction.cache import PostgresCacheBackend

    pool = _RecordingArgsPool()
    backend = PostgresCacheBackend(pool, ttl_hours=1)
    await backend.set("k", "v")
    await backend.try_claim("k", 30.0, "owner-a")
    await backend.renew_claim("k", 30.0, "owner-a")
    await backend.release("k", "owner-a")
    await backend.discard("k", "坏值")
    unclean = PostgresCacheBackend(pool, ttl_hours=1)
    await unclean.purge_expired()

    # set(INSERT) + set 内的首次 purge + try_claim/renew/release/discard/再次 purge
    assert len(pool.calls) == 7
    for sql, args in pool.calls:
        _assert_placeholders_match_args(sql, args)
    # 占位行语义：try_claim 必须复用同一个空载荷参数（$2）做条件，不能错位。
    try_claim_sql = next(
        sql for sql, args in pool.calls if "RETURNING cache_key" in sql
        and "INSERT INTO extraction_result_cache" in sql)
    assert "payload = $2" in try_claim_sql


@pytest.mark.asyncio
async def test_postgres_set_refreshes_created_at_and_purge_protects_lease():
    """nit-6：回写要刷新 created_at；TTL 清理不能删掉仍在续租的占位行。"""
    from moray.extraction.cache import PostgresCacheBackend

    pool = _RecordingArgsPool()
    backend = PostgresCacheBackend(pool, ttl_hours=1)
    await backend.set("k", "v")

    set_sql = next(sql for sql, _ in pool.calls
                   if "SET payload = EXCLUDED.payload" in sql)
    assert "created_at = now()" in set_sql

    purge_sql = next(sql for sql, _ in pool.calls if "DELETE" in sql)
    assert "coalesce(expires_at, created_at)" in purge_sql
