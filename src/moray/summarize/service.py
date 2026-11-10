"""文档总结核心逻辑（ADR 0004）。

解析 → prompt → 结构化输出收敛为一个独立 service 函数，API 端点只做薄封装。
这是"先同步后异步"的切换缝：未来接 jobs 时把 `summarize_document` 原样接入
BatchRunner handler，HTTP 契约不动。
"""

from __future__ import annotations

from pathlib import Path

from agentscope.agent import Agent
from agentscope.message import UserMsg

from ..config import build_chat_model
from .schemas import SummarizeResult

ALLOWED_EXTENSIONS = {".pdf", ".docx", ".xlsx", ".txt", ".md"}
_TEXT_EXTENSIONS = {".txt", ".md"}

_SYSTEM_PROMPT = (
    "你是文档总结专家。阅读用户提供的文档全文，产出四块结构化结果：\n"
    "- summary：整篇文档的一段式中文摘要；\n"
    "- keyPoints：要点列表（中文）；\n"
    "- persons：涉及人物，每项含 name/identity/role；\n"
    "- riskClues：风险线索，每项含 clue/reason/level。\n"
    "只输出文档中确实出现的内容，不要编造。"
)
_FOCUS_SUFFIX = "本次总结的侧重点：{focus}"
_DOCUMENT_PROMPT = "文档全文：\n{document}\n\n请给出四块结构化总结。"


def _system_prompt(focus: str | None) -> str:
    if focus:
        return f"{_SYSTEM_PROMPT}\n{_FOCUS_SUFFIX.format(focus=focus)}"
    return _SYSTEM_PROMPT


def _build_prompt(text: str) -> str:
    return _DOCUMENT_PROMPT.format(document=text)


def _parse_text(content: bytes, ext: str) -> str:
    """解析文档为纯文本；空结果由调用方判 ValueError。"""
    if ext in _TEXT_EXTENSIONS:
        # 显式 utf-8；解码失败抛 UnicodeDecodeError，由上层转 500
        return content.decode("utf-8")

    # 二进制文档：临时文件带上正确扩展名，markitdown 为主，PDF 以 pymupdf4llm 兜底。
    # 两者都懒加载：离线测试只走 txt/md 路径，不要求 markitdown/pymupdf4llm 在场。
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=ext) as f:
        f.write(content)
        f.flush()
        try:
            from markitdown import MarkItDown

            return MarkItDown().convert(f.name).text_content or ""
        except Exception:
            if ext != ".pdf":
                raise
            import pymupdf4llm

            return pymupdf4llm.to_markdown(f.name) or ""


async def _call_structured(text: str, focus: str | None) -> SummarizeResult:
    """LLM 结构化调用缝：测试 monkeypatch 这里返回假结果。"""
    model = build_chat_model(stream=False)
    agent = Agent(
        name="Summarizer",
        system_prompt=_system_prompt(focus),
        model=model,
    )
    reply = await agent.reply(
        UserMsg(name="user", content=_build_prompt(text)),
        structured_schema=SummarizeResult,
    )
    return SummarizeResult.model_validate(reply.structured_output)


async def summarize_document(
    content: bytes, filename: str, focus: str | None
) -> dict:
    """总结非结构化文档，返回四块结构（camelCase 裸 dict）。"""
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError(f"不支持的文件类型: {ext or '(无扩展名)'}")

    text = _parse_text(content, ext)
    if not text.strip():
        raise ValueError("文档解析结果为空，无法总结")

    result = await _call_structured(text, focus)
    return result.model_dump(by_alias=True)
