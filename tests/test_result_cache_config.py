"""分块结果内容级缓存的配置开关（默认关闭，不改变现有作业语义）。"""

import pytest

from moray.config import ConfigError, build_result_cache, result_cache_settings


@pytest.fixture()
def _env(monkeypatch):
    monkeypatch.delenv("MORAY_RESULT_CACHE_ENABLED", raising=False)
    monkeypatch.delenv("MORAY_RESULT_CACHE_BACKEND", raising=False)
    monkeypatch.delenv("MORAY_RESULT_CACHE_TTL_HOURS", raising=False)
    monkeypatch.delenv("MORAY_RESULT_CACHE_REMOTE_WAIT_SECONDS", raising=False)
    return monkeypatch


def test_cache_disabled_by_default(_env):
    assert result_cache_settings()["enabled"] is False


def test_cache_opt_in_via_env(_env):
    _env.setenv("MORAY_RESULT_CACHE_ENABLED", "1")
    s = result_cache_settings()
    assert s["enabled"] is True
    assert s["backend"] == "memory"   # 零外部依赖的默认后端


def test_cache_postgres_backend_selectable(_env):
    _env.setenv("MORAY_RESULT_CACHE_ENABLED", "true")
    _env.setenv("MORAY_RESULT_CACHE_BACKEND", "postgres")
    s = result_cache_settings()
    assert s == {
        "enabled": True,
        "backend": "postgres",
        "ttl_hours": 0,
        "remote_wait_seconds": 300.0,
    }


@pytest.mark.parametrize("off", ["0", "false", "no", "off", ""])
def test_cache_explicitly_disabled(_env, off):
    _env.setenv("MORAY_RESULT_CACHE_ENABLED", off)
    assert result_cache_settings()["enabled"] is False


def test_unknown_backend_fails_fast(_env):
    """写错后端名不能静默退回 memory，否则用户以为跨 worker 共享其实是各一份（S7）。"""
    _env.setenv("MORAY_RESULT_CACHE_ENABLED", "1")
    _env.setenv("MORAY_RESULT_CACHE_BACKEND", "postgress")
    with pytest.raises(ConfigError, match="MORAY_RESULT_CACHE_BACKEND"):
        result_cache_settings()


def test_disabled_cache_ignores_invalid_backend_and_ttl(_env):
    """nit-5：回滚时只把 ENABLED=0，残留写错的 BACKEND/TTL 不能挡住启动。"""
    _env.setenv("MORAY_RESULT_CACHE_ENABLED", "0")
    _env.setenv("MORAY_RESULT_CACHE_BACKEND", "postgress")
    _env.setenv("MORAY_RESULT_CACHE_TTL_HOURS", "abc")
    _env.setenv("MORAY_RESULT_CACHE_REMOTE_WAIT_SECONDS", "nan")
    s = result_cache_settings()
    assert s == {"enabled": False}


def test_postgres_backend_requires_pool(_env):
    _env.setenv("MORAY_RESULT_CACHE_ENABLED", "1")
    _env.setenv("MORAY_RESULT_CACHE_BACKEND", "postgres")
    with pytest.raises(ConfigError, match="连接池"):
        import asyncio
        asyncio.run(build_result_cache())


def test_ttl_setting_has_default_and_parses(_env):
    _env.setenv("MORAY_RESULT_CACHE_ENABLED", "1")
    assert result_cache_settings()["ttl_hours"] == 0
    _env.setenv("MORAY_RESULT_CACHE_TTL_HOURS", "12")
    assert result_cache_settings()["ttl_hours"] == 12


@pytest.mark.parametrize("bad", ["12h", "-1", "1.5", "abc"])
def test_invalid_ttl_fails_fast(_env, bad):
    """TTL 非法值不能静默变成 0=永不清理（N5）。"""
    _env.setenv("MORAY_RESULT_CACHE_ENABLED", "1")
    _env.setenv("MORAY_RESULT_CACHE_TTL_HOURS", bad)
    with pytest.raises(ConfigError, match="MORAY_RESULT_CACHE_TTL_HOURS"):
        result_cache_settings()


def test_remote_wait_defaults_and_parses(_env):
    """跨进程等待窗口必须可配置（S2），默认明显高于 p99 单块耗时。"""
    _env.setenv("MORAY_RESULT_CACHE_ENABLED", "1")
    assert result_cache_settings()["remote_wait_seconds"] == 300.0
    _env.setenv("MORAY_RESULT_CACHE_REMOTE_WAIT_SECONDS", "120.5")
    assert result_cache_settings()["remote_wait_seconds"] == 120.5


@pytest.mark.parametrize("bad", ["0", "-5", "nan", "inf", "abc"])
def test_invalid_remote_wait_fails_fast(_env, bad):
    _env.setenv("MORAY_RESULT_CACHE_ENABLED", "1")
    _env.setenv("MORAY_RESULT_CACHE_REMOTE_WAIT_SECONDS", bad)
    with pytest.raises(
            ConfigError, match="MORAY_RESULT_CACHE_REMOTE_WAIT_SECONDS"):
        result_cache_settings()


def test_build_result_cache_passes_remote_wait_through(_env):
    import asyncio

    _env.setenv("MORAY_RESULT_CACHE_ENABLED", "1")
    _env.setenv("MORAY_RESULT_CACHE_REMOTE_WAIT_SECONDS", "42.5")
    cache = asyncio.run(build_result_cache())
    assert cache is not None
    assert cache.remote_wait_seconds == 42.5


async def test_schema_failure_degrades_to_no_cache(monkeypatch):
    """nit-6：缓存建表失败不能拖垮 API/worker 启动，只降级为不缓存。"""
    import moray.config as config_mod

    class _BrokenCache:
        async def init_schema(self):
            raise RuntimeError("DDL 权限不足")

    async def fake_build(settings=None, *, pool=None):
        return _BrokenCache()

    monkeypatch.setattr(config_mod, "build_result_cache", fake_build)
    assert await config_mod.build_result_cache_safe() is None


async def test_schema_success_keeps_cache(monkeypatch):
    import moray.config as config_mod

    class _OkCache:
        inited = False

        async def init_schema(self):
            self.inited = True

    ok = _OkCache()

    async def fake_build(settings=None, *, pool=None):
        return ok

    monkeypatch.setattr(config_mod, "build_result_cache", fake_build)
    assert await config_mod.build_result_cache_safe() is ok
    assert ok.inited is True


@pytest.mark.parametrize("bad", ["1", "0.5", "29.9"])
def test_remote_wait_shorter_than_claim_lease_fails_fast(_env, bad):
    """nit-2：等待窗口短于一个租约时，等待方会在健康 leader 续租前就超时自抽，
    验收 5「同块只一次真调」必然失效。非法值启动即报错（与本文件约定一致）。"""
    _env.setenv("MORAY_RESULT_CACHE_ENABLED", "1")
    _env.setenv("MORAY_RESULT_CACHE_REMOTE_WAIT_SECONDS", bad)
    with pytest.raises(
            ConfigError, match="MORAY_RESULT_CACHE_REMOTE_WAIT_SECONDS"):
        result_cache_settings()


def test_remote_wait_equal_to_claim_lease_is_allowed(_env):
    """nit-2：等于一个租约是下限，允许。"""
    _env.setenv("MORAY_RESULT_CACHE_ENABLED", "1")
    _env.setenv("MORAY_RESULT_CACHE_REMOTE_WAIT_SECONDS", "30")
    assert result_cache_settings()["remote_wait_seconds"] == 30.0


def _install_fake_api_deps(monkeypatch):
    """把 build_deps 里的重资源替身掉，只验证 env → AppDeps 的装配。"""
    from types import SimpleNamespace

    import moray.api.server as server_mod
    import moray.config_center as config_center_mod

    class _Pool:
        async def execute(self, *args, **kwargs):
            return None

        async def fetchval(self, *args, **kwargs):
            return None

    class _EntityStore:
        def __init__(self, pool):
            self.pool = pool

        async def init_schema(self):
            pass

    class _KnowledgeBase:
        def __init__(self, store):
            self.store = store

    class _Ingestor:
        def __init__(self, store):
            self.store = store

    class _JobStore:
        def __init__(self, pool):
            self.pool = pool

        async def init_schema(self):
            pass

    class _Dashboard:
        def __init__(self, pool):
            self.pool = pool

        async def init_schema(self):
            pass

    class _Resolver:
        def __init__(self, pool):
            self.pool = pool
            self.store = SimpleNamespace(seed_builtin=self._seed)

        async def init_schema(self):
            pass

        async def _seed(self):
            pass

    async def _fake_init_pool():
        return _Pool()

    monkeypatch.setattr(server_mod, "init_pool", _fake_init_pool)
    monkeypatch.setattr(server_mod, "EntityStore", _EntityStore)
    monkeypatch.setattr(server_mod, "KnowledgeBase", _KnowledgeBase)
    monkeypatch.setattr(server_mod, "Ingestor", _Ingestor)
    monkeypatch.setattr(server_mod, "JobStore", _JobStore)
    monkeypatch.setattr(server_mod, "Dashboard", _Dashboard)
    monkeypatch.setattr(config_center_mod, "DbDomainResolver", _Resolver)
    return server_mod


async def test_build_deps_wires_result_cache_from_env(_env, monkeypatch):
    """nit-6：MORAY_RESULT_CACHE_ENABLED=1 时 build_deps 真的构造出
    result_cache 并放进 AppDeps；关闭时为 None。"""
    from moray.extraction.cache import ContentResultCache

    server_mod = _install_fake_api_deps(monkeypatch)

    _env.setenv("MORAY_RESULT_CACHE_ENABLED", "1")
    deps_on = await server_mod.build_deps()
    assert isinstance(deps_on.result_cache, ContentResultCache)

    _env.setenv("MORAY_RESULT_CACHE_ENABLED", "0")
    deps_off = await server_mod.build_deps()
    assert deps_off.result_cache is None


async def test_worker_build_runner_wires_result_cache_from_env(_env, monkeypatch):
    """nit-6：worker 的 _build_runner 也必须把 result_cache 传给
    build_document_handler；漏传时缓存测试全绿但线上 worker 不缓存。"""
    from types import SimpleNamespace

    import moray.jobs.worker as worker_mod
    from moray.extraction.cache import ContentResultCache

    captured = {}

    async def fake_build_handler(**kwargs):
        captured.update(kwargs)
        return object()

    async def _fake_init_pool():
        return SimpleNamespace(close=_noop)

    async def _noop(*args, **kwargs):
        return None

    class _Store:
        def __init__(self, pool):
            pass

        async def init_schema(self):
            pass

    class _Jobs:
        def __init__(self, pool):
            pass

        async def init_schema(self):
            pass

    class _Ingestor:
        def __init__(self, store):
            pass

    monkeypatch.setattr(worker_mod, "init_pool", _fake_init_pool)
    monkeypatch.setattr(worker_mod, "EntityStore", _Store)
    monkeypatch.setattr(worker_mod, "JobStore", _Jobs)
    monkeypatch.setattr(worker_mod, "Ingestor", _Ingestor)
    monkeypatch.setattr(worker_mod, "build_document_handler", fake_build_handler)

    _env.setenv("MORAY_RESULT_CACHE_ENABLED", "1")
    _runner, _pool = await worker_mod._build_runner(
        worker_id="w-test", concurrency=1, lease_seconds=30,
        offline=True, use_dashboard=False)
    assert isinstance(captured["result_cache"], ContentResultCache)

    captured.clear()
    _env.setenv("MORAY_RESULT_CACHE_ENABLED", "0")
    await worker_mod._build_runner(
        worker_id="w-test", concurrency=1, lease_seconds=30,
        offline=True, use_dashboard=False)
    assert captured["result_cache"] is None
