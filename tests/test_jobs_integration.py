"""批量任务 / 断点续跑的 DB 集成测试（需要 moray-pgvector）。

用假 handler 模拟成功/失败/崩溃，确定性验证状态机，不调模型。
    RUN_DB_TESTS=1 .venv/bin/python -m pytest tests/test_jobs_integration.py -q
"""

import os

import pytest

from moray.jobs import BatchRunner, JobStatus, JobStore
from moray.kb import close_pool, init_pool

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DB_TESTS") != "1",
    reason="需要 PostgreSQL；设置 RUN_DB_TESTS=1 且启动 moray-pgvector 后运行",
)


@pytest.fixture()
async def jobs():
    pool = await init_pool()
    store = JobStore(pool)
    await store.init_schema()
    await pool.execute("TRUNCATE extraction_jobs RESTART IDENTITY")
    yield store
    await close_pool(pool)


async def _seed(store, n):
    for i in range(n):
        await store.create_job(
            job_id=f"doc-{i}", document_id=f"d{i}", document_ref=f"/tmp/d{i}.txt")


@pytest.mark.asyncio
async def test_idempotent_create(jobs):
    rec, created = await jobs.create_job(
        job_id="x", document_id="d", document_ref="/d.txt")
    assert created is True
    rec2, created2 = await jobs.create_job(
        job_id="x", document_id="d", document_ref="/d.txt")
    assert created2 is False
    assert rec2.job_id == "x"
    assert len(await jobs.list_jobs()) == 1


@pytest.mark.asyncio
async def test_happy_path_all_succeed(jobs):
    await _seed(jobs, 3)

    async def ok(job):
        return {"entities": 2}

    summary = await BatchRunner(jobs, ok, max_attempts=3).run_once()
    assert summary.succeeded == 3
    assert summary.failed == 0
    assert summary.remaining == 0
    counts = await jobs.counts()
    assert counts.get("succeeded") == 3


@pytest.mark.asyncio
async def test_failure_goes_back_pending_then_succeeds(jobs):
    await jobs.create_job(job_id="flaky", document_id="d", document_ref="/f.txt")
    state = {"calls": 0}

    async def flaky(job):
        state["calls"] += 1
        if state["calls"] == 1:
            raise RuntimeError("模型超时")
        return {"entities": 1}

    runner = BatchRunner(jobs, flaky, max_attempts=3)
    s1 = await runner.run_once()
    # 第一次失败回 pending，本轮 claim 不会立刻再拿（已标 pending 但循环会再取到）
    # 第二次成功：
    s2 = await runner.run_once(recover=False)
    assert state["calls"] == 2
    succeeded = await jobs.list_jobs(JobStatus.SUCCEEDED)
    assert succeeded[0]["result_summary"]["entities"] == 1


@pytest.mark.asyncio
async def test_max_attempts_marks_terminal_failed(jobs):
    await jobs.create_job(job_id="bad", document_id="d", document_ref="/b.txt")

    async def always_fail(job):
        raise ValueError("文档格式损坏")

    runner = BatchRunner(jobs, always_fail, max_attempts=2)
    # 每轮对同一任务只试一次：需要跑满 2 轮才到 failed 终态
    r1 = await runner.run_once()
    assert r1.failed == 1
    assert len(await jobs.list_jobs(JobStatus.PENDING)) == 1  # 回 pending 等下轮
    await runner.run_once(recover=False)
    failed = await jobs.list_jobs(JobStatus.FAILED)
    assert len(failed) == 1
    assert "格式损坏" in failed[0]["error"]
    assert failed[0]["attempts"] == 2

    # 终态 failed 不会再被认领；reset_failed 后可重试
    s = await runner.run_once(recover=False)
    assert s.processed == 0
    assert await jobs.reset_failed() == 1
    pending = await jobs.list_jobs(JobStatus.PENDING)
    assert len(pending) == 1


@pytest.mark.asyncio
async def test_crash_recovery_stale_running(jobs):
    await jobs.create_job(job_id="crash", document_id="d", document_ref="/c.txt")
    # 模拟 worker 认领后崩溃：任务停在 running
    job = await jobs.claim_next()
    assert job.status == JobStatus.RUNNING

    # 新进程启动：recover_stale 收回 pending，再跑能正常成功
    assert await jobs.recover_stale() == 1
    async def ok(job):
        return {"entities": 1}
    summary = await BatchRunner(jobs, ok).run_once()
    assert summary.succeeded == 1


@pytest.mark.asyncio
async def test_max_jobs_limit(jobs):
    await _seed(jobs, 5)
    async def ok(job):
        return {}
    summary = await BatchRunner(jobs, ok).run_once(max_jobs=2)
    assert summary.processed == 2
    assert summary.remaining == 3
