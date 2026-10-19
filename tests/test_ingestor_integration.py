"""Ingestor / KnowledgeBase 集成测试（需要 moray-pgvector）。

用 FakeEmbedder 给出确定性向量（按名字哈希），避免加载 ONNX 模型；
无数据库时自动 skip。
    RUN_DB_TESTS=1 .venv/bin/python -m pytest tests/test_ingestor_integration.py -q
"""

import os

import numpy as np
import pytest

from moray.extraction.document import ExtractionOutput
from moray.kb import EntityStore, Ingestor, KnowledgeBase, close_pool, init_pool
from moray.schema import (
    Entity,
    EntityMention,
    ExtractionResult,
    Relation,
    Span,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DB_TESTS") != "1",
    reason="需要 PostgreSQL；设置 RUN_DB_TESTS=1 且启动 moray-pgvector 后运行",
)


class FakeEmbedder:
    """按字符串生成确定性、近似可分的归一化向量：

    同首词的名字共享一个大方向（模拟"简称↔全称"近义），其余用哈希扰动。
    """

    dim = 512

    def __init__(self) -> None:
        self._cache: dict[str, list[float]] = {}

    def embed(self, texts):
        single = isinstance(texts, str)
        items = [texts] if single else texts
        out = np.array([self._one(t) for t in items], dtype=np.float32)
        return out[0] if single else out

    def embed_one(self, text: str) -> list[float]:
        return self._one(text).tolist()

    def _one(self, text: str) -> np.ndarray:
        if text in self._cache:
            return np.array(self._cache[text])
        rng = np.random.default_rng(abs(hash(text)) % (2**32))
        v = rng.normal(size=self.dim).astype(np.float32)
        v /= np.linalg.norm(v)
        self._cache[text] = v.tolist()
        return v


def _output() -> ExtractionOutput:
    result = ExtractionResult(
        entities=[
            Entity(
                name="华为技术有限公司", type="ORG", confidence=0.98,
                aliases=["华为"],
                mentions=[EntityMention(
                    surface="华为", span=Span(start=0, end=2, text="华为"))],
            ),
            Entity(name="任正非", type="PER", confidence=0.9,
                   mentions=[EntityMention(surface="任正非",
                                           span=Span(start=3, end=6, text="任正非"))]),
        ],
        relations=[
            Relation(head="任正非", predicate="创立", tail="华为技术有限公司",
                     confidence=0.9, evidence_span=Span(start=0, end=6, text="任正非创立华为")),
        ],
    )
    return ExtractionOutput(document_id="t1", domain_code="general", result=result)


@pytest.fixture()
async def store():
    pool = await init_pool()
    s = EntityStore(pool)
    await s.init_schema()
    # 实体是父表，级联清空别名/出处/关系/证据
    await pool.execute("TRUNCATE entities RESTART IDENTITY CASCADE")
    yield s
    await close_pool(pool)


@pytest.mark.asyncio
async def test_ingest_entities_relations_and_mentions(store):
    ing = Ingestor(store, FakeEmbedder())
    report = await ing.ingest(_output())

    assert report.entities_written == 2
    assert report.relations_written == 1
    assert report.relations_skipped == []

    # 别名 + mention 落库
    hit = await store.find_by_name_or_alias("华为")
    assert hit["name"] == "华为技术有限公司"

    async with store.pool.acquire() as conn:
        n_mentions = await conn.fetchval(
            "SELECT count(*) FROM entity_mentions WHERE document_id=$1", "t1")
        n_evidence = await conn.fetchval(
            "SELECT count(*) FROM relation_evidence WHERE document_id=$1", "t1")
    assert n_mentions == 2
    assert n_evidence == 1


@pytest.mark.asyncio
async def test_ingest_is_idempotent(store):
    ing = Ingestor(store, FakeEmbedder())
    await ing.ingest(_output())
    await ing.ingest(_output())  # 重复入库
    assert await store.count() == 2
    assert await store.count_relations() == 1
    async with store.pool.acquire() as conn:
        mentions = await conn.fetchval(
            "SELECT count(*) FROM entity_mentions WHERE document_id=$1", "t1")
    assert mentions == 2  # 不应翻倍


@pytest.mark.asyncio
async def test_missing_endpoint_creates_misc(store):
    out = ExtractionOutput(
        document_id="t2", domain_code="general",
        result=ExtractionResult(
            entities=[Entity(name="甲公司", type="ORG")],
            relations=[Relation(head="甲公司", predicate="投资", tail="乙公司")],
        ),
    )
    # 乙公司不在实体列表 -> 自动建 MISC 占位，关系仍写入
    ing = Ingestor(store, FakeEmbedder(), create_missing_endpoints=True)
    report = await ing.ingest(out)
    assert report.relations_written == 1
    misc = await store.find_by_name_or_alias("乙公司")
    assert misc and misc["type"] == "MISC"

    # 关闭兜底 -> 跳过关系
    out2 = ExtractionOutput(
        document_id="t3", domain_code="general",
        result=ExtractionResult(
            entities=[Entity(name="丙公司", type="ORG")],
            relations=[Relation(head="丙公司", predicate="投资", tail="丁公司")],
        ),
    )
    ing2 = Ingestor(store, FakeEmbedder(), create_missing_endpoints=False)
    report2 = await ing2.ingest(out2)
    assert report2.relations_written == 0
    assert report2.relations_skipped == ["丙公司 -[投资]-> 丁公司"]


@pytest.mark.asyncio
async def test_knowledge_base_card_and_locate(store):
    await Ingestor(store, FakeEmbedder()).ingest(_output())
    kb = KnowledgeBase(store, FakeEmbedder())

    # 精确别名定位
    candidates = await kb.locate("华为")
    assert candidates and candidates[0].name == "华为技术有限公司"
    assert candidates[0].similarity == 1.0  # 精确命中距离 0

    # 知识卡片含出入边
    eid = await store.get_entity_id("任正非")
    card = await kb.card(eid)
    assert card is not None
    assert any(r["predicate"] == "创立" for r in card.outgoing)
