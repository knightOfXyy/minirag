"""集中配置：多供应商 LLM 接入 + 构造聊天模型客户端。

全项目只在这里读环境变量、只在这里决定"用哪家模型"，其他模块通过
build_chat_model() 拿模型，因此换供应商不改业务代码：

    LLM_PROVIDER=volcengine   # 默认，火山方舟 ARK（行为与历史完全一致）
    LLM_PROVIDER=deepseek     # DeepSeek 官方 API
    LLM_PROVIDER=openai       # 任意 OpenAI 兼容网关（自建/第三方代理）

三家在 agentscope 里构造签名一致（credential, model, parameters, stream），
差异都收敛在本文件的"参数翻译层"：不同厂商支持的 reasoning_effort 取值不同、
思考模式的开关形态不同，这里统一成 LLM_REASONING_EFFORT / LLM_THINKING 两个
中性变量，非法取值在启动时就报错，而不是等到第一次调用炸在请求体里。
"""

from __future__ import annotations

import os
from functools import lru_cache

from dotenv import load_dotenv

# 包根目录的上两级 = 项目根（src/moray/config.py -> 项目根）
_PROJECT_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", ".."),
)
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))

PROVIDERS = ("volcengine", "deepseek", "openai")

# 各 provider 允许的 reasoning_effort（agentscope 的 Literal 约束为准）。
# deepseek 只有 high/max：agentscope 的 Parameters 是 Literal["high","max"]，
# 传 low 会在构造期抛 ValidationError，所以这里先做兼容映射。
_EFFORT_ALLOWED = {
    "volcengine": ("minimal", "low", "medium", "high"),
    "deepseek": ("high", "max"),
    "openai": ("minimal", "low", "medium", "high"),
}
# 把用户习惯的档位翻译成该厂商真实存在的档位
_EFFORT_ALIAS = {
    "deepseek": {
        "low": "high",       # DeepSeek 只有 high/max，low 语义上靠 high
        "medium": "high",
        "xhigh": "max",
        # minimal 不是"少想一点"而是"别想"，翻成关闭思考模式，见下面处理
        "minimal": None,
    },
}

_THINKING_VALUES = ("auto", "enabled", "disabled")


class ConfigError(RuntimeError):
    """配置不合法：启动即失败，错误信息里直接给出该怎么改。"""


def _first_env(*names: str) -> str:
    """取第一个非空的环境变量（用于 LLM_* 优先、ARK_* 兼容）。"""
    for n in names:
        v = os.environ.get(n)
        if v is not None and v.strip():
            return v.strip()
    return ""


def _mask(secret: str) -> str:
    """打码展示密钥：只露头尾，中间用长度提示。"""
    if len(secret) <= 8:
        return "*（过短，已隐藏）"
    return f"{secret[:3]}…{secret[-4:]}（{len(secret)} 位）"


def _resolve_switches(provider: str, effort: str, thinking: str) -> dict:
    """把中性的 LLM_REASONING_EFFORT / LLM_THINKING 翻译成厂商参数。

    返回 {"reasoning_effort": str|None, "thinking": bool|None}，
    None 一律表示"不往请求体里塞，沿用厂商默认"。
    """
    thinking = thinking.lower()
    if thinking and thinking not in _THINKING_VALUES:
        raise ConfigError(
            f"LLM_THINKING={thinking!r} 不合法，只能是："
            f"{'/'.join(_THINKING_VALUES)}（留空或 auto=沿用默认）"
        )
    thinking_bool = None if thinking in ("", "auto") else thinking == "enabled"

    effort = effort.lower()
    if not effort:
        return {"reasoning_effort": None, "thinking": thinking_bool}

    allowed = _EFFORT_ALLOWED[provider]
    alias = _EFFORT_ALIAS.get(provider, {})
    if effort in alias:                      # 兼容映射（含 minimal→关闭思考）
        mapped = alias[effort]
        if mapped is None:                   # minimal：对 DeepSeek = 别思考
            if thinking_bool is True:
                raise ConfigError(
                    "LLM_REASONING_EFFORT=minimal 表示不思考，和 "
                    "LLM_THINKING=enabled 冲突；请把 LLM_THINKING 设为 "
                    "disabled 或删掉 EFFORT。"
                )
            return {"reasoning_effort": None, "thinking": False}
        effort = mapped

    if effort not in allowed:
        raise ConfigError(
            f"provider={provider} 不支持 LLM_REASONING_EFFORT={effort!r}，"
            f"可选：{'/'.join(allowed)}（留空=不传该参数）"
        )
    return {"reasoning_effort": effort, "thinking": thinking_bool}


@lru_cache(maxsize=1)
def get_settings() -> dict:
    """读取一次配置并缓存。密钥缺失/取值非法都在这里立刻失败。"""
    provider = (_first_env("LLM_PROVIDER") or "volcengine").lower()
    if provider not in PROVIDERS:
        raise ConfigError(
            f"LLM_PROVIDER={provider!r} 未知，可选：{'/'.join(PROVIDERS)}"
        )

    if provider == "deepseek":
        api_key = _first_env("DEEPSEEK_API_KEY")
        base_url = _first_env("DEEPSEEK_BASE_URL") or "https://api.deepseek.com"
        chat_model = _first_env("DEEPSEEK_CHAT_MODEL") or "deepseek-flash"
        key_var = "DEEPSEEK_API_KEY"
    elif provider == "openai":
        api_key = _first_env("OPENAI_API_KEY")
        # OpenAI 兼容网关的 base_url 必须显式给（自建/vLLM/第三方代理各不同），
        # 故意不给默认值：静默打到 api.openai.com 更容易烧错钱。
        base_url = _first_env("OPENAI_BASE_URL")
        chat_model = _first_env("OPENAI_CHAT_MODEL")
        key_var = "OPENAI_API_KEY"
        if not base_url or not chat_model:
            raise ConfigError(
                "LLM_PROVIDER=openai 需要同时设置 OPENAI_BASE_URL 与 "
                "OPENAI_CHAT_MODEL（本文件不提供默认值，避免误连官方计费）"
            )
    else:  # volcengine：与历史行为逐字一致
        api_key = _first_env("ARK_API_KEY")
        base_url = (
            _first_env("ARK_BASE_URL")
            or "https://ark.cn-beijing.volces.com/api/coding/v3"
        )
        chat_model = _first_env("ARK_CHAT_MODEL") or "deepseek-v4-flash-ga"
        key_var = "ARK_API_KEY"

    if not api_key:
        raise ConfigError(
            f"未找到 {key_var}（当前 LLM_PROVIDER={provider}）。"
            f"请在项目根 .env 里设置 {key_var}=你的密钥，"
            "或把 LLM_PROVIDER 改成你有密钥的那家。"
        )

    switches = _resolve_switches(
        provider,
        _first_env("LLM_REASONING_EFFORT", "ARK_REASONING_EFFORT"),
        _first_env("LLM_THINKING", "ARK_THINKING"),
    )
    return {
        "provider": provider,
        "api_key": api_key,
        "base_url": base_url,
        "chat_model": chat_model,
        **switches,
    }


def describe() -> dict:
    """当前接的是谁（密钥打码），给 /healthz 和自检 CLI 用。"""
    s = get_settings()
    return {
        "provider": s["provider"],
        "base_url": s["base_url"],
        "chat_model": s["chat_model"],
        "api_key_masked": _mask(s["api_key"]),
        "reasoning_effort": s["reasoning_effort"],
        "thinking": s["thinking"],
    }


# provider -> (延迟导入路径, 取类的方式)
def _provider_classes(provider: str):
    """按 provider 取 agentscope 的 Credential / ChatModel 类。

    延迟导入：只用一家时不必把三家的依赖都拉起来，也让离线单测更快。
    """
    if provider == "deepseek":
        from agentscope.credential import DeepSeekCredential
        from agentscope.model import DeepSeekChatModel
        return DeepSeekCredential, DeepSeekChatModel
    if provider == "openai":
        from agentscope.credential import OpenAICredential
        from agentscope.model import OpenAIChatModel
        return OpenAICredential, OpenAIChatModel
    from agentscope.credential import VolcengineCredential
    from agentscope.model import VolcengineChatModel
    return VolcengineCredential, VolcengineChatModel


def provider_classes():
    """返回当前 provider 的 (Credential 类, ChatModel 类)，给教学与自检用。

    L01 想让学生看清"接入一家 API 就是这两样东西"，所以手工展开一次；
    生产代码请直接用它下面的 build_chat_model()。
    """
    return _provider_classes(get_settings()["provider"])


def _int_env(name: str, default: int) -> int:
    raw = _first_env(name)
    return int(raw) if raw.isdigit() and int(raw) > 0 else default


def _bool_env(name: str, default: bool = False) -> bool:
    """读布尔开关：1/true/yes/on 视为开，其余（含未设置）为关。"""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def local_backfill_enabled() -> bool:
    """结构化输出瘦身 + mention/span/关系证据本地确定性回填开关（默认关）。

    MORAY_LOCAL_BACKFILL=1 打开后：模型只输出逻辑实体/关系（无字符偏移、无
    自报置信度），出现位置由本地多模式自动机回填。放量门槛（macro-F1 跌
    ≤0.5pt、关系 F1 不降、输出 token -≥30%、截断不增）达成前保持默认关闭。
    """
    return _bool_env("MORAY_LOCAL_BACKFILL", False)


def chunk_settings() -> dict:
    """长文分块阈值与并发（波次 4 双路接线用）。env 可覆盖，缺省走保守值。

    MORAY_CHUNK_THRESHOLD_UNITS：文本估算单位超过它才分块（≤ 则整篇走旧闭环）。
    MORAY_CHUNK_MAX_UNITS / _OVERLAP：分块预算（与 Chunker 默认一致）。
    MORAY_CHUNK_CONCURRENCY：块级并发上限（对单条 /extract 也生效）。
    """
    return {
        "threshold_units": _int_env("MORAY_CHUNK_THRESHOLD_UNITS", 4000),
        "max_units": _int_env("MORAY_CHUNK_MAX_UNITS", 1500),
        "overlap_units": _int_env("MORAY_CHUNK_OVERLAP_UNITS", 200),
        "concurrency": _int_env("MORAY_CHUNK_CONCURRENCY", 4),
    }


# 视为“开启”的取值；其余（含 0/false/no/off/空）一律关闭。
_ENABLED_WORDS = {"1", "true", "yes", "on"}


def result_cache_settings() -> dict:
    """分块抽取结果【内容级缓存】开关（工单 05）。

    纯加速层，默认【关闭】——不配任何东西时作业语义与历史逐字节一致。
      MORAY_RESULT_CACHE_ENABLED：置真（1/true/yes/on）才启用。
      MORAY_RESULT_CACHE_BACKEND：存储后端：
        · memory（缺省）：进程内、零外部依赖，单飞在这一层生效；
        · postgres：复用作业库（extraction_result_cache 表），跨 worker
          副本/重启共享缓存内容（跨进程强一致单飞不做保证，至多多抽一次）。
    """
    enabled = _first_env("MORAY_RESULT_CACHE_ENABLED").lower() in _ENABLED_WORDS
    backend = _first_env("MORAY_RESULT_CACHE_BACKEND") or "memory"
    return {"enabled": enabled, "backend": backend}


async def build_result_cache(settings: dict | None = None, *, pool=None):
    """按配置构造分块结果缓存；关闭或后端不可用时返回 None（= 完全不缓存）。

    memory 后端零依赖即可用；postgres 后端需传入 asyncpg 连接池并建表，
    建表/初始化失败时安全退回 memory（保证服务可用、不改变抽取正确性）。
    """
    s = settings or result_cache_settings()
    if not s.get("enabled"):
        return None
    backend_name = s.get("backend", "memory")
    from .extraction.cache import ContentResultCache, InMemoryCacheBackend

    if backend_name == "postgres" and pool is not None:
        from .extraction.cache import PostgresCacheBackend

        pg = PostgresCacheBackend(pool)
        try:
            await pg.init_schema()
            return ContentResultCache(pg)
        except Exception:  # noqa: BLE001 缓存是加速层：建表失败退回内存，不阻断服务
            import logging

            logging.getLogger(__name__).warning(
                "抽取结果缓存 postgres 后端初始化失败，退回进程内内存后端",
                exc_info=True)
    return ContentResultCache(InMemoryCacheBackend())


def build_chat_model(*, stream: bool = False):
    """按 LLM_PROVIDER 构造聊天模型客户端（默认非流式，便于结构化抽取）。"""
    s = get_settings()
    cred_cls, model_cls = _provider_classes(s["provider"])

    # 只在显式配置时才构造 Parameters：各模型层的每个参数都是 None 才不写进
    # 请求体，保证默认路径与"没这些开关时"逐字节一致。
    param_kwargs: dict = {}
    if s["reasoning_effort"]:
        param_kwargs["reasoning_effort"] = s["reasoning_effort"]
    if s["thinking"] is not None:
        param_kwargs["thinking_enable"] = s["thinking"]
    parameters = (
        model_cls.Parameters(**param_kwargs) if param_kwargs else None
    )

    return model_cls(
        credential=cred_cls(api_key=s["api_key"], base_url=s["base_url"]),
        model=s["chat_model"],
        parameters=parameters,
        stream=stream,
    )
