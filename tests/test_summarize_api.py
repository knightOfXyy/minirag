"""POST /summarize 的进程内集成测试（httpx ASGITransport，不起端口、不触网）。

假 LLM 通过 monkeypatch `moray.summarize.service._call_structured` 注入，
小样例文件放 tests/fixtures/docs/（txt/md 真实内容，走显式 utf-8 解码，
不依赖 markitdown/pymupdf4llm）。
"""

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from moray.api.app import create_app
from moray.summarize.schemas import SummarizeResult

FIXTURES = Path(__file__).parent / "fixtures" / "docs"


def _fake_result():
    return SummarizeResult(
        summary="一段摘要",
        key_points=["要点1"],
        persons=[{"name": "张三", "identity": "研究员", "role": "负责人"}],
        risk_clues=[{"clue": "线索", "reason": "依据", "level": "高"}],
    )


@pytest.fixture()
async def client(monkeypatch):
    async def fake_call(text, focus):
        return _fake_result()

    monkeypatch.setattr("moray.summarize.service._call_structured", fake_call)
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        yield ac


@pytest.mark.asyncio
async def test_summarize_txt_returns_four_blocks(client):
    content = (FIXTURES / "sample.txt").read_bytes()
    r = await client.post(
        "/summarize", files={"file": ("sample.txt", content, "text/plain")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"summary", "keyPoints", "persons", "riskClues"}
    assert body["summary"] == "一段摘要"
    assert body["keyPoints"] == ["要点1"]
    assert body["persons"][0]["name"] == "张三"
    assert body["riskClues"][0]["level"] == "高"


@pytest.mark.asyncio
async def test_summarize_md_returns_four_blocks(client):
    content = (FIXTURES / "sample.md").read_bytes()
    r = await client.post(
        "/summarize", files={"file": ("sample.md", content, "text/markdown")})
    assert r.status_code == 200, r.text
    assert set(r.json()) == {"summary", "keyPoints", "persons", "riskClues"}


@pytest.mark.asyncio
async def test_unsupported_extension_returns_400(client):
    r = await client.post(
        "/summarize",
        files={"file": ("evil.exe", b"MZ", "application/octet-stream")})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_empty_text_returns_400(client):
    r = await client.post(
        "/summarize", files={"file": ("empty.txt", b"   \n", "text/plain")})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_oversize_returns_400(client, monkeypatch):
    monkeypatch.setattr("moray.api.app.MAX_FILE_BYTES", 10)
    r = await client.post(
        "/summarize", files={"file": ("big.txt", b"x" * 100, "text/plain")})
    assert r.status_code == 400
