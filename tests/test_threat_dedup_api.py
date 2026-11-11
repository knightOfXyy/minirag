"""POST /threat-embed 与 /threat-dedup-judge 的进程内集成测试（不起端口、不触网）。

embed monkeypatch get_embedder；judge monkeypatch _call_dedup_model 缝。
"""

import numpy as np
import pytest
from httpx import ASGITransport, AsyncClient

from moray.api.app import create_app
from moray.summarize.dedup_schema import DedupJudgeResult


class FakeEmbedder:
    def embed(self, texts):
        items = [texts] if isinstance(texts, str) else list(texts)
        return np.ones((len(items), 3), dtype="float32")

    def embed_one(self, text):
        return self.embed(text)[0].tolist()


@pytest.fixture()
async def client(monkeypatch):
    monkeypatch.setattr(
        "moray.summarize.dedup_service.get_embedder", lambda *a, **k: FakeEmbedder()
    )

    async def fake_judge(pair):
        return DedupJudgeResult(same=True, confident=True, reason="同一事实")

    monkeypatch.setattr("moray.summarize.dedup_service._call_dedup_model", fake_judge)
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        yield ac


@pytest.mark.asyncio
async def test_embed_returns_vectors(client):
    r = await client.post("/threat-embed", json={"texts": ["线索甲", "线索乙"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"embeddings"}
    assert len(body["embeddings"]) == 2
    assert all(len(v) == 3 for v in body["embeddings"])


@pytest.mark.asyncio
async def test_embed_empty_texts(client):
    r = await client.post("/threat-embed", json={"texts": []})
    assert r.status_code == 200
    assert r.json()["embeddings"] == []


@pytest.mark.asyncio
async def test_dedup_judge_returns_verdict(client):
    r = await client.post(
        "/threat-dedup-judge",
        json={
            "newTitle": "与境外人员秘密会面",
            "newReason": "r1",
            "existingTitle": "私下接触境外人员",
            "existingReason": "r2",
            "similarity": 0.72,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verdict"] == "duplicate"
    assert body["reason"] == "同一事实"


@pytest.mark.asyncio
async def test_dedup_judge_rejects_missing_field(client):
    r = await client.post("/threat-dedup-judge", json={"newTitle": "x"})
    assert r.status_code == 422
