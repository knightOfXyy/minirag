"""敌情线索语义消歧服务的离线测试：不联网、不烧 token。

- embed：monkeypatch Embedder，断言批量、L2 归一化维度、空入参；
- LLM 裁决：monkeypatch `_call_dedup_model` 缝，断言三档映射与 prompt 注入相似度。
"""

import pytest

from moray.summarize import dedup_service as svc
from moray.summarize.dedup_schema import DedupJudgeResult


class FakeEmbedder:
    def __init__(self):
        self.calls = []

    def embed(self, texts):
        import numpy as np

        self.calls.append(list(texts) if not isinstance(texts, str) else [texts])
        n = 1 if isinstance(texts, str) else len(texts)
        vecs = np.ones((n, 4), dtype="float32")
        return vecs[0] if n == 1 else vecs

    def embed_one(self, text):
        return self.embed(text).tolist()


@pytest.fixture()
def fake_embedder(monkeypatch):
    fe = FakeEmbedder()
    monkeypatch.setattr(svc, "get_embedder", lambda *a, **k: fe)
    return fe


def test_embed_texts_batch_returns_normalized_vectors(fake_embedder):
    out = svc.embed_texts(["a", "b", "c"])
    assert len(out) == 3
    assert all(len(v) == 4 for v in out)
    # 单例 embedder 被批量调用一次
    assert len(fake_embedder.calls) == 1 and len(fake_embedder.calls[0]) == 3


def test_embed_texts_empty_returns_empty_list(fake_embedder):
    assert svc.embed_texts([]) == []


def test_cosine_basic_math():
    assert svc.cosine([1, 0], [1, 0]) == pytest.approx(1.0)
    assert svc.cosine([1, 0], [0, 1]) == pytest.approx(0.0)
    assert svc.cosine([0, 0], [1, 1]) == 0.0  # 零向量安全


@pytest.mark.asyncio
async def test_judge_pair_same_confident_maps_duplicate(monkeypatch):
    async def fake_model(pair):
        assert pair.similarity == pytest.approx(0.72)
        return DedupJudgeResult(same=True, confident=True, reason="同一事实")

    monkeypatch.setattr(svc, "_call_dedup_model", fake_model)
    out = await svc.judge_pair(
        new_title="与境外人员秘密会面", new_reason="r1",
        existing_title="私下接触境外人员", existing_reason="r2", similarity=0.72,
    )
    assert out["verdict"] == "duplicate"
    assert out["reason"] == "同一事实"


@pytest.mark.asyncio
async def test_judge_pair_not_confident_maps_suspect(monkeypatch):
    async def fake_model(pair):
        return DedupJudgeResult(same=True, confident=False, reason="信息不足")

    monkeypatch.setattr(svc, "_call_dedup_model", fake_model)
    out = await svc.judge_pair(
        new_title="a", new_reason="", existing_title="b", existing_reason="", similarity=0.7,
    )
    # LLM 不自信 → 不自动杀，落 suspect 交人工
    assert out["verdict"] == "suspect"


@pytest.mark.asyncio
async def test_judge_pair_different_maps_fresh(monkeypatch):
    async def fake_model(pair):
        return DedupJudgeResult(same=False, confident=True, reason="不同事实")

    monkeypatch.setattr(svc, "_call_dedup_model", fake_model)
    out = await svc.judge_pair(
        new_title="a", new_reason="", existing_title="b", existing_reason="", similarity=0.65,
    )
    assert out["verdict"] == "fresh"
    assert out["reason"] == "不同事实"
