"""分块结果内容级缓存的配置开关（默认关闭，不改变现有作业语义）。"""

import pytest

from moray.config import result_cache_settings


@pytest.fixture()
def _env(monkeypatch):
    monkeypatch.delenv("MORAY_RESULT_CACHE_ENABLED", raising=False)
    monkeypatch.delenv("MORAY_RESULT_CACHE_BACKEND", raising=False)
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
    assert s == {"enabled": True, "backend": "postgres"}


@pytest.mark.parametrize("off", ["0", "false", "no", "off", ""])
def test_cache_explicitly_disabled(_env, off):
    _env.setenv("MORAY_RESULT_CACHE_ENABLED", off)
    assert result_cache_settings()["enabled"] is False
