"""
================================================================================
L09 · 实体消歧 + HITL：让「华为 / 华为云 / 华为技术有限公司」不再各自为政
================================================================================

上接 L05 的痛点：同一现实公司被抽成多个实体，导致知识图谱里"一个华为"碎成
几片。L07 给了向量相似度，L08 给了入库与合并原语，本课把它们串成消歧闭环：

  新实体 ──向量召回──> 候选已有实体
            │
            ├─ 判定 same(高可信)      -> 自动合并：别名/出处/关系外键迁移
            ├─ 判定 different(低相似) -> 作为新实体保留
            └─ uncertain(暧昧地带)    -> 进【人工审核队列】(HITL)，人裁决后合并

关键设计（企业级）：
  · 判定器 Judge 是可替换策略。默认 RuleBasedJudge（离线/确定/零花费）；
    暧昧地带可升级到 LLMJudge（消歧 Agent，--llm，联网花 token）。
  · 自动合并只敢处理"特别确定"的，拿不准一律不猜、交给人——宁可多审，不可错并。
  · 合并是事务：迁移别名/mentions/关系外键、删除重复实体，全成功或全回滚。
  · HITL 的交互式 Agent 确认在 L02 已学；这里用【持久化审核队列】实现异步
    HITL：审核员不必在线等待，事后逐条 approved/rejected。

默认 OFFLINE（规则判定，秒跑，不花 token）。
运行：
  python lessons/l09_disambiguation_hitl.py
  python lessons/l09_disambiguation_hitl.py --llm     # 暧昧案例交消歧 Agent
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

from moray.extraction.document import ExtractionOutput
from moray.kb import (
    Disambiguator,
    EntityStore,
    Ingestor,
    LLMJudge,
    RuleBasedJudge,
    close_pool,
    init_pool,
)
from moray.schema import Entity, ExtractionResult, Relation, Span, EntityMention

USE_LLM = "--llm" in sys.argv


def output_with_fragmented_huawei() -> ExtractionOutput:
    """构造一篇把"华为"抽碎了的文档结果（复现 L05 现象）。"""
    result = ExtractionResult(
        entities=[
            # 规范实体先入库（第一批）
            Entity(name="华为技术有限公司", type="ORG", confidence=0.98,
                   aliases=["Huawei"],
                   mentions=[EntityMention(surface="华为技术有限公司",
                                            span=Span(start=0, end=8, text="华为技术有限公司"))]),
        ],
        relations=[],
    )
    return ExtractionOutput(document_id="doc_base", domain_code="general",
                            result=result)


def output_second_batch() -> ExtractionOutput:
    """第二批：同一家公司的简称、一个子公司、一个无关公司、一个歧义名。"""
    result = ExtractionResult(
        entities=[
            # 简称，高相似+名字关联 -> 期望自动并到 华为技术有限公司
            Entity(name="华为", type="ORG", confidence=0.9,
                   mentions=[EntityMention(surface="华为",
                                            span=Span(start=0, end=2, text="华为"))]),
            # 母子公司，同类型且都含"华为"，相似度落在暧昧区 -> 进人工队列
            Entity(name="华为云计算技术有限公司", type="ORG", confidence=0.9,
                   aliases=["华为云"],
                   mentions=[EntityMention(surface="华为云计算技术有限公司",
                                            span=Span(start=10, end=22, text="华为云计算技术有限公司"))]),
            # 明显无关的另一家 -> 直接新建
            Entity(name="腾讯控股有限公司", type="ORG", confidence=0.95,
                   aliases=["腾讯"],
                   mentions=[EntityMention(surface="腾讯",
                                            span=Span(start=30, end=32, text="腾讯"))]),
        ],
        relations=[
            Relation(head="华为", predicate="子公司", tail="华为云计算技术有限公司",
                     confidence=0.85,
                     evidence_span=Span(start=0, end=22, text="华为的子公司华为云")),
        ],
    )
    return ExtractionOutput(document_id="doc_second", domain_code="general",
                            result=result)


async def main() -> None:
    pool = await init_pool()
    store = EntityStore(pool)
    await store.init_schema()
    # 教学库彻底重置（实体为父表，级联清别名/出处/关系/审核队列）
    await pool.execute("TRUNCATE entities RESTART IDENTITY CASCADE")

    judge = LLMJudge() if USE_LLM else RuleBasedJudge()
    disambiguator = Disambiguator(store, judge=judge)
    # 关键：给 Ingestor 装上消歧器
    ingestor = Ingestor(store, disambiguator=disambiguator)

    print("=" * 78)
    print("第 1 批：写入规范实体「华为技术有限公司」")
    print("=" * 78)
    r1 = await ingestor.ingest(output_with_fragmented_huawei())
    print(f"  created={r1.created} merged={r1.merged} queued={r1.queued}")

    # 在第 2 批写入【之前】，干净地展示规则 Judge 对三个名字的判定。
    # 此时库里只有规范实体一个，候选就是它，判定不受后续污染。
    print("\n判定预演（库里只有「华为技术有限公司」，规则 Judge 如何判三个新名字）：")
    for nm in ["华为", "华为云计算技术有限公司", "腾讯控股有限公司"]:
        cands = await disambiguator.candidates_for(nm)
        if not cands:
            print(f"  「{nm}」无候选 -> 直接新建")
            continue
        d = await judge.judge(name=nm, type="ORG", aliases=[], candidate=cands[0])
        print(f"  「{nm}」 vs 「{cands[0].name}」 sim={d.similarity:.3f}"
              f" -> {d.verdict:9} | {d.reason}")

    print("\n" + "=" * 78)
    print("第 2 批：简称「华为」/ 子公司「华为云」/ 无关「腾讯」入库（带消歧）")
    print("=" * 78)
    r2 = await ingestor.ingest(output_second_batch())
    print(f"  自动合并 merged : {r2.merged}")
    print(f"  进审核队列 queued: {r2.queued}")
    print(f"  新建     created: {r2.created}")

    # ---------- HITL：人工审核队列 ----------
    print("\n" + "=" * 78)
    print("HITL：审核台拉取待人工裁决的合并建议")
    print("=" * 78)
    pending = await store.pending_reviews()
    for rv in pending:
        print(f"  [#{rv['id']}] 建议把「{rv['duplicate_name']}」并入"
              f"「{rv['survivor_name']}」 sim={rv['similarity']:.2f}")
        print(f"        理由：{rv['reason']}")

    # 模拟审核员逐条裁决：
    #   「华为」确为同一公司简称 -> 批准合并
    #   「华为云计算技术有限公司」是独立子公司（不同法律实体）-> 拒绝合并
    print("\n审核员逐条裁决：")
    for rv in pending:
        approve = rv["duplicate_name"] == "华为"
        print(f"  [#{rv['id']}] 「{rv['duplicate_name']}」并入"
              f"「{rv['survivor_name']}」-> {'批准 ✅' if approve else '拒绝 ❌（独立子公司）'}")
        await store.decide_review(rv["id"], approved=approve)
    print(f"  剩余待审：{len(await store.pending_reviews())} 条")

    # ---------- 演示一次"批准合并" ----------
    print("\n" + "=" * 78)
    print("补充演示：若人工【批准】合并，外键/别名如何迁移")
    print("=" * 78)
    # 再造一个高相似简称并让它进队/或直接手动建一对，然后批准
    from moray.schema import EntityMention as M, Span as S
    # 直接放两个"审核台确认是同一家"的写法入库，再人工登记并批准一条合并建议。
    # 真实系统里这条建议由消歧器 enqueue；这里手动构造以专注演示"批准合并"动作。
    out3 = ExtractionOutput(
        document_id="doc_third", domain_code="general",
        result=ExtractionResult(entities=[
            Entity(name="华为终端有限公司", type="ORG", confidence=0.8,
                   mentions=[M(surface="华为终端",
                                span=S(start=0, end=4, text="华为终端"))])]),
    )
    r3 = await ingestor.ingest(out3)
    print(f"  「华为终端有限公司」处理结果：created={r3.created} queued={r3.queued}")
    hw_id = await store.get_entity_id("华为技术有限公司")
    term_id = await store.get_entity_id("华为终端有限公司")
    if term_id is not None:
        rid = await store.enqueue_review(
            survivor_id=hw_id, duplicate_id=term_id,
            survivor_name="华为技术有限公司", duplicate_name="华为终端有限公司",
            similarity=0.81, reason="人工核对营业执照，确认为同一集团下需归并的品牌实体")
        rv = next(x for x in await store.pending_reviews() if x["id"] == rid)
        print(f"  审核员批准 [#{rv['id']}]：把「{rv['duplicate_name']}」"
              f"并入「{rv['survivor_name']}」")
        await store.decide_review(rid, approved=True)

    # 验证合并结果：旧名应解析到规范实体，其叫法成为别名
    survivor = await store.find_by_name_or_alias("华为终端有限公司")
    hw = await store.find_by_name_or_alias("华为技术有限公司")
    print(f"\n  合并后按旧名「华为终端有限公司」精确查找 -> {survivor['name'] if survivor else None}")
    print(f"  规范实体仍唯一：{hw['name']}（id={hw['id']}）")

    # 关系外键迁移验证：第二批里 华为 -[子公司]-> 华为云，"华为"被并后关系仍在
    hw_id = hw["id"]
    rels = await store.relations_of(hw_id, direction="out")
    print(f"  「华为」并入后，其关系仍挂在规范实体上：")
    for r in rels:
        print(f"    {r['src_name']} —[{r['predicate']}]→ {r['dst_name']}")

    print("\n  当前实体清单：")
    async with store.pool.acquire() as conn:
        for row in await conn.fetch("SELECT name, type FROM entities ORDER BY name"):
            print(f"    [{row['type']}] {row['name']}")

    await close_pool(pool)
    print("\n# 动手任务：")
    print("# 1. 调 RuleBasedJudge 的 auto_merge_thr / reject_thr，重跑看哪些案例")
    print("#    在 自动合并/人工/新建 之间移动——体会阈值就是成本与风险的旋钮。")
    print("# 2. --llm 让消歧 Agent 判「华为云」，对比规则与模型的理由。")
    print("# 3. 进库 SELECT * FROM merge_reviews; 查看 approved/rejected 审计留痕。")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:  # noqa: BLE001
        print("数据库不可用？请先：docker start moray-pgvector")
        print("错误：", repr(exc)[:300])
