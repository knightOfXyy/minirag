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


@lru_cache(maxsize=1)
def get_settings() -> dict:
    """读取一次配置并缓存（API Key 缺失时给出可操作的报错）。"""
    api_key = os.environ.get("ARK_API_KEY")
    if not api_key:
        raise RuntimeError(
            "未找到 ARK_API_KEY。请复制 .env.example 为 .env 并填入密钥。"
        )
    return {
        "api_key": api_key,
        "base_url": os.environ.get(
            "ARK_BASE_URL",
            "https://ark.cn-beijing.volces.com/api/coding/v3",
        ),
        "chat_model": os.environ.get("ARK_CHAT_MODEL", "deepseek-v4-flash-ga"),
    }


def build_chat_model(*, stream: bool = False) -> VolcengineChatModel:
    """构造一个火山方舟聊天模型客户端（默认非流式，便于结构化抽取）。"""
    s = get_settings()
    credential = VolcengineCredential(
        api_key=s["api_key"],
        base_url=s["base_url"],
    )
    return VolcengineChatModel(
        credential=credential,
        model=s["chat_model"],
        stream=stream,
    )
