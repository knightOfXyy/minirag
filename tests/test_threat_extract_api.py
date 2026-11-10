"""POST /threat-extract 的进程内集成测试（httpx ASGITransport，不起端口、不触网）。

假 LLM 通过 monkeypatch `moray.summarize.threat_service._call_threat_model` 注入，
样例文件走 txt/md 显式 utf-8 解码，不依赖 markitdown/pymupdf4llm。
"""

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from moray.api.app import create_app
from moray.summarize.threat_schema import ThreatClue, ThreatClueList

FIXTURES = Path(__file__).parent / "fixtures" / "docs"


def _fake_clues():
    return ThreatClueList(
        clues=[
            ThreatClue(title="私下接触境外人员", reason="未走审批", level="高"),
            ThreatClue(title="违规拷贝实验数据", reason="违反保密规定", level="中"),
        ]
    )


@pytest.fixture()
async def client(monkeypatch):
    async def fake_call(text, focus):
        return _fake_clues()

    monkeypatch.setattr("moray.summarize.threat_service._call_threat_model", fake_call)
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        yield ac


@pytest.mark.asyncio
async def test_threat_extract_txt_returns_clues(client):
    content = (FIXTURES / "sample.txt").read_bytes()
    r = await client.post(
        "/threat-extract", files={"file": ("sample.txt", content, "text/plain")}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"clues"}
    assert len(body["clues"]) == 2
    first = body["clues"][0]
    assert set(first) == {"title", "reason", "level"}
    assert first["title"] == "私下接触境外人员"
    assert first["level"] == "高"


@pytest.mark.asyncio
async def test_threat_extract_md_returns_clues(client):
    content = (FIXTURES / "sample.md").read_bytes()
    r = await client.post(
        "/threat-extract", files={"file": ("sample.md", content, "text/markdown")}
    )
    assert r.status_code == 200
    assert len(r.json()["clues"]) == 2


@pytest.mark.asyncio
async def test_unsupported_extension_returns_400(client):
    r = await client.post(
        "/threat-extract",
        files={"file": ("evil.exe", b"MZ", "application/octet-stream")},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_empty_text_returns_400(client):
    r = await client.post(
        "/threat-extract", files={"file": ("empty.txt", b"  \n", "text/plain")}
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_oversize_returns_400(client, monkeypatch):
    monkeypatch.setattr("moray.api.app.MAX_FILE_BYTES", 10)
    r = await client.post(
        "/threat-extract", files={"file": ("big.txt", b"x" * 100, "text/plain")}
    )
    assert r.status_code == 400
