"""pgvector 集成测试（需要本地 Docker 里的 moray-pgvector）。

无数据库时自动 skip，不阻断普通单元测试：
    .venv/bin/python -m pytest tests/ -q
显式跑集成测试（先启动容器）：
    docker start moray-pgvector && .venv/bin/python -m pytest tests/ -q
"""

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DB_TESTS") != "1",
    reason="需要 PostgreSQL；设置 RUN_DB_TESTS=1 且启动 moray-pgvector 后运行",
)


@pytest.fixture()
async def store():
    from moray.kb import EntityStore, close_pool, init_pool

    pool = await init_pool()
    s = EntityStore(pool)
    await s.init_schema()
    await pool.execute("TRUNCATE entities RESTART IDENTITY CASCADE")
    yield s
    await close_pool(pool)


@pytest.mark.asyncio
async def test_upsert_and_count(store):
    await store.upsert_entity(
        name="测试公司", type="ORG", embedding=[0.1] * 512,
        aliases=["测试"], confidence=0.9,
    )
    # 幂等：再写一次不应产生重复行
    await store.upsert_entity(
        name="测试公司", type="ORG", embedding=[0.2] * 512,
        aliases=["测试", "测司"], confidence=0.95,
    )
    assert await store.count() == 1
    hit = await store.find_by_name_or_alias("测试")
    assert hit and hit["name"] == "测试公司"


@pytest.mark.asyncio
async def test_similarity_ranking(store):
    import numpy as np

    rng = np.random.default_rng(0)
    base = rng.normal(size=512).astype(np.float32)
    base /= np.linalg.norm(base)
    near = base + 0.01 * rng.normal(size=512).astype(np.float32)
    far = rng.normal(size=512).astype(np.float32)

    await store.upsert_entity(name="近邻公司", type="ORG", embedding=near.tolist())
    await store.upsert_entity(name="遥远公司", type="ORG", embedding=far.tolist())

    hits = await store.similar_entities(base.tolist(), type="ORG", k=2)
    assert hits[0].name == "近邻公司"
    assert hits[0].similarity > hits[1].similarity


@pytest.mark.asyncio
async def test_alias_exact_match(store):
    await store.upsert_entity(
        name="华为技术有限公司", type="ORG", embedding=[0.0] * 512,
        aliases=["华为", "Huawei"],
    )
    assert (await store.find_by_name_or_alias("Huawei"))["name"] == "华为技术有限公司"
    assert await store.find_by_name_or_alias("不存在") is None


@pytest.mark.asyncio
async def test_unknown_confidence_is_null_not_zero(store):
    """slim 的未知置信度必须落 NULL（可区分未知/最不可信），不能写 0。"""
    eid = await store.upsert_entity(
        name="未知置信公司", type="ORG", embedding=[0.1] * 512)
    row = await store.get(eid)
    assert row["confidence"] is None

    # 已有已知值时，再写「未知」不能把已知值冲掉
    await store.upsert_entity(
        name="未知置信公司", type="ORG", embedding=[0.1] * 512, confidence=0.7)
    await store.upsert_entity(
        name="未知置信公司", type="ORG", embedding=[0.1] * 512)
    assert (await store.get(eid))["confidence"] == pytest.approx(0.7, abs=1e-6)

    from moray.kb.query import KnowledgeBase

    class _Emb:
        def embed_one(self, text):
            return [0.1] * 512

    card = await KnowledgeBase(store, embedder=_Emb()).card(eid)
    assert card.confidence == pytest.approx(0.7, abs=1e-6)


@pytest.mark.asyncio
async def test_relation_unknown_confidence_sorts_last(store):
    """/relations 排序：未知(NULL) 不能排在有值前面，也不能被当成 0 参与比较。"""
    a = await store.upsert_entity(
        name="甲", type="ORG", embedding=[0.1] * 512)
    b = await store.upsert_entity(
        name="乙", type="ORG", embedding=[0.1] * 512)
    c = await store.upsert_entity(
        name="丙", type="ORG", embedding=[0.1] * 512)
    await store.upsert_relation(
        source_id=a, predicate="未知证据", target_id=b, confidence=None)
    await store.upsert_relation(
        source_id=a, predicate="已知证据", target_id=c, confidence=0.9)

    rels = await store.relations_of(a, direction="out")
    assert [r["predicate"] for r in rels] == ["已知证据", "未知证据"]
    assert rels[1]["confidence"] is None
