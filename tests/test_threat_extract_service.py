"""敌情线索抽取 service 的离线测试：不联网、不烧 token。

LLM 调用缝是 `moray.summarize.threat_service._call_threat_model`，
测试 monkeypatch 这个缝返回假 ThreatClueList，断言：
- 非法扩展名/无扩展名/空文本显式 ValueError；
- focus 注入 system prompt；
- 结构化结果原样透传为 camelCase dict 列表。
"""

import pytest

from moray.summarize.threat_schema import ThreatClue, ThreatClueList
from moray.summarize.threat_service import (
    _threat_system_prompt,
    extract_threat_clues,
)


@pytest.mark.asyncio
async def test_empty_text_raises_value_error():
    with pytest.raises(ValueError):
        await extract_threat_clues(b"  \n\n ", "doc.txt", None)


@pytest.mark.asyncio
async def test_unsupported_extension_raises_value_error():
    with pytest.raises(ValueError):
        await extract_threat_clues(b"hello", "doc.exe", None)


@pytest.mark.asyncio
async def test_no_extension_raises_value_error():
    with pytest.raises(ValueError):
        await extract_threat_clues(b"hello", "doc", None)


def test_system_prompt_injects_focus_only_when_present():
    p = _threat_system_prompt("重点关注对外披露")
    assert "重点关注对外披露" in p
    assert "敌情线索" in _threat_system_prompt(None)
    assert "本次侧重点" not in _threat_system_prompt(None)


@pytest.mark.asyncio
async def test_clues_passthrough_camelcase(monkeypatch):
    captured = {}

    async def fake_call(text, focus):
        captured["text"] = text
        captured["focus"] = focus
        return ThreatClueList(
            clues=[
                ThreatClue(title="私下接触境外人员", reason="未走审批", level="高"),
                ThreatClue(title="违规拷贝数据", reason="违反保密规定", level="中"),
            ]
        )

    monkeypatch.setattr("moray.summarize.threat_service._call_threat_model", fake_call)

    out = await extract_threat_clues("文档正文".encode("utf-8"), "brief.txt", "泄密")

    assert captured["text"] == "文档正文"
    assert captured["focus"] == "泄密"
    assert out == {
        "clues": [
            {"title": "私下接触境外人员", "reason": "未走审批", "level": "高"},
            {"title": "违规拷贝数据", "reason": "违反保密规定", "level": "中"},
        ]
    }


@pytest.mark.asyncio
async def test_empty_clues_returns_empty_list(monkeypatch):
    async def fake_call(text, focus):
        return ThreatClueList(clues=[])

    monkeypatch.setattr("moray.summarize.threat_service._call_threat_model", fake_call)
    out = await extract_threat_clues(b"some text", "a.txt", None)
    assert out == {"clues": []}
