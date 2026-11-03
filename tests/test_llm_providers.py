"""多供应商接入（LLM_PROVIDER）的配置层测试：全部离线，不发任何请求。

覆盖三件事：
1. 选对类：provider 决定用 agentscope 的哪对 Credential/ChatModel；
2. 参数翻译：中性的 LLM_REASONING_EFFORT / LLM_THINKING 在各家语义正确，
   非法取值在构造期就报错（而不是第一次调用时炸在请求体里）；
3. 报错可操作：缺 Key / 未知 provider / 少 base_url 时，信息里就带着怎么改。
"""

import pytest

from moray import config
from moray.config import (
    ConfigError,
    build_chat_model,
    describe,
    get_settings,
)


@pytest.fixture()
def env(monkeypatch):
    """干净的环境变量 + 每次前后清缓存，避免用例互相污染。"""
    for var in ("LLM_PROVIDER", "LLM_REASONING_EFFORT", "LLM_THINKING",
                "LLM_TEMPERATURE",
                "ARK_REASONING_EFFORT", "ARK_THINKING",
                "ARK_API_KEY", "ARK_BASE_URL", "ARK_CHAT_MODEL",
                "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_CHAT_MODEL",
                "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_CHAT_MODEL"):
        monkeypatch.delenv(var, raising=False)
    # 默认给一把假的 ARK Key：config 在 import 时 load_dotenv 会带进真 Key，
    # 测试不该依赖它，也不该把真 Key 传进断言。
    monkeypatch.setenv("ARK_API_KEY", "ark-fake-key-for-tests-0000")
    config.get_settings.cache_clear()
    yield monkeypatch
    config.get_settings.cache_clear()


# ---------------------------------------------------------------------------
# 1. 选对类
# ---------------------------------------------------------------------------
def test_default_provider_is_volcengine_unchanged(env):
    """不设 LLM_PROVIDER = 火山方舟，行为与接入多供应商之前完全一致。"""
    s = get_settings()
    assert s["provider"] == "volcengine"
    assert s["chat_model"] == "deepseek-v4-flash-ga"      # 沿用原默认名
    assert s["base_url"].endswith("/api/coding/v3")
    from agentscope.model import VolcengineChatModel

    model = build_chat_model(stream=False)
    # 工单 01：构造出的是 InstrumentedVolcengineChatModel（观测埋点子类），
    # 行为同构于基类，用 isinstance 断言"选对了 provider 家族"。
    assert isinstance(model, VolcengineChatModel)
    assert type(model).__name__ == "InstrumentedVolcengineChatModel"
    assert model.stream is False
    # 没配开关时不往请求体里塞任何新字段
    assert model.parameters.model_dump(exclude_none=True) == {}


def test_deepseek_provider_builds_deepseek_model(env):
    env.setenv("LLM_PROVIDER", "deepseek")
    env.setenv("DEEPSEEK_API_KEY", "sk-fake-deepseek-key-0000")
    s = get_settings()
    assert s["base_url"] == "https://api.deepseek.com"    # 官方默认，无需手填
    assert s["chat_model"] == "deepseek-flash"            # 2026 新模型名
    from agentscope.model import DeepSeekChatModel

    model = build_chat_model(stream=False)
    assert isinstance(model, DeepSeekChatModel)
    assert type(model).__name__ == "InstrumentedDeepSeekChatModel"
    assert model.model == "deepseek-flash"


def test_openai_provider_requires_base_url_and_model(env):
    env.setenv("LLM_PROVIDER", "openai")
    env.setenv("OPENAI_API_KEY", "sk-fake")
    with pytest.raises(ConfigError, match="OPENAI_BASE_URL"):
        get_settings()                      # 故意不给默认值：别静默打到官方计费
    env.setenv("OPENAI_BASE_URL", "http://localhost:8000/v1")
    env.setenv("OPENAI_CHAT_MODEL", "some-served-model")
    from agentscope.model import OpenAIChatModel

    model = build_chat_model()
    assert isinstance(model, OpenAIChatModel)
    assert type(model).__name__ == "InstrumentedOpenAIChatModel"


def test_temperature_is_opt_in_and_recordable(env):
    """A/B 需要固定 temperature；未配置时不往请求体塞字段（默认行为不变）。"""
    assert get_settings()["temperature"] is None
    assert build_chat_model().parameters.model_dump(exclude_none=True) == {}

    env.setenv("LLM_TEMPERATURE", "0")
    get_settings.cache_clear()
    assert get_settings()["temperature"] == 0.0
    assert build_chat_model().parameters.temperature == 0.0

    env.setenv("LLM_TEMPERATURE", "3")
    get_settings.cache_clear()
    with pytest.raises(ConfigError, match="LLM_TEMPERATURE"):
        get_settings()


def test_missing_key_names_the_variable_to_set(env):
    env.setenv("LLM_PROVIDER", "deepseek")
    with pytest.raises(ConfigError, match="DEEPSEEK_API_KEY"):
        get_settings()


def test_unknown_provider_is_rejected(env):
    env.setenv("LLM_PROVIDER", "ollama")
    with pytest.raises(ConfigError, match="LLM_PROVIDER"):
        get_settings()


def test_llm_prefix_wins_over_ark_alias(env):
    """昨天还叫 ARK_*，今天中性名 LLM_* 优先、旧名继续可用。"""
    env.setenv("ARK_REASONING_EFFORT", "low")
    env.setenv("LLM_REASONING_EFFORT", "medium")
    assert get_settings()["reasoning_effort"] == "medium"
    env.delenv("LLM_REASONING_EFFORT")
    get_settings.cache_clear()
    assert get_settings()["reasoning_effort"] == "low"


# ---------------------------------------------------------------------------
# 2. reasoning_effort / thinking 的按厂商翻译
# ---------------------------------------------------------------------------
def test_deepseek_effort_mapping(env):
    env.setenv("LLM_PROVIDER", "deepseek")
    env.setenv("DEEPSEEK_API_KEY", "sk-fake-deepseek-key-0000")

    env.setenv("LLM_REASONING_EFFORT", "low")        # DeepSeek 无 low → high
    get_settings.cache_clear()
    assert get_settings()["reasoning_effort"] == "high"

    env.setenv("LLM_REASONING_EFFORT", "xhigh")      # → max
    get_settings.cache_clear()
    assert get_settings()["reasoning_effort"] == "max"

    env.setenv("LLM_REASONING_EFFORT", "minimal")    # minimal = 别思考
    get_settings.cache_clear()
    s = get_settings()
    assert s["reasoning_effort"] is None and s["thinking"] is False
    assert build_chat_model().parameters.thinking_enable is False


def test_deepseek_rejects_effort_it_cannot_send(env):
    """minimal 对 volcengine 合法，对 DeepSeek 只能翻成关闭思考；
    而 medium 之类映射不到的一律拦下，避免 agentscope 的 Literal 在构造期炸。"""
    env.setenv("LLM_PROVIDER", "deepseek")
    env.setenv("DEEPSEEK_API_KEY", "sk-fake-deepseek-key-0000")
    env.setenv("LLM_REASONING_EFFORT", "ultra")
    get_settings.cache_clear()
    with pytest.raises(ConfigError, match="high/max"):
        get_settings()


def test_conflicting_switches_are_caught(env):
    env.setenv("LLM_PROVIDER", "deepseek")
    env.setenv("DEEPSEEK_API_KEY", "sk-fake-deepseek-key-0000")
    env.setenv("LLM_REASONING_EFFORT", "minimal")
    env.setenv("LLM_THINKING", "enabled")
    get_settings.cache_clear()
    with pytest.raises(ConfigError, match="冲突"):
        get_settings()


def test_thinking_explicit_disabled_builds_parameters(env):
    env.setenv("LLM_THINKING", "disabled")
    s = get_settings()
    assert s["thinking"] is False
    assert build_chat_model().parameters.thinking_enable is False


# ---------------------------------------------------------------------------
# 3. describe()/自检 CLI 不能泄密
# ---------------------------------------------------------------------------
def test_describe_never_leaks_the_key(env):
    d = describe()
    assert "ark-fake-key-for-tests-0000" not in str(d)
    assert d["api_key_masked"].startswith("ark")
    assert "…" in d["api_key_masked"]
    assert set(d) == {"provider", "base_url", "chat_model", "api_key_masked",
                      "reasoning_effort", "thinking"}


def test_llm_check_error_translator_is_actionable(env):
    """自检 CLI 把网关错误翻译成人话：401/404/429 各给下一步。"""
    from moray.llm_check import _explain_error

    assert "API Key" in _explain_error(Exception("Error code: 401 Unauthorized"))
    assert "deepseek-flash" in _explain_error(
        Exception("Error code: 404 Model Not Found"))
    assert "额度" in _explain_error(
        Exception("Error code: 429 AccountQuotaExceeded"))
