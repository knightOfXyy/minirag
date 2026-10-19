"""实体合并 + 审核队列的 DB 集成测试（需要 moray-pgvector）。

无 DB 自动 skip；RUN_DB_TESTS=1 且启动容器后运行。
"""

import os

import pytest

from moray.kb import EntityStore, close_pool, init_pool

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DB_TESTS") != "1",
    reason="需要 PostgreSQL；设置 RUN_DB_TESTS=1 且启动 moray-pgvector 后运行",
)


@pytest.fixture()
async def store():
    pool = await init_pool()
    s = EntityStore(pool)
    await s.init_schema()
    await pool.execute("TRUNCATE entities RESTART IDENTITY CASCADE")
    yield s
    await close_pool(pool)


async def _mk(store, name, type="ORG", vec=None, aliases=None):
    return await store.upsert_entity(
        name=name, type=type, embedding=vec or [0.0] * 512,
        aliases=aliases or [], confidence=0.9)


@pytest.mark.asyncio
async def test_merge_moves_aliases_mentions_and_relations(store):
    survivor = await _mk(store, "华为技术有限公司", aliases=["Huawei"])
    dup = await _mk(store, "华为", aliases=["HW"])

    # dup 有一条出边关系 + 一条入边关系
    other = await _mk(store, "华为云计算技术有限公司")
    third = await _mk(store, "任正非", type="PER")
    r1 = await store.upsert_relation(source_id=dup, predicate="子公司", target_id=other)
    r2 = await store.upsert_relation(source_id=third, predicate="创立", target_id=dup)
    await store.add_relation_evidence(
        r1, document_id="d", ev={"surface": "华为的子公司华为云"})

    await store.merge_entities(survivor, dup)

    # duplicate 消失
    assert await store.get(dup) is None
    # 旧名/旧别名都能解析到 survivor
    assert (await store.find_by_name_or_alias("华为"))["id"] == survivor
    assert (await store.find_by_name_or_alias("HW"))["id"] == survivor
    assert (await store.find_by_name_or_alias("Huawei"))["id"] == survivor

    # 关系外键迁移：两条关系现在都连在 survivor 上
    rels = await store.relations_of(survivor, direction="both")
    names = {(r["src_name"], r["predicate"], r["dst_name"]) for r in rels}
    assert ("华为技术有限公司", "子公司", "华为云计算技术有限公司") in names
    assert ("任正非", "创立", "华为技术有限公司") in names
    # 证据仍在
    async with store.pool.acquire() as conn:
        ev = await conn.fetchval(
            "SELECT count(*) FROM relation_evidence WHERE relation_id=$1", r1)
    assert ev == 1


@pytest.mark.asyncio
async def test_merge_is_idempotent(store):
    a = await _mk(store, "甲公司")
    b = await _mk(store, "乙公司")
    await store.merge_entities(a, b)
    await store.merge_entities(a, b)  # b 已不存在，不应报错
    assert await store.count() == 1


@pytest.mark.asyncio
async def test_merge_drops_self_and_duplicate_relations(store):
    a = await _mk(store, "甲公司")
    b = await _mk(store, "甲公司简称")
    # a 已有的关系，b 上也有一条等价关系；合并后不应产生重复/自指
    c = await _mk(store, "丙公司")
    await store.upsert_relation(source_id=a, predicate="投资", target_id=c)
    await store.upsert_relation(source_id=b, predicate="投资", target_id=c)
    await store.merge_entities(a, b)
    rels = await store.relations_of(a, direction="out")
    assert len(rels) == 1
    assert rels[0]["dst_name"] == "丙公司"


@pytest.mark.asyncio
async def test_review_queue_lifecycle(store):
    survivor = await _mk(store, "华为技术有限公司")
    dup = await _mk(store, "华为云公司")

    rid = await store.enqueue_review(
        survivor_id=survivor, duplicate_id=dup,
        survivor_name="华为技术有限公司", duplicate_name="华为云公司",
        similarity=0.82, reason="暧昧")
    assert len(await store.pending_reviews()) == 1

    # 重复入队同一对待审不产生新行
    rid2 = await store.enqueue_review(
        survivor_id=survivor, duplicate_id=dup,
        survivor_name="华为技术有限公司", duplicate_name="华为云公司",
        similarity=0.82, reason="暧昧")
    assert rid2 == rid

    # 拒绝：实体都保留，队列项终态
    assert await store.decide_review(rid, approved=False) is True
    assert len(await store.pending_reviews()) == 0
    assert await store.get(dup) is not None
    # 终态后不能再裁决
    assert await store.decide_review(rid, approved=True) is False


@pytest.mark.asyncio
async def test_review_approve_merges(store):
    survivor = await _mk(store, "A公司")
    dup = await _mk(store, "A司")
    rid = await store.enqueue_review(
        survivor_id=survivor, duplicate_id=dup,
        survivor_name="A公司", duplicate_name="A司",
        similarity=0.9, reason="简称")
    await store.decide_review(rid, approved=True)
    assert await store.get(dup) is None
    assert await store.count() == 1
