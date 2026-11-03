"""分块结果内容级缓存的配置开关（默认关闭，不改变现有作业语义）。"""

import pytest

from moray.config import ConfigError, build_result_cache, result_cache_settings


@pytest.fixture()
def _env(monkeypatch):
    monkeypatch.delenv("MORAY_RESULT_CACHE_ENABLED", raising=False)
    monkeypatch.delenv("MORAY_RESULT_CACHE_BACKEND", raising=False)
    monkeypatch.delenv("MORAY_RESULT_CACHE_TTL_HOURS", raising=False)
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
    assert s == {"enabled": True, "backend": "postgres", "ttl_hours": 0}


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
