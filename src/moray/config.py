"""集中配置：从环境变量读取 ARK 连接信息，构造模型客户端。

全项目只在这里直接读环境变量，其他模块通过函数拿模型，
便于以后切换供应商、注入测试用的假模型。
"""

from __future__ import annotations

import os
from functools import lru_cache

from dotenv import load_dotenv

from agentscope.credential import VolcengineCredential
from agentscope.model import VolcengineChatModel

# 包根目录的上两级 = 项目根（src/moray/config.py -> 项目根）
_PROJECT_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", ".."),
)
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))


# reasoning_effort 的合法档位（与 agentscope VolcengineChatModel.Parameters 一致）
REASONING_EFFORTS = ("minimal", "low", "medium", "high")


@lru_cache(maxsize=1)
def get_settings() -> dict:
    """读取一次配置并缓存（API Key 缺失时给出可操作的报错）。"""
    api_key = os.environ.get("ARK_API_KEY")
    if not api_key:
        raise RuntimeError(
            "未找到 ARK_API_KEY。请复制 .env.example 为 .env 并填入密钥。"
        )
    effort = os.environ.get("ARK_REASONING_EFFORT", "").strip().lower()
    if effort and effort not in REASONING_EFFORTS:
        raise RuntimeError(
            f"ARK_REASONING_EFFORT={effort!r} 不合法，可选："
            f"{'/'.join(REASONING_EFFORTS)}（留空=沿用网关默认）"
        )
    thinking = os.environ.get("ARK_THINKING", "").strip().lower()
    if thinking and thinking not in ("auto", "enabled", "disabled"):
        raise RuntimeError(
            "ARK_THINKING 只能是 auto/enabled/disabled（留空或 auto=网关默认）"
        )
    return {
        "api_key": api_key,
        "base_url": os.environ.get(
            "ARK_BASE_URL",
            "https://ark.cn-beijing.volces.com/api/coding/v3",
        ),
        "chat_model": os.environ.get("ARK_CHAT_MODEL", "deepseek-v4-flash-ga"),
        # 抽取任务不需要思考链。deepseek-v4-flash 默认 auto 会想很多（实测一次
        # 简单问答也烧 458~796 reasoning token），而输出单价远高于输入。
        # 留空 = 完全不传该参数，沿用网关默认，行为与改动前一致。
        "reasoning_effort": effort or None,
        "thinking": None if thinking in ("", "auto") else thinking == "enabled",
    }


def build_chat_model(*, stream: bool = False) -> VolcengineChatModel:
    """构造一个火山方舟聊天模型客户端（默认非流式，便于结构化抽取）。"""
    s = get_settings()
    credential = VolcengineCredential(
        api_key=s["api_key"],
        base_url=s["base_url"],
    )
    # 只在显式配置时才构造 Parameters：None 让 agentscope 不往请求体里塞任何
    # 新字段，保证默认路径与历史完全一致。
    param_kwargs = {}
    if s["reasoning_effort"]:
        param_kwargs["reasoning_effort"] = s["reasoning_effort"]
    if s["thinking"] is not None:
        param_kwargs["thinking_enable"] = s["thinking"]
    parameters = (
        VolcengineChatModel.Parameters(**param_kwargs) if param_kwargs else None
    )
    return VolcengineChatModel(
        credential=credential,
        model=s["chat_model"],
        parameters=parameters,
        stream=stream,
    )
