"""
批量抽取 worker 守护进程（多 worker 机制的入口）
================================================

独立于 API 服务运行，轮询 extraction_jobs 并调用 L11 handler
（L10 抽取审校 -> L08/09 入库 -> L13 看板）。可以【同时起多个进程】水平扩展，
靠数据库租约（claim_next 的 FOR UPDATE SKIP LOCKED + lease + 心跳）互斥。

用法：
    # 真实抽取（需要 .env 的 ARK 凭证；默认不并发，省钱稳妥）
    python -m moray.jobs.worker

    # 4 进程各跑各的（终端 / 容器 / k8s Pod 各起一份即可）
    python -m moray.jobs.worker --worker-id w1
    python -m moray.jobs.worker --worker-id w2

    # 单进程内并行 3 个任务（贴合 LLM RPM/TPM 配额），租约 10 分钟
    python -m moray.jobs.worker --concurrency 3 --lease-seconds 600

    # 常驻轮询：队列清空后每 10s 再看一次，Ctrl-C 优雅退出（在途任务释放/完成）
    python -m moray.jobs.worker --poll --poll-interval 10

    # 离线教学：确定性假抽取，不调 LLM（演示多 worker 分发/抢占用）
    python -m moray.jobs.worker --offline --once
"""

from __future__ import annotations

import argparse
import asyncio
import signal
from typing import Callable

from ..kb import EntityStore, Ingestor, close_pool, init_pool
from .handlers import build_document_handler
from .jobstore import JobStore
from .runner import BatchRunner, default_worker_id


async def _build_runner(
    *, worker_id: str, concurrency: int, lease_seconds: int,
    offline: bool, use_dashboard: bool,
    extractor_fn: Callable | None = None,
) -> tuple[BatchRunner, object]:
    pool = await init_pool()
    store = EntityStore(pool)
    await store.init_schema()

    jobs = JobStore(pool)
    await jobs.init_schema()

    ingestor = Ingestor(store)
    dashboard = None
    if use_dashboard:
        from ..observability import Dashboard
        dashboard = Dashboard(pool)
        await dashboard.init_schema()

    if extractor_fn is None:
        if offline:
            from .offline import fake_extractor
            extractor_fn = fake_extractor
        else:
            from ..extraction import Extractor
            from ..extraction.document import Document as _Doc

            service = Extractor("general", max_retries=1)

            async def extractor_fn(text: str, feedback: str | None):
                body = text
                if feedback:
                    body = text + "\n\n【上次问题，请务必修正】\n" + feedback
                out = await service.extract(
                    _Doc(id="worker-live", text=body, source="worker"))
                return out.result

    handler = await build_document_handler(
        store=store, ingestor=ingestor, extractor_fn=extractor_fn,
        source="worker", dashboard=dashboard,
        model_name="offline-fake" if offline else "ark-llm")

    runner = BatchRunner(
        jobs, handler, worker_id=worker_id,
        lease_seconds=lease_seconds, concurrency=concurrency)
    return runner, pool


async def run_worker(args: argparse.Namespace) -> None:
    runner, pool = await _build_runner(
        worker_id=args.worker_id, concurrency=args.concurrency,
        lease_seconds=args.lease_seconds, offline=args.offline,
        use_dashboard=not args.no_dashboard)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, runner.request_stop)
        except NotImplementedError:  # Windows
            pass

    print(f"[{runner.worker_id}] 启动：concurrency={args.concurrency} "
          f"lease={args.lease_seconds}s offline={args.offline} "
          f"mode={'once' if args.once else 'poll'}")
    try:
        while not runner._stop.is_set():
            summary = await runner.run_once()
            print(f"[{runner.worker_id}] {summary.summary_line()}")
            if args.once or not args.poll:
                break
            # 轮询等待，可被停机信号立即唤醒
            try:
                await asyncio.wait_for(
                    runner._stop.wait(), timeout=args.poll_interval)
            except asyncio.TimeoutError:
                pass
    finally:
        await close_pool(pool)
        print(f"[{runner.worker_id}] 已停止，连接池关闭")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Moray 批量抽取 worker")
    p.add_argument("--worker-id", default=None, help="缺省自动生成 主机-pid-uuid")
    p.add_argument("--concurrency", type=int, default=1, help="单进程内并行任务数")
    p.add_argument("--lease-seconds", type=int, default=300, help="任务租约时长")
    p.add_argument("--poll", action="store_true", help="常驻轮询（默认只跑一轮）")
    p.add_argument("--poll-interval", type=float, default=10.0)
    p.add_argument("--once", action="store_true", help="处理一轮后退出")
    p.add_argument("--offline", action="store_true", help="用确定性假抽取，不调 LLM")
    p.add_argument("--no-dashboard", action="store_true")
    args = p.parse_args(argv)
    if not args.once and not args.poll:
        args.once = True  # 默认一次性，适合 cron/k8s Job
    if args.worker_id is None:
        args.worker_id = default_worker_id()
    asyncio.run(run_worker(args))


if __name__ == "__main__":
    main()
