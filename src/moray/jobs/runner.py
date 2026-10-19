"""
批量任务运行器（L11）
=====================

把无状态的"抽取→审校→消歧→入库"接进可恢复的批量作业循环：

    启动：recover_stale()  崩溃遗留在 running 的任务收回 pending
    循环：claim_next() 原子认领 -> handler 处理 -> succeeded / 失败回 pending
    终止：没有 pending 任务（或达到 max_jobs）

可恢复性来自两点：
  1. 任务状态持久化在 PostgreSQL，进程重启不丢；
  2. 下游处理必须【幂等】（我们的 Ingestor 已满足）：同一文档重跑不会产生
     重复实体/关系。handler 抛错时任务回 pending，下次重试安全。

handler 由调用方注入：async (JobRecord) -> dict(成功统计)。
这样运行器与"具体怎么抽取/入哪个库"完全解耦，离线测试可注入假 handler。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .jobstore import JobRecord, JobStore

# 处理函数：接收任务记录，返回成功统计 dict；抛异常视为失败
JobHandler = Callable[[JobRecord], Awaitable[dict]]


@dataclass
class BatchRunSummary:
    processed: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    remaining: int = 0
    failed_jobs: list[str] = field(default_factory=list)

    def summary_line(self) -> str:
        return (
            f"处理 {self.processed}：成功 {self.succeeded}，失败 {self.failed}，"
            f"跳过 {self.skipped}，剩余 pending {self.remaining}"
        )


class BatchRunner:
    def __init__(
        self,
        store: JobStore,
        handler: JobHandler,
        *,
        max_attempts: int = 3,
    ) -> None:
        self.store = store
        self.handler = handler
        # 同一任务最多自动重试次数；超过转 failed 终态，需人工 reset_failed
        self.max_attempts = max_attempts

    async def run_once(
        self, *, max_jobs: int | None = None, recover: bool = True
    ) -> BatchRunSummary:
        """跑一轮：把当前 pending 任务尽量处理掉（可选上限）。"""
        if recover:
            recovered = await self.store.recover_stale()
            if recovered:
                # 仅用于调用方感知；具体打印由上层决定
                pass

        summary = BatchRunSummary()
        processed = 0
        seen: set[str] = set()  # 同一轮内每个任务只认领一次，重试留到下一轮
        while max_jobs is None or processed < max_jobs:
            job = await self.store.claim_next(exclude_seen=seen)
            if job is None:
                break
            seen.add(job.job_id)
            processed += 1
            summary.processed += 1
            try:
                result = await self.handler(job)
                await self.store.mark_succeeded(job.job_id, result or {})
                summary.succeeded += 1
            except Exception as exc:  # noqa: BLE001
                summary.failed += 1
                summary.failed_jobs.append(job.job_id)
                err = f"{type(exc).__name__}: {exc}"
                # 超过重试上限 -> failed 终态；否则回 pending 等待下一轮
                if int(job["attempts"]) >= self.max_attempts:
                    await self.store.mark_failed_terminal(job.job_id, err)
                else:
                    await self.store.mark_failed(job.job_id, err)

        counts = await self.store.counts()
        summary.remaining = counts.get("pending", 0)
        return summary

    async def run_to_completion(
        self, *, max_jobs: int | None = None, recover: bool = True
    ) -> BatchRunSummary:
        """语义同 run_once（单进程顺序处理时等价）；保留命名供未来并发扩展。"""
        return await self.run_once(max_jobs=max_jobs, recover=recover)
