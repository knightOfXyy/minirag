"""敌情线索抽取核心逻辑（统一导入入口的服务端 AI 分流）。

与 summarize.service 共用文档解析缝 `_parse_text`（markitdown 统一解析
pdf/docx/xlsx/txt/md），只替换 system prompt 与结构化 schema：
本模块把一篇文档抽取成「多条敌情线索」，每条 title/reason/level。

LLM 调用收敛在 `_call_threat_model`，测试 monkeypatch 这个缝即可离线验证。
"""

from __future__ import annotations

from pathlib import Path

from agentscope.agent import Agent
from agentscope.message import UserMsg

from ..config import build_chat_model
from .service import ALLOWED_EXTENSIONS, _parse_text
from .threat_schema import ThreatClueList

_SYSTEM_PROMPT = (
    "你是敌情线索分析专家。阅读用户提供的文档全文，从中抽取所有可独立成条的敌情线索，"
    "每条线索给出：\n"
    "- title：一句话线索描述（中文，客观陈述事实，不带编号）；\n"
    "- reason：该线索的判定依据（文档中的具体事实，没有则留空字符串）；\n"
    "- level：风险等级，只能是 高/中/低，无法判断时给 低。\n"
    "只抽取文档中确实出现的内容，不要编造；同一事实不要拆成多条；"
    "若文档不含任何敌情线索，返回空 clues。"
)
_FOCUS_SUFFIX = "本次侧重点：{focus}"
_DOCUMENT_PROMPT = "文档全文：\n{document}\n\n请抽取敌情线索。"


def _threat_system_prompt(focus: str | None) -> str:
    if focus:
        return f"{_SYSTEM_PROMPT}\n{_FOCUS_SUFFIX.format(focus=focus)}"
    return _SYSTEM_PROMPT


async def _call_threat_model(text: str, focus: str | None) -> ThreatClueList:
    """LLM 结构化调用缝：测试 monkeypatch 这里返回假结果。"""
    model = build_chat_model(stream=False)
    agent = Agent(
        name="ThreatExtractor",
        system_prompt=_threat_system_prompt(focus),
        model=model,
    )
    reply = await agent.reply(
        UserMsg(name="user", content=_DOCUMENT_PROMPT.format(document=text)),
        structured_schema=ThreatClueList,
    )
    return ThreatClueList.model_validate(reply.structured_output)


async def extract_threat_clues(
    content: bytes, filename: str, focus: str | None
) -> dict:
    """解析文档并抽取多条敌情线索，返回 {"clues": [{title,reason,level}]}（camelCase）。"""
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError(f"不支持的文件类型: {ext or '(无扩展名)'}")

    text = _parse_text(content, ext)
    if not text.strip():
        raise ValueError("文档解析结果为空，无法抽取线索")

    result = await _call_threat_model(text, focus)
    return result.model_dump(by_alias=True)
