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


def test_postgres_backend_requires_pool(_env):
    _env.setenv("MORAY_RESULT_CACHE_ENABLED", "1")
    _env.setenv("MORAY_RESULT_CACHE_BACKEND", "postgres")
    with pytest.raises(ConfigError, match="连接池"):
        import asyncio
        asyncio.run(build_result_cache())


def test_ttl_setting_has_default_and_parses(_env):
    assert result_cache_settings()["ttl_hours"] == 0
    _env.setenv("MORAY_RESULT_CACHE_TTL_HOURS", "12")
    assert result_cache_settings()["ttl_hours"] == 12


@pytest.mark.parametrize("bad", ["12h", "-1", "1.5", "abc"])
def test_invalid_ttl_fails_fast(_env, bad):
    """TTL 非法值不能静默变成 0=永不清理（N5）。"""
    _env.setenv("MORAY_RESULT_CACHE_TTL_HOURS", bad)
    with pytest.raises(ConfigError, match="MORAY_RESULT_CACHE_TTL_HOURS"):
        result_cache_settings()


def test_remote_wait_defaults_and_parses(_env):
    """跨进程等待窗口必须可配置（S2），默认明显高于 p99 单块耗时。"""
    assert result_cache_settings()["remote_wait_seconds"] == 300.0
    _env.setenv("MORAY_RESULT_CACHE_REMOTE_WAIT_SECONDS", "120.5")
    assert result_cache_settings()["remote_wait_seconds"] == 120.5


@pytest.mark.parametrize("bad", ["0", "-5", "nan", "inf", "abc"])
def test_invalid_remote_wait_fails_fast(_env, bad):
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
