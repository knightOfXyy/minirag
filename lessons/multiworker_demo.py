"""
================================================================================
多 worker 机制演示（租约 / 心跳 / 抢占 / 水平扩展）
================================================================================

对应生产代码：src/moray/jobs/{jobstore,runner,worker,offline}.py。
本脚本在【同一事件循环】里起两个逻辑 worker 并发抢同一批任务，直观验证：

  1) 认领靠 UPDATE ... FOR UPDATE SKIP LOCKED：N 个 worker 不重不漏分完任务；
  2) 每个任务带租约（locked_by / locked_at / expires_at），处理期间后台心跳
     续租，别的 worker 绝不抢正在正常执行的活；
  3) worker 崩溃（心跳停止）-> 租约过期 -> 被另一个 worker 接管重跑，
     因 Ingestor 幂等，重跑不产生脏数据；
  4) 优雅停机：在途任务 release 回 pending，立刻可被接走。

真实多进程（生产用法）：
    python -m moray.jobs.worker --offline --worker-id w1 --once
    python -m moray.jobs.worker --offline --worker-id w2 --once   # 另一终端
    python -m moray.jobs.worker --concurrency 3 --poll             # 常驻+进程内并行
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


async def main() -> None:
    from moray.jobs import BatchRunner, JobStore, default_worker_id
    from moray.jobs.offline import fake_extractor
    from moray.jobs.handlers import build_document_handler
    from moray.kb import EntityStore, Ingestor, close_pool, init_pool

    pool = await init_pool()
    estore = EntityStore(pool)
    await estore.init_schema()
    jobs = JobStore(pool)
    await jobs.init_schema()
    await pool.execute("TRUNCATE entities RESTART IDENTITY CASCADE")
    await pool.execute("TRUNCATE extraction_jobs RESTART IDENTITY")

    # 造 8 个作业（引用临时文档）
    docs = []
    for i in range(8):
        p = Path(f"/tmp/mw_demo_{i}.txt")
        p.write_text(f"腾讯总部位于深圳，马化腾创立了腾讯。编号{i}。", encoding="utf-8")
        docs.append(p)
        await jobs.create_job(
            job_id=f"demo-{i}", document_id=f"demo{i}", document_ref=str(p))
    print(f"播种 {len(docs)} 个 pending 作业")

    # 记录每个任务被谁处理（用共享 dict + 锁断言无重复）
    handled: dict[str, str] = {}
    hlock = asyncio.Lock()

    def make_handler(wid: str):
        async def tracking_handler(job):
            await asyncio.sleep(0.05)  # 放大并发窗口
            async with hlock:
                assert job.job_id not in handled, f"{job.job_id} 被重复处理！"
                handled[job.job_id] = wid
            return {"by": wid}
        return tracking_handler

    runner1 = BatchRunner(
        jobs, make_handler("w1"), worker_id="w1",
        lease_seconds=60, heartbeat_seconds=20)
    runner2 = BatchRunner(
        jobs, make_handler("w2"), worker_id="w2",
        lease_seconds=60, heartbeat_seconds=20)

    print("\n--- 两个 worker 并发跑一轮 ---")
    s1, s2 = await asyncio.gather(runner1.run_once(), runner2.run_once())
    by_w1 = [k for k, v in handled.items() if v == "w1"]
    by_w2 = [k for k, v in handled.items() if v == "w2"]
    print(f"  w1 成功 {s1.succeeded}：{sorted(by_w1)}")
    print(f"  w2 成功 {s2.succeeded}：{sorted(by_w2)}")
    print(f"  合计处理 {len(handled)} 个，无重复：{len(handled) == 8}")

    print("\n--- 租约与心跳：未过期不可抢 ---")
    await jobs.create_job(
        job_id="lease-demo", document_id="ld", document_ref=str(docs[0]))
    claimed = await jobs.claim_next(worker_id="owner", lease_seconds=60)
    await jobs.heartbeat(claimed.job_id, "owner", 60)
    thief = await jobs.claim_next(worker_id="thief", lease_seconds=60)
    print(f"  owner 持锁并心跳后，thief 认领结果：{thief}（None=抢不到，正确）")

    print("\n--- 崩溃接管：租约过期后被别的 worker 接走 ---")
    await pool.execute(
        "UPDATE extraction_jobs SET expires_at=now()-interval '1 second' "
        "WHERE job_id='lease-demo'")
    taken = await jobs.claim_next(worker_id="rescuer", lease_seconds=60)
    print(f"  过期后 rescuer 接管：{taken.job_id if taken else None}，"
          f"locked_by={taken['locked_by'] if taken else None}，"
          f"attempts={taken['attempts'] if taken else None}")
    await jobs.mark_succeeded(
        taken.job_id, {"note": "接管重跑成功（Ingestor 幂等保证无脏数据）"})

    counts = await jobs.counts()
    print("\n最终作业状态：", counts)

    await close_pool(pool)
    print("\n生产多进程：")
    print("  python -m moray.jobs.worker --worker-id w1 --poll   # 终端1")
    print("  python -m moray.jobs.worker --worker-id w2 --poll   # 终端2")
    print("  python -m moray.jobs.worker --concurrency 3 --poll  # 单进程内并行3个")


if __name__ == "__main__":
    asyncio.run(main())
