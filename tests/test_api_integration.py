"""FastAPI 接口的进程内集成测试（httpx ASGITransport，不起端口）。

需要 moray-pgvector；无 DB 自动 skip。
    RUN_DB_TESTS=1 .venv/bin/python -m pytest tests/test_api_integration.py -q
"""

import os

import numpy as np
import pytest

from moray.api.app import AppDeps, create_app
from moray.jobs import JobStore
from moray.kb import EntityStore, Ingestor, KnowledgeBase, close_pool, init_pool
from moray.observability import Dashboard, RunRecorder
from moray.schema import Entity, EntityMention, ExtractionResult, Relation, Span

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DB_TESTS") != "1",
    reason="需要 PostgreSQL；设置 RUN_DB_TESTS=1 且启动 moray-pgvector 后运行",
)

TEXT = "腾讯控股有限公司总部位于深圳，马化腾创立了腾讯。"


class FakeEmbedder:
    dim = 512

    def __init__(self):
        self._cache = {}

    def embed(self, texts):
        single = isinstance(texts, str)
        items = [texts] if single else texts
        out = np.array([self._one(t) for t in items], dtype=np.float32)
        return out[0] if single else out

    def embed_one(self, text):
        return self._one(text).tolist()

    def _one(self, text):
        if text not in self._cache:
            v = np.random.default_rng(abs(hash(text)) % (2 ** 32)).normal(
                size=self.dim).astype(np.float32)
            v /= np.linalg.norm(v)
            self._cache[text] = v
        return np.array(self._cache[text])


async def _extractor(text, feedback):
    def m(name):
        i = text.find(name)
        return EntityMention(surface=name,
                             span=Span(start=i, end=i + len(name), text=name))

    def ev(sub):
        i = text.find(sub)
        return Span(start=i, end=i + len(sub), text=sub) if i >= 0 else None

    return ExtractionResult(
        entities=[
            Entity(name="腾讯控股有限公司", type="ORG", confidence=0.9,
                   aliases=["腾讯"], mentions=[m("腾讯控股有限公司")]),
            Entity(name="深圳", type="LOC", confidence=0.9, mentions=[m("深圳")]),
            Entity(name="马化腾", type="PER", confidence=0.9, mentions=[m("马化腾")]),
        ],
        relations=[
            Relation(head="腾讯控股有限公司", predicate="总部位于", tail="深圳",
                     confidence=0.8, evidence_span=ev("腾讯控股有限公司总部位于深圳")),
            Relation(head="马化腾", predicate="创立", tail="腾讯控股有限公司",
                     confidence=0.8, evidence_span=ev("马化腾创立了腾讯")),
        ],
    )


@pytest.fixture()
async def client():
    from httpx import ASGITransport, AsyncClient

    pool = await init_pool()
    store = EntityStore(pool)
    await store.init_schema()
    await pool.execute("TRUNCATE entities RESTART IDENTITY CASCADE")
    await pool.execute("TRUNCATE extraction_jobs RESTART IDENTITY")
    emb = FakeEmbedder()
    kb = KnowledgeBase(store, emb)
    ingestor = Ingestor(store, emb)

    jobs = JobStore(pool)
    await jobs.init_schema()
    dash = Dashboard(pool)
    await dash.init_schema()
    await pool.execute("TRUNCATE pipeline_runs RESTART IDENTITY")

    deps = AppDeps(pool=pool, store=store, kb=kb, ingestor=ingestor,
                   job_store=jobs, dashboard=dash, extractor_fn=_extractor)
    app = create_app(deps)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        yield ac, deps
    await close_pool(pool)


@pytest.mark.asyncio
async def test_healthz(client):
    ac, _ = client
    r = await ac.get("/healthz")
    assert r.status_code == 200 and r.json()["ok"] is True


@pytest.mark.asyncio
async def test_extract_and_query(client):
    ac, _ = client
    r = await ac.post("/extract", json={"text": TEXT, "document_id": "api1"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["accepted_by_review"] is True
    assert len(body["entities"]) == 3
    assert body["ingest"]["entities_written"] == 3

    # 别名定位
    r = await ac.get("/entities/locate", params={"query": "腾讯", "k": 1})
    cand = r.json()["candidates"][0]
    assert cand["name"] == "腾讯控股有限公司" and cand["similarity"] == 1.0

    # 关系查询（入边）
    r = await ac.get("/relations",
                     params={"name": "腾讯", "direction": "in"})
    preds = {(x["source"], x["predicate"]) for x in r.json()["relations"]}
    assert ("马化腾", "创立") in preds


@pytest.mark.asyncio
async def test_jobs_flow(client, tmp_path):
    ac, _ = client
    f = tmp_path / "d.txt"
    f.write_text(TEXT, encoding="utf-8")
    r = await ac.post("/jobs", json={"document_ref": str(f)})
    assert r.status_code == 200 and r.json()["created"] is True
    job_id = r.json()["job_id"]
    # 幂等
    r2 = await ac.post("/jobs", json={"document_ref": str(f)})
    assert r2.json()["created"] is False

    r = await ac.get(f"/jobs/{job_id}")
    assert r.status_code == 200 and r.json()["status"] == "pending"
    r = await ac.get("/jobs", params={"status": "pending"})
    assert len(r.json()["jobs"]) == 1


@pytest.mark.asyncio
async def test_review_decision_merges(client):
    ac, deps = client
    # 先入库两个实体，再登记一条待审合并
    a = await deps.store.upsert_entity(
        name="腾讯控股有限公司", type="ORG", embedding=deps.kb.embedder.embed_one("A"))
    b = await deps.store.upsert_entity(
        name="腾讯公司", type="ORG", embedding=deps.kb.embedder.embed_one("B"))
    rid = await deps.store.enqueue_review(
        survivor_id=a, duplicate_id=b, survivor_name="腾讯控股有限公司",
        duplicate_name="腾讯公司", similarity=0.9, reason="test")

    r = await ac.get("/reviews")
    assert any(x["id"] == rid for x in r.json()["reviews"])

    r = await ac.post(f"/reviews/{rid}/decision", json={"approved": True})
    assert r.status_code == 200 and r.json()["status"] == "approved"
    # 合并后 b 已不存在
    assert await deps.store.get(b) is None
    # 重复裁决 -> 404
    r = await ac.post(f"/reviews/{rid}/decision", json={"approved": True})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_dashboard_endpoint(client):
    ac, deps = client
    await deps.dashboard.record(RunRecorder(
        document_id="x", n_entities=3, n_relations=1, span_ratio=1.0,
        review_passed=True, n_auto_merged=1, n_queued=0, n_created=2))
    r = await ac.get("/metrics/dashboard")
    assert r.status_code == 200
    assert r.json()["runs"] == 1 and r.json()["pass_rate"] == 1.0


@pytest.mark.asyncio
async def test_extract_501_without_extractor(client):
    from httpx import ASGITransport, AsyncClient
    from moray.api.app import create_app as _create

    ac, deps = client
    deps.extractor_fn = None
    # 直接用同一 deps 新建 app 调用，验证未配置抽取器时 501
    app = _create(deps)
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://t") as c2:
        r = await c2.post("/extract", json={"text": "x"})
        assert r.status_code == 501
