"""多 worker 机制的 DB 集成测试（租约 / 心跳 / 并发 / 抢占 / 优雅停机）。

需要 moray-pgvector：
    RUN_DB_TESTS=1 .venv/bin/python -m pytest tests/test_multiworker_integration.py -q
"""

import os

import asyncio

import pytest

from moray.jobs import BatchRunner, JobStatus, JobStore
from moray.kb import close_pool, init_pool

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DB_TESTS") != "1",
    reason="需要 PostgreSQL；设置 RUN_DB_TESTS=1 且启动 moray-pgvector 后运行",
)


@pytest.fixture()
async def store():
    pool = await init_pool()
    s = JobStore(pool)
    await s.init_schema()
    await pool.execute("TRUNCATE extraction_jobs RESTART IDENTITY")
    yield s
    await close_pool(pool)


async def _seed(store: JobStore, n: int, prefix: str = "doc"):
    for i in range(n):
        await store.create_job(
            job_id=f"{prefix}-{i}", document_id=f"{prefix}{i}",
            document_ref=f"/tmp/{prefix}{i}.txt")


@pytest.mark.asyncio
async def test_two_workers_never_claim_same_job(store):
    """跨进程互斥：两个 worker 各跑一轮，合起来不重不漏处理全部任务。"""
    await _seed(store, 6)
    handled: dict[str, str] = {}
    lock = asyncio.Lock()

    def make_handler(tag):
        async def handler(job):
            await asyncio.sleep(0.01)  # 制造重叠窗口
            async with lock:
                # 同一任务绝不应被两个 worker 都处理
                assert job.job_id not in handled
                handled[job.job_id] = tag
            return {"by": tag}
        return handler

    w1 = BatchRunner(store, make_handler("w1"), worker_id="w1",
                     lease_seconds=60, heartbeat_seconds=20)
    w2 = BatchRunner(store, make_handler("w2"), worker_id="w2",
                     lease_seconds=60, heartbeat_seconds=20)
    r1, r2 = await asyncio.gather(w1.run_once(), w2.run_once())

    assert r1.succeeded + r2.succeeded == 6
    assert len(handled) == 6
    counts = await store.counts()
    assert counts.get("succeeded") == 6
    assert counts.get("running", 0) == 0


@pytest.mark.asyncio
async def test_lease_heartbeat_prevents_theft(store):
    """持有方正常心跳、租约未过期：另一个 worker 不能接管。"""
    await store.create_job(
        job_id="long", document_id="d", document_ref="/long.txt")
    job = await store.claim_next(worker_id="owner", lease_seconds=60)
    assert job["locked_by"] == "owner"

    # owner 持续续租
    assert await store.heartbeat(job.job_id, "owner", 60) is True

    # 另一个 worker 想领：不应领走到期任务（未过期），只能领 pending，这里没有
    stolen = await store.claim_next(worker_id="thief", lease_seconds=60)
    assert stolen is None


@pytest.mark.asyncio
async def test_expired_lease_is_taken_over(store):
    """持有方崩溃（心跳停止）导致租约过期：被别的 worker 接管，locked_by 易主。"""
    await store.create_job(
        job_id="dead", document_id="d", document_ref="/dead.txt")
    job = await store.claim_next(worker_id="dead-w", lease_seconds=60)
    await store.pool.execute(
        "UPDATE extraction_jobs SET expires_at=now()-interval '10 seconds'")

    taken = await store.claim_next(worker_id="alive-w", lease_seconds=60)
    assert taken is not None and taken.job_id == "dead"
    assert taken["locked_by"] == "alive-w"
    assert taken["attempts"] == 2  # 接管也算一次重试


@pytest.mark.asyncio
async def test_heartbeat_fails_after_takeover(store):
    """被接管后，原持有者的心跳返回 False（据此放弃状态写入）。"""
    await store.create_job(
        job_id="j", document_id="d", document_ref="/j.txt")
    job = await store.claim_next(worker_id="a", lease_seconds=60)
    await store.pool.execute(
        "UPDATE extraction_jobs SET expires_at=now()-interval '1 second'")
    await store.claim_next(worker_id="b", lease_seconds=60)
    assert await store.heartbeat(job.job_id, "a", 60) is False
    assert await store.heartbeat(job.job_id, "b", 60) is True


@pytest.mark.asyncio
async def test_inprocess_concurrency(store):
    """单进程 concurrency=N：任务并行执行，全部成功。"""
    await _seed(store, 5)
    active = 0
    max_active = 0
    lock = asyncio.Lock()

    async def slow_handler(job):
        nonlocal active, max_active
        async with lock:
            active += 1
            max_active = max(max_active, active)
        await asyncio.sleep(0.05)
        async with lock:
            active -= 1
        return {}

    runner = BatchRunner(store, slow_handler, worker_id="par", concurrency=3)
    summary = await runner.run_once()
    assert summary.succeeded == 5
    assert max_active >= 2  # 确实发生了并行（而非纯串行）


@pytest.mark.asyncio
async def test_graceful_stop_releases_inflight(store):
    """处理途中收到停止信号：在途任务被 release 回 pending，可被别人接走。"""
    await store.create_job(
        job_id="inflight", document_id="d", document_ref="/i.txt")

    async def blocking_handler(job):
        await asyncio.sleep(0.2)
        raise RuntimeError("stop during work")

    runner = BatchRunner(
        store, blocking_handler, worker_id="stopping", lease_seconds=60,
        heartbeat_seconds=20)

    async def stop_soon():
        await asyncio.sleep(0.05)
        runner.request_stop()

    await asyncio.gather(runner.run_once(), stop_soon())

    # 停机导致 handler 报错，runner 走 release 分支 -> 回 pending
    pending = await store.list_jobs(JobStatus.PENDING)
    assert any(j.job_id == "inflight" for j in pending)
    # 紧接着可被另一个 worker 正常处理
    async def ok(job):
        return {"entities": 1}
    s = await BatchRunner(store, ok, worker_id="next",
                          lease_seconds=60, heartbeat_seconds=20).run_once()
    assert s.succeeded == 1


@pytest.mark.asyncio
async def test_terminal_state_clears_lease_fields(store):
    await store.create_job(
        job_id="z", document_id="d", document_ref="/z.txt")

    async def ok(job):
        return {}

    await BatchRunner(store, ok, worker_id="w", lease_seconds=60,
                      heartbeat_seconds=20).run_once()
    row = await store.pool.fetchrow(
        "SELECT status, locked_by, locked_at, expires_at "
        "FROM extraction_jobs WHERE job_id='z'")
    assert row["status"] == JobStatus.SUCCEEDED
    assert row["locked_by"] is None
    assert row["expires_at"] is None
