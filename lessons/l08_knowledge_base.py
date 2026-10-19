"""
================================================================================
L08 · 知识库存储层：ExtractionResult -> pgvector，结构化+语义联合检索
================================================================================

把 L03~L07 的成果接到一起：
  Extractor 产出 ExtractionOutput
        │  Ingestor.ingest()
        ▼
  PostgreSQL：entities(向量+别名+出处) + relations(+证据)
        │  KnowledgeBase
        ▼
  自然语言定位实体（精确优先/向量兜底）+ 关系图 -> 实体知识卡片

默认 OFFLINE：用内置的构造数据演示，秒跑、不花 token、不依赖模型；
加 --live 会真实抽取 data/samples/news_001.txt 后入库。

运行：
  python lessons/l08_knowledge_base.py            # 离线演示
  python lessons/l08_knowledge_base.py --live     # 真实抽取后入库
  前置：docker start moray-pgvector
================================================================================
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from moray.extraction.document import Document, ExtractionOutput
from moray.kb import EntityStore, Ingestor, KnowledgeBase, close_pool, init_pool
from moray.schema import (
    Entity,
    EntityMention,
    ExtractionResult,
    Relation,
    Span,
)

LIVE = "--live" in sys.argv


def build_demo_output() -> ExtractionOutput:
    """构造一份与真实抽取同构的结果（离线演示用）。"""
    result = ExtractionResult(
        entities=[
            Entity(
                name="华为技术有限公司", type="ORG", confidence=0.98,
                aliases=["华为", "Huawei"],
                mentions=[
                    EntityMention(surface="华为技术有限公司",
                                  span=Span(start=0, end=8, text="华为技术有限公司")),
                    EntityMention(surface="华为", span=Span(start=19, end=21, text="华为")),
                ],
            ),
            Entity(
                name="华为云计算技术有限公司", type="ORG", confidence=0.97,
                aliases=["华为云"],
                mentions=[EntityMention(surface="华为云计算技术有限公司",
                                        span=Span(start=26, end=38, text="华为云计算技术有限公司"))],
            ),
            Entity(name="任正非", type="PER", confidence=0.9,
                   mentions=[EntityMention(surface="任正非", span=Span(start=55, end=58, text="任正非"))]),
            Entity(name="深圳", type="LOC", confidence=0.95,
                   mentions=[EntityMention(surface="深圳", span=Span(start=17, end=19, text="深圳"))]),
            Entity(name="贵阳", type="LOC", confidence=0.92,
                   mentions=[EntityMention(surface="贵阳", span=Span(start=40, end=42, text="贵阳"))]),
        ],
        relations=[
            Relation(head="华为技术有限公司", predicate="子公司",
                     tail="华为云计算技术有限公司", confidence=0.97,
                     evidence_span=Span(start=9, end=38, text="其子公司华为云计算技术有限公司")),
            Relation(head="任正非", predicate="创立", tail="华为技术有限公司",
                     confidence=0.8, evidence_span=Span(start=52, end=66, text="公司创始人任正非")),
            Relation(head="华为云计算技术有限公司", predicate="总部位于", tail="贵阳",
                     confidence=0.85),
        ],
    )
    return ExtractionOutput(
        document_id="demo_doc", domain_code="general", result=result,
        model_name="offline-demo",
    )


async def live_output() -> ExtractionOutput:
    from moray.extraction import Extractor

    text = (ROOT / "data/samples/news_001.txt").read_text(encoding="utf-8").strip()
    doc = Document(id="news_001", text=text, source="news", domain_code="general")
    return await Extractor("general", max_retries=1).extract(doc)


async def main() -> None:
    output = await live_output() if LIVE else build_demo_output()

    pool = await init_pool()
    store = EntityStore(pool)
    await store.init_schema()
    await pool.execute("TRUNCATE relations RESTART IDENTITY CASCADE")  # 教学库重置

    # ---------- 入库 ----------
    ingestor = Ingestor(store)
    report = await ingestor.ingest(output)

    print("=" * 78)
    print(f"入库完成（{'真实抽取' if LIVE else '离线构造'}，文档 {output.document_id}）")
    print("=" * 78)
    print(f"  实体写入 {report.entities_written}，关系写入 {report.relations_written}")
    if report.relations_skipped:
        print("  跳过的关系（端点无法解析）:", report.relations_skipped)
    print(f"  库内实体总数 {await store.count()}，关系总数 {await store.count_relations()}")

    # 幂等性：再 ingest 一次，数量不应增长
    await ingestor.ingest(output)
    print(f"  重复入库后：实体 {await store.count()}，关系 {await store.count_relations()}"
          "（幂等，数量不变）")

    # ---------- 联合检索 ----------
    kb = KnowledgeBase(store)
    print("\n" + "=" * 78)
    print("联合检索：自然语言/简称 -> 实体卡片（含结构化关系）")
    print("=" * 78)

    for q in (["华为", "华为云计算技术有限公司"] if not LIVE else ["华为", "任正非"]):
        candidates = await kb.locate(q)
        print(f"\n查询「{q}」-> 候选 "
              f"{[(c.name, round(c.similarity, 2)) for c in candidates[:3]]}")
        card = await kb.ask(q)
        if card:
            print(card.summary())

    # 纯结构化查询：某实体的出边/入边
    print("\n" + "-" * 78)
    hw_id = await store.get_entity_id("华为技术有限公司")
    rels = await store.relations_of(hw_id, direction="both")
    print(f"结构化查询：华为技术有限公司 的全部关系（出+入，共 {len(rels)} 条）")
    for r in rels:
        print(f"  {r['src_name']} —[{r['predicate']}]→ {r['dst_name']}  conf={r['confidence']:.2f}")

    await close_pool(pool)
    print("\n# 动手任务：")
    print("# 1. 进库查看：docker exec -it moray-pgvector psql -U moray -d moray_kb")
    print("#    SELECT name,type FROM entities;  SELECT * FROM relations;")
    print("# 2. --live 跑真实新闻入库，对比离线构造与真实抽取的差异。")
    print("# 3. 思考：relations 里仍可能把'华为'与'华为技术有限公司'当两个端点，")
    print("#    这正是 L09 入库前实体消歧要解决的问题。")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:  # noqa: BLE001
        print("数据库不可用？请先：docker start moray-pgvector")
        print("错误：", repr(exc)[:200])
