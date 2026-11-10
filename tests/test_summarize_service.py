"""文档总结 service 的离线测试：不联网、不烧 token。

LLM 调用缝是 `moray.summarize.service._call_structured`（解析 → prompt →
结构化输出收敛在 service 内部，未来接 job 时复用它，见 ADR 0004 切换缝）。
测试 monkeypatch 这个缝返回假结构化结果，断言 prompt 注入 focus、结果原样透传、
空文本/非法扩展名显式报错。
"""

import pytest

from moray.summarize.schemas import SummarizeResult
from moray.summarize.service import _system_prompt, summarize_document


@pytest.mark.asyncio
async def test_empty_text_raises_value_error():
    with pytest.raises(ValueError):
        await summarize_document(b"   \n\n  ", "doc.txt", None)


@pytest.mark.asyncio
async def test_unsupported_extension_raises_value_error():
    with pytest.raises(ValueError):
        await summarize_document(b"hello", "doc.exe", None)


@pytest.mark.asyncio
async def test_no_extension_raises_value_error():
    with pytest.raises(ValueError):
        await summarize_document(b"hello", "doc", None)


def test_system_prompt_injects_focus_only_when_present():
    assert "重点关注涉密合作" in _system_prompt("重点关注涉密合作")
    assert "本次总结的侧重点" not in _system_prompt(None)


@pytest.mark.asyncio
async def test_structured_result_passthrough(monkeypatch):
    captured = {}

    async def fake_call(text, focus):
        captured["text"] = text
        captured["focus"] = focus
        return SummarizeResult(
            summary="一段摘要",
            key_points=["要点1", "要点2"],
            persons=[{"name": "张三", "identity": "研究员", "role": "负责人"}],
            risk_clues=[{"clue": "线索", "reason": "依据", "level": "高"}],
        )

    monkeypatch.setattr("moray.summarize.service._call_structured", fake_call)

    out = await summarize_document("文档正文".encode("utf-8"), "doc.md", "风险")

    assert captured["text"] == "文档正文"
    assert captured["focus"] == "风险"
    assert out == {
        "summary": "一段摘要",
        "keyPoints": ["要点1", "要点2"],
        "persons": [{"name": "张三", "identity": "研究员", "role": "负责人"}],
        "riskClues": [{"clue": "线索", "reason": "依据", "level": "高"}],
    }
