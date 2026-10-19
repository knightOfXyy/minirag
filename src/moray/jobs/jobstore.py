"""
批量任务的持久化仓库（L11）
============================

为什么不用 AgentScope 自带的 Task/AgentState？
  · agentscope.state.AgentState 管的是【一次对话】的 context/reply_id（用于
    多轮记忆和 HITL 恢复）；
  · agentscope.state.Task 只是内存里的 pending/in_progress/completed 模型，
    进程退出就没了。
  它们都不是"跨进程崩溃后，批量文档能接着跑"的持久化任务队列。

本模块用一张 PostgreSQL 表 extraction_jobs 提供：
  · 稳定 job_id 幂等创建（同一文档重复提交只产生一个任务）；
  · 原子认领 claim()：多 worker/多次启动不会重复处理同一篇；
  · 状态机 pending -> running -> succeeded/failed；
  · recover_stale()：进程崩在 running，重启时把孤儿任务收回 pending 重跑；
  · 因为下游入库（L08 Ingestor）本身幂等，重跑既不会重复扣费风险外的脏数据，
    也不会重复写库。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import asyncpg

_SCHEMA_SQL = Path(__file__).with_name("schema.sql")


class JobStatus:
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    TERMINAL = (SUCCEEDED, CANCELLED)  # succeeded 终态；failed 可重试


def dsn() -> str:
    import os

    return (
        f"postgresql://{os.environ.get('POSTGRES_USER','moray')}:"
        f"{os.environ.get('POSTGRES_PASSWORD','moray_dev_2026')}"
        f"@{os.environ.get('POSTGRES_HOST','localhost')}:"
        f"{os.environ.get('POSTGRES_PORT','5433')}/"
        f"{os.environ.get('POSTGRES_DB','moray_kb')}"
    )


class JobRecord:
    """轻量任务记录（dict 的可读包装）。"""

    def __init__(self, row: dict) -> None:
        self._row = row

    def __getitem__(self, key: str) -> Any:
        return self._row[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self._row.get(key, default)

    @property
    def status(self) -> str:
        return self._row["status"]

    @property
    def job_id(self) -> str:
        return self._row["job_id"]

    def as_dict(self) -> dict:
        return dict(self._row)

    def __repr__(self) -> str:
        return f"<Job {self.job_id} {self.status} attempts={self._row['attempts']}>"


class JobStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def init_schema(self) -> None:
        await self.pool.execute(_SCHEMA_SQL.read_text(encoding="utf-8"))

    async def create_job(
        self, *, job_id: str, document_id: str, document_ref: str,
        domain_code: str = "general",
    ) -> tuple[JobRecord, bool]:
        """幂等创建。返回 (记录, 是否新建)。已存在则原样返回旧记录。"""
        row = await self.pool.fetchrow(
            """
            INSERT INTO extraction_jobs (job_id, document_id, document_ref, domain_code)
            VALUES ($1,$2,$3,$4)
            ON CONFLICT (job_id) DO NOTHING
            RETURNING *
            """,
            job_id, document_id, document_ref, domain_code,
        )
        if row is not None:
            return JobRecord(dict(row)), True
        row = await self.pool.fetchrow(
            "SELECT * FROM extraction_jobs WHERE job_id=$1", job_id)
        return JobRecord(dict(row)), False

    async def claim_next(
        self,
        *,
        worker_id: str = "worker",
        lease_seconds: int = 300,
        exclude_seen: set[str] | None = None,
        claim_expired: bool = True,
    ) -> JobRecord | None:
        """原子认领下一个任务并置 running，带租约（lease）。多 worker 安全。

        可认领两类行（按 id 先到先得）：
          1. status='pending' 的新任务；
          2. status='running' 但租约已过期（expires_at < now()）的"疑似僵死"
             任务——前一个 worker 崩溃或卡住，由本 worker 接管重跑。
             claim_expired=False 可只领新任务（如做新任务/接管分通道时）。

        认领地写入：locked_by=worker_id、locked_at=now、
        expires_at=now()+lease_seconds。处理期间应周期性 heartbeat 续租；
        只要活还在干、心跳不断，别的 worker 绝不会抢走它。

        exclude_seen: 本轮已尝试过的 job_id（失败回 pending 后不立即重取，
        重试留到下一轮 run_once）。
        """
        seen = exclude_seen or set()
        row = await self.pool.fetchrow(
            """
            UPDATE extraction_jobs
            SET status='running', attempts=attempts+1,
                started_at=now(), updated_at=now(), error='',
                locked_by=$2, locked_at=now(),
                expires_at=now() + make_interval(secs => $3)
            WHERE id = (
                SELECT id FROM extraction_jobs
                WHERE NOT (job_id = ANY($1::text[]))
                  AND (
                        status='pending'
                        OR ($4 AND status='running' AND expires_at < now())
                      )
                ORDER BY id
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            RETURNING *
            """,
            list(seen), worker_id, lease_seconds, claim_expired,
        )
        return JobRecord(dict(row)) if row else None

    async def heartbeat(
        self, job_id: str, worker_id: str, lease_seconds: int = 300
    ) -> bool:
        """续租：只有当前持有者能续。返回是否续成功（行仍属于本 worker）。

        若返回 False，说明租约已过期并被别人接管——调用方应主动放弃处理
        （继续写库可能与接管者并发；好在 Ingestor 幂等，结果仍正确，只是浪费）。
        """
        val = await self.pool.fetchval(
            """
            UPDATE extraction_jobs
            SET locked_at=now(),
                expires_at=now() + make_interval(secs => $3),
                updated_at=now()
            WHERE job_id=$1 AND locked_by=$2 AND status='running'
            RETURNING 1
            """,
            job_id, worker_id, lease_seconds,
        )
        return val is not None

    async def release(self, job_id: str, worker_id: str) -> None:
        """主动释放：worker 优雅停机时把未做完的任务退回 pending，不必等租约超时。"""
        await self.pool.execute(
            """
            UPDATE extraction_jobs
            SET status='pending', locked_by=NULL, expires_at=NULL,
                updated_at=now()
            WHERE job_id=$1 AND locked_by=$2 AND status='running'
            """,
            job_id, worker_id,
        )

    async def reclaim_expired(self) -> int:
        """把租约过期的 running 任务收回 pending。

        与旧版 recover_stale 的关键区别：只回收【确实过期】的，绝不影响其他
        worker 正在正常心跳的任务。通常无需单独调用——claim_next 会顺带接管，
        这里保留给"只想清理、不处理"的运维场景/启动自检。
        """
        n = await self.pool.fetchval(
            """
            WITH upd AS (
                UPDATE extraction_jobs
                SET status='pending', locked_by=NULL, expires_at=NULL,
                    updated_at=now()
                WHERE status='running' AND expires_at < now()
                RETURNING 1
            )
            SELECT count(*) FROM upd
            """)
        return int(n or 0)

    async def recover_stale(self) -> int:
        """兼容旧接口：等价于 reclaim_expired（只收过期，不再无脑全收）。"""
        return await self.reclaim_expired()

    async def mark_succeeded(self, job_id: str, summary: dict) -> None:
        await self.pool.execute(
            """
            UPDATE extraction_jobs
            SET status='succeeded', finished_at=now(), updated_at=now(),
                result_summary=$2, error='',
                locked_by=NULL, locked_at=NULL, expires_at=NULL
            WHERE job_id=$1
            """,
            job_id, summary or {},  # jsonb 经连接 codec 自动编码
        )

    async def mark_failed(self, job_id: str, error: str) -> None:
        """失败回到 pending（可被重试），记录错误并释放租约。"""
        await self.pool.execute(
            """
            UPDATE extraction_jobs
            SET status='pending', error=$2, updated_at=now(),
                locked_by=NULL, locked_at=NULL, expires_at=NULL
            WHERE job_id=$1
            """,
            job_id, error[:2000],
        )

    async def mark_failed_terminal(self, job_id: str, error: str) -> None:
        await self.pool.execute(
            """
            UPDATE extraction_jobs
            SET status='failed', finished_at=now(), updated_at=now(), error=$2,
                locked_by=NULL, locked_at=NULL, expires_at=NULL
            WHERE job_id=$1
            """,
            job_id, error[:2000],
        )

    async def recover_stale(self) -> int:
        """把崩溃遗留在 running 的任务收回 pending（幂等重跑的前提）。"""
        n = await self.pool.fetchval(
            """
            WITH upd AS (
                UPDATE extraction_jobs SET status='pending', updated_at=now()
                WHERE status='running' RETURNING 1
            )
            SELECT count(*) FROM upd
            """,
        )
        return int(n or 0)

    async def list_jobs(self, status: str | None = None) -> list[JobRecord]:
        if status:
            rows = await self.pool.fetch(
                "SELECT * FROM extraction_jobs WHERE status=$1 ORDER BY id", status)
        else:
            rows = await self.pool.fetch(
                "SELECT * FROM extraction_jobs ORDER BY id")
        return [JobRecord(dict(r)) for r in rows]

    async def counts(self) -> dict[str, int]:
        rows = await self.pool.fetch(
            "SELECT status, count(*)::int AS n FROM extraction_jobs GROUP BY status")
        return {r["status"]: r["n"] for r in rows}

    async def reset_failed(self) -> int:
        """把 failed（终态）重置为 pending，供手动重试。"""
        return int(await self.pool.fetchval(
            """
            WITH upd AS (
                UPDATE extraction_jobs SET status='pending', error=''
                WHERE status='failed' RETURNING 1
            )
            SELECT count(*) FROM upd
            """) or 0)
