"""
HTTP 服务启动入口（L14）
========================

从环境变量装配 AppDeps 并启动 uvicorn：

    python -m moray.api.server                 # 查询/HITL/看板/作业（无需 LLM）
    python -m moray.api.server --llm           # 额外开启 POST /extract 实时抽取
    python -m moray.api.server --port 8000

前置：docker start moray-pgvector
"""

from __future__ import annotations

import argparse

from ..jobs import JobStore
from ..kb import EntityStore, Ingestor, KnowledgeBase, init_pool
from ..observability import Dashboard
from .app import AppDeps, create_app


async def build_deps(*, enable_llm: bool = False,
                     with_dashboard: bool = True,
                     with_jobs: bool = True) -> AppDeps:
    pool = await init_pool()
    store = EntityStore(pool)
    await store.init_schema()
    kb = KnowledgeBase(store)
    ingestor = Ingestor(store)  # L14 默认不挂消歧器，保持接口简单

    job_store = JobStore(pool) if with_jobs else None
    if job_store is not None:
        await job_store.init_schema()

    dashboard = Dashboard(pool) if with_dashboard else None
    if dashboard is not None:
        await dashboard.init_schema()

    extractor_fn = None
    if enable_llm:
        from ..extraction import Extractor

        service = Extractor("general", max_retries=1)

        async def _extract(text: str, feedback: str | None):
            # 无状态抽取；返工轮的反馈通过首条 prompt 携带
            doc_text = text
            if feedback:
                doc_text = text + "\n\n【上次问题，请务必修正】\n" + feedback
            from ..extraction.document import Document as _Doc

            out = await service.extract(
                _Doc(id="api-live", text=doc_text, source="api"))
            return out.result

        extractor_fn = _extract

    return AppDeps(
        pool=pool, store=store, kb=kb, ingestor=ingestor,
        job_store=job_store, dashboard=dashboard,
        extractor_fn=extractor_fn)


def main(argv: list[str] | None = None) -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="Moray HTTP API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--llm", action="store_true", help="启用实时 LLM 抽取")
    args = parser.parse_args(argv)

    async def factory() -> AppDeps:
        return await build_deps(enable_llm=args.llm)

    # 依赖在 uvicorn 自己的事件循环里、经 lifespan 构建（连接池才绑定正确循环）
    app = create_app(deps_factory=factory)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
