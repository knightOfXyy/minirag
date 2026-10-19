"""
批量任务运行器（L11；L15+ 扩展为多 worker 安全）
==================================================

把无状态的"抽取→审校→消歧→入库"接进可恢复的批量作业循环：

    启动：reclaim_expired()  把【租约过期】的僵死任务收回 pending
    循环：claim_next() 原子认领（带 lease）-> handler 处理（后台心跳续租）
          -> succeeded / 失败回 pending / 超限 failed
    终止：没有可领任务（或达到 max_jobs），或收到停止信号

多 worker 怎么保证不打架：
  · 认领是一条 UPDATE ... FOR UPDATE SKIP LOCKED，跨进程原子：两个 worker
    绝不会领到同一行；
  · 每个任务带租约 expires_at：处理期间心跳不断就不会被别人抢走；只有 worker
    崩溃/卡死、租约真的过期后，任务才会被另一个 worker 接管重跑；
  · 下游 handler 必须幂等（Ingestor 已满足），所以"接管重跑"只会重复劳动、
    不会产生脏数据。

单进程内还可以用 concurrency=N 并行处理多个任务（贴合 LLM 的 RPM/TPM 配额）；
跨进程的互斥始终由数据库租约保证，与进程内并发正交。

handler 由调用方注入：async (JobRecord) -> dict(成功统计)。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .jobstore import JobRecord, JobStore

# 处理函数：接收任务记录，返回成功统计 dict；抛异常视为失败
JobHandler = Callable[[JobRecord], Awaitable[dict]]


def default_worker_id() -> str:
    """可读且跨进程唯一的 worker 标识：主机名-pid-短uuid。"""
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"


@dataclass
class BatchRunSummary:
    processed: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    reclaimed: int = 0  # 接管的过期任务数
    remaining: int = 0
    failed_jobs: list[str] = field(default_factory=list)

    def summary_line(self) -> str:
        return (
            f"处理 {self.processed}：成功 {self.succeeded}，失败 {self.failed}，"
            f"接管过期 {self.reclaimed}，跳过 {self.skipped}，"
            f"剩余可领 {self.remaining}"
        )


class BatchRunner:
    def __init__(
        self,
        store: JobStore,
        handler: JobHandler,
        *,
        max_attempts: int = 3,
        worker_id: str | None = None,
        lease_seconds: int = 300,
        heartbeat_seconds: int | None = None,
        concurrency: int = 1,
    ) -> None:
        self.store = store
        self.handler = handler
        # 同一任务最多自动重试次数；超过转 failed 终态，需人工 reset_failed
        self.max_attempts = max_attempts
        self.worker_id = worker_id or default_worker_id()
        self.lease_seconds = lease_seconds
        # 心跳间隔默认取租约的 1/3，留足两次重试余量
        self.heartbeat_seconds = (
            heartbeat_seconds if heartbeat_seconds is not None
            else max(5, lease_seconds // 3))
        if concurrency < 1:
            raise ValueError("concurrency 必须 >= 1")
        self.concurrency = concurrency
        # 优雅停机信号：置位后不再认领新任务，在途任务尽量做完/释放
        self._stop = asyncio.Event()

    def request_stop(self) -> None:
        self._stop.set()

    # ----------------------------------------------------------- 单任务执行
    async def _run_one(self, job: JobRecord) -> str:
        """执行一个已认领任务，含后台心跳。返回 'succeeded'/'failed'/'retry'。"""
        stop_beat = asyncio.Event()
        lease_lost = False  # 租约被别的 worker 接管后置真

        async def heartbeat_loop() -> None:
            nonlocal lease_lost
            while not stop_beat.is_set():
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(
                        stop_beat.wait(), timeout=self.heartbeat_seconds)
                if stop_beat.is_set():
                    return
                ok = await self.store.heartbeat(
                    job.job_id, self.worker_id, self.lease_seconds)
                if not ok:
                    # 租约已失效并被他人接管：不再续租，handler 跑完也不落状态
                    # （Ingestor 幂等，重复劳动无害；这里避免与接管者争抢状态）
                    lease_lost = True
                    return

        beat_task = asyncio.create_task(heartbeat_loop())
        try:
            result = await self.handler(job)
            if lease_lost:
                return "retry"  # 交给接管者；本 worker 不更新该任务状态
            await self.store.mark_succeeded(job.job_id, result or {})
            return "succeeded"
        except Exception as exc:  # noqa: BLE001
            err = f"{type(exc).__name__}: {exc}"
            if lease_lost:
                return "retry"
            # attempts 在认领时已 +1；超限进终态，否则回 pending 下轮重试。
            # 停机中则主动释放，不必等下一轮认领。
            if self._stop.is_set():
                await self.store.release(job.job_id, self.worker_id)
                return "retry"
            if int(job["attempts"]) >= self.max_attempts:
                await self.store.mark_failed_terminal(job.job_id, err)
                return "failed"
            await self.store.mark_failed(job.job_id, err)
            return "retry"
        finally:
            stop_beat.set()
            beat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await beat_task

    # ------------------------------------------------------------------ 一轮
    async def run_once(
        self, *, max_jobs: int | None = None, recover: bool = True
    ) -> BatchRunSummary:
        """跑一轮：用 concurrency 个槽位并行把可领任务尽量处理掉。"""
        summary = BatchRunSummary()
        if recover:
            summary.reclaimed = await self.store.reclaim_expired()

        seen: set[str] = set()  # 本轮每个任务只认领一次，重试留下一轮
        sem = asyncio.Semaphore(self.concurrency)
        active: dict[str, asyncio.Task] = {}
        processed = 0

        async def worker_slot(job: JobRecord) -> None:
            async with sem:
                outcome = await self._run_one(job)
            summary.processed += 1
            if outcome == "succeeded":
                summary.succeeded += 1
            elif outcome == "failed":
                summary.failed += 1
                summary.failed_jobs.append(job.job_id)

        # 填满并发槽：只要没到上限、没收到停止信号，就持续认领并派发
        while not self._stop.is_set() and (
                max_jobs is None or processed < max_jobs):
            job = await self.store.claim_next(
                worker_id=self.worker_id,
                lease_seconds=self.lease_seconds,
                exclude_seen=seen)
            if job is None:
                break
            seen.add(job.job_id)
            processed += 1
            active[job.job_id] = asyncio.create_task(worker_slot(job))
            # 达到并发上限则等任意一个完成，腾出槽位再继续认领
            running = [t for t in active.values() if not t.done()]
            if len(running) >= self.concurrency:
                await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)

        # 等待所有在途任务结束（优雅停机时 handler 完成或主动 release）
        if active:
            await asyncio.gather(
                *active.values(), return_exceptions=True)

        counts = await self.store.counts()
        summary.remaining = counts.get("pending", 0) + counts.get(
            "running", 0)
        return summary

    async def run_to_completion(
        self, *, max_jobs: int | None = None, recover: bool = True
    ) -> BatchRunSummary:
        """语义同 run_once（单进程内已并发）；保留命名兼容 L11 调用方。"""
        return await self.run_once(max_jobs=max_jobs, recover=recover)
