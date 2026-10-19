"""
================================================================================
L11 · 状态与 Session：批量文档任务、断点续跑、崩溃恢复
================================================================================

先厘清一个概念（对照 agentscope 2.0.10 源码）：
  · AgentState（session_id / context / reply_id）管的是【一次对话】的多轮记忆
    与 HITL 恢复；
  · state.Task 只是内存里的 pending/in_progress/completed 待办，进程退出即丢。
  它们都不提供"一批文档跨进程崩溃后接着跑"的持久化。

企业里真正需要的是【持久化作业队列】，本课自建（复用已在跑的 PostgreSQL）：

  extraction_jobs 表 + JobStore（幂等创建/原子认领/状态机/孤儿回收）
        │
  BatchRunner.run_once():
     recover_stale() -> claim_next() 原子认领 -> handler -> succeeded/failed
        │
  handler（jobs/handlers.py）：读文档 -> L10 抽取审校闭环 -> L08/L09 幂等入库

断点续跑成立的两大支柱：
  ① 任务状态持久化：崩溃后 running 孤儿任务重启时收回 pending；
  ② 处理幂等：Ingestor 按 (domain,type,name) 幂等，重跑不产生重复实体/关系，
     所以"至少处理一次(at-least-once)"也不会脏库。

本脚本离线：用确定性的"关键词抽取器"代替 LLM，配合本地 bge 入库，
可反复、零花费地演示 提交/崩溃/恢复/重复提交。
运行： python lessons/l11_batch_resume.py   （前置 docker start moray-pgvector）
================================================================================
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from moray.extraction.document import Document, ExtractionOutput
from moray.jobs import BatchRunner, JobStore, JobStatus
from moray.jobs.handlers import build_document_handler
from moray.kb import EntityStore, Ingestor, close_pool, init_pool
from moray.schema import Entity, EntityMention, ExtractionResult, Relation, Span

BATCH_DIR = ROOT / "data/samples/batch"


# ---------------------------------------------------------------------------
# 离线确定性"抽取器"：针对本批三篇样例，按关键词抽出实体/关系并自动算 span。
# 真实系统里换成 Extractor Agent（L04）或 L10 团队即可，handler/runner 不变。
# ---------------------------------------------------------------------------
_KNOWLEDGE = {
    "腾讯": ("腾讯控股有限公司", "ORG", "深圳", "LOC", "马化腾", "PER"),
    "阿里": ("阿里巴巴集团控股有限公司", "ORG", "杭州", "LOC", "马云", "PER"),
    "比亚迪": ("比亚迪股份有限公司", "ORG", "深圳", "LOC", "王传福", "PER"),
}


async def deterministic_extractor(text: str, feedback) -> ExtractionResult:
    key = next((k for k in _KNOWLEDGE if k in text), None)
    assert key, f"离线抽取器不认识该文本：{text[:20]}"
    org, _, city, _, person, _ = _KNOWLEDGE[key]

    def mention(name):
        i = text.find(name)
        return EntityMention(surface=name,
                             span=Span(start=i, end=i + len(name), text=name))

    def rel_span(pred_substr: str):
        i = text.find(pred_substr)
        return Span(start=i, end=i + len(pred_substr), text=pred_substr) if i >= 0 else None

    entities = [
        Entity(name=org, type="ORG", confidence=0.9, mentions=[mention(org)]),
        Entity(name=city, type="LOC", confidence=0.9, mentions=[mention(city)]),
        Entity(name=person, type="PER", confidence=0.9, mentions=[mention(person)]),
    ]
    relations = [
        Relation(head=org, predicate="总部位于", tail=city, confidence=0.85),
        Relation(head=person, predicate="创立", tail=org, confidence=0.85),
    ]
    return ExtractionResult(entities=entities, relations=relations)


def _job_id(path: Path) -> str:
    return "job-" + hashlib.sha1(path.name.encode()).hexdigest()[:10]


async def show_state(store: JobStore) -> None:
    print("  任务状态：", await store.counts())


async def main() -> None:
    pool = await init_pool()
    jobs = JobStore(pool)
    await jobs.init_schema()
    entity_store = EntityStore(pool)
    await entity_store.init_schema()

    # 教学重置：清任务；为避免互相干扰只清本批相关实体（直接整库重置便于观察）
    await pool.execute("TRUNCATE extraction_jobs RESTART IDENTITY")
    await pool.execute("TRUNCATE entities RESTART IDENTITY CASCADE")

    ingestor = Ingestor(entity_store)  # 不挂消歧器，专注演示作业状态
    handler = await build_document_handler(
        store=entity_store, ingestor=ingestor,
        extractor_fn=deterministic_extractor)
    runner = BatchRunner(jobs, handler, max_attempts=2)

    # ---------- 1) 提交一批文档（幂等：重复提交不新增任务） ----------
    print("=" * 78)
    print("1) 提交 data/samples/batch 下的文档")
    print("=" * 78)
    files = sorted(BATCH_DIR.glob("*.txt"))
    for p in files:
        doc_id = p.stem
        _, created = await jobs.create_job(
            job_id=_job_id(p), document_id=doc_id,
            document_ref=str(p), domain_code="general")
        print(f"  提交 {p.name:10} -> {'新建任务' if created else '已存在(幂等忽略)'}")
    # 再提交一次同样的文件，验证幂等
    p0 = files[0]
    _, created = await jobs.create_job(
        job_id=_job_id(p0), document_id=p0.stem,
        document_ref=str(p0), domain_code="general")
    print(f"  重复提交 {p0.name} -> {'新建任务' if created else '已存在(幂等忽略)'}")
    await show_state(jobs)

    # ---------- 2) 只处理前 2 个，模拟"分批跑/没跑完" ----------
    print("\n" + "=" * 78)
    print("2) 本轮最多处理 2 个任务（max_jobs=2）")
    print("=" * 78)
    s = await runner.run_once(max_jobs=2)
    print(" ", s.summary_line())
    await show_state(jobs)

    # ---------- 3) 模拟崩溃：手工把一个 succeeded 之外的任务置 running ----------
    print("\n" + "=" * 78)
    print("3) 模拟进程崩溃：把剩余任务强行置为 running 后'进程死亡'")
    print("=" * 78)
    await pool.execute(
        "UPDATE extraction_jobs SET status='running' WHERE status='pending'")
    print("  崩溃时刻：", await jobs.counts(), "（running 是无人接管的孤儿）")

    # ---------- 4) 新进程启动：recover_stale 回收孤儿，继续跑完 ----------
    print("\n" + "=" * 78)
    print("4) 新进程启动：recover_stale() 回收孤儿 -> 继续处理")
    print("=" * 78)
    s2 = await runner.run_once()  # 默认 recover=True
    print(" ", s2.summary_line())
    await show_state(jobs)

    # ---------- 5) 再来一轮：已全部 succeeded，不应再处理（不重复花钱/写库） ----------
    print("\n" + "=" * 78)
    print("5) 空跑一轮：终态任务不会被重复处理")
    print("=" * 78)
    before = await entity_store.count()
    s3 = await runner.run_once()
    after = await entity_store.count()
    print(" ", s3.summary_line(), f"| 实体数 {before}->{after}（不变，幂等）")

    # ---------- 6) 失败重试上限 -> failed 终态 + reset 手动重试 ----------
    print("\n" + "=" * 78)
    print("6) 毒丸任务：连续失败超过 max_attempts 转 failed，可 reset 后重试")
    print("=" * 78)
    poison = ROOT / "data/samples/batch/_poison.txt"
    poison.write_text("这是一段没有任何已知公司的文本。", encoding="utf-8")
    await jobs.create_job(job_id="poison", document_id="_poison",
                          document_ref=str(poison), domain_code="general")
    # 每轮只试一次：跑满 max_attempts(=2) 轮后进入 failed 终态
    for rnd in (1, 2):
        s4 = await runner.run_once()
        print(f"  第 {rnd} 轮：{s4.summary_line()}")
        st = await jobs.counts()
        if "failed" in st:
            break
    bad = await jobs.list_jobs(JobStatus.FAILED)
    for b in bad:
        print(f"   failed: {b['job_id']} attempts={b['attempts']} "
              f"error={b['error'][:50]}")

    # 演示手动恢复：修好问题后 reset_failed 可重新入队（这里直接删除毒丸文件）
    poison.unlink()
    print("  reset_failed 可把 failed 拉回 pending 数：", await jobs.reset_failed(),
          "（修好后即可重跑；此处为不报错再次清理掉该任务）")
    await pool.execute("DELETE FROM extraction_jobs WHERE job_id='poison'")

    # ---------- 汇总入库结果 ----------
    print("\n" + "=" * 78)
    print("知识库里最终的实体（来自三篇文档，幂等无重复）")
    print("=" * 78)
    async with pool.acquire() as conn:
        for row in await conn.fetch(
                "SELECT type, name FROM entities ORDER BY type, name"):
            print(f"  [{row['type']}] {row['name']}")
        n_rel = await conn.fetchval("SELECT count(*) FROM relations")
    print(f"  关系总数：{n_rel}")

    await close_pool(pool)
    print("\n# 动手任务：")
    print("# 1. 在处理第 2 个任务后 Ctrl-C 强杀进程，再重启脚本，验证真实崩溃恢复。")
    print("# 2. 把 max_attempts 调成 1，观察毒丸任务是否更快进入 failed 终态。")
    print("# 3. 思考：为什么 succeeded 设为终态、failed 却默认可重试？")
    print("#    （提示：确定性成功 vs 可能是瞬时故障的失败）")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:  # noqa: BLE001
        print("数据库不可用？请先：docker start moray-pgvector")
        print("错误：", repr(exc)[:300])
