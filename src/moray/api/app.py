"""
FastAPI 服务化（L14）
=====================

把前 13 课的能力包成一组 HTTP 接口，外部系统/前端即可使用：

  抽取      POST /extract                直接提交文本 -> 抽取/审校/（可选）入库
  作业      POST /jobs                   登记文档作业（配合 BatchRunner 跑）
            GET  /jobs、/jobs/{id}        作业进度/状态（L11 断点续跑）
  查询      GET  /entities/locate        自然语言/简称定位实体（L08/L12）
            GET  /entities/{id}/card     实体知识卡片（关系图）
            GET  /relations              结构化出/入边查询
            GET  /entities/similar       向量近邻
  人工审核  GET  /reviews                 待裁决合并（L09 HITL）
            POST /reviews/{id}/decision  批准/拒绝 -> 合并或保留
  看板      GET  /metrics/dashboard       L13 聚合质量指标
  健康      GET  /healthz

设计：
  · create_app(deps=...) 用依赖注入接收 pool/store/extractor，测试可注入
    假抽取器并用 httpx ASGITransport 不起端口调用；
  · 长耗时 LLM 抽取不阻塞事件循环：放 anyio.to_thread / asyncio（抽取本身是 async）；
  · 应用不负责启动 BatchRunner worker（那是独立进程），只登记/查询作业。
"""

from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Awaitable, Callable

from fastapi import Depends, FastAPI, HTTPException, Query

from ..extraction.document import Document, ExtractionOutput, Usage
from ..kb import EntityStore, Ingestor, KnowledgeBase
from ..orchestration import run_extraction_team
from .schemas import (
    EnqueueJobRequest,
    ExtractRequest,
    ExtractResponse,
    ReviewDecisionRequest,
)

# extractor_fn: async (text, feedback) -> ExtractionResult
ExtractorFn = Callable[[str, str | None], Awaitable]


@dataclass
class AppDeps:
    pool: object
    store: EntityStore
    kb: KnowledgeBase
    ingestor: Ingestor
    job_store: object | None = None
    dashboard: object | None = None
    extractor_fn: ExtractorFn | None = None  # 固定抽取器（测试/离线）
    # 动态抽取器提供方：按请求 domain_code 从 DB 配置中心取最新领域包；
    # 存在时优先于 extractor_fn（运营改 domain_configs 表即热更，无需重启）。
    extractor_provider: object | None = None


def create_app(
    deps: AppDeps | None = None,
    *,
    deps_factory: Callable[[], Awaitable[AppDeps]] | None = None,
) -> FastAPI:
    """创建应用。两种装配方式：

    · deps=         传入已构建好的依赖（测试/同事件循环内使用）；
    · deps_factory= 异步工厂，在 FastAPI lifespan（uvicorn 自己的事件循环）
      里构建依赖——生产必须走这条，因为 asyncpg 连接池绑定创建它的事件循环，
      不能在 asyncio.run() 里建好再拿到 uvicorn 循环复用。
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if deps_factory is not None:
            app.state.deps = await deps_factory()
            try:
                yield
            finally:
                await close_pool_safe(app.state.deps.pool)
        else:
            app.state.deps = deps
            yield

    app = FastAPI(
        title="Moray 实体抽取知识库", version="0.14.0", lifespan=lifespan)
    # 直接注入（测试/同循环）时立即挂上；工厂模式则等 lifespan 设置
    if deps is not None:
        app.state.deps = deps

    async def close_pool_safe(pool) -> None:
        from ..kb import close_pool

        try:
            await close_pool(pool)
        except Exception:  # noqa: BLE001
            pass

    async def get_deps() -> AppDeps:
        d = getattr(app.state, "deps", None)
        if d is None:  # pragma: no cover
            raise HTTPException(503, "应用依赖未初始化")
        return d

    # ------------------------------------------------------------------ 健康
    @app.get("/healthz")
    async def healthz(d: AppDeps = Depends(get_deps)) -> dict:
        try:
            n = await d.store.count()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(503, f"知识库不可用: {exc}") from exc

        # 当前连的是哪家模型（打码）。查询型 api 容器按设计可以不配 LLM 密钥，
        # 所以这里读不到也只报告 llm=null，不能让 /healthz 因此挂掉。
        try:
            from ..config import describe
            llm = describe()
        except Exception as exc:  # noqa: BLE001
            llm = {"provider": None, "error": str(exc)}
        return {"ok": True, "entities": n, "llm": llm}

    # ------------------------------------------------- 领域配置（配置中心）
    @app.get("/domains")
    async def list_domains(d: AppDeps = Depends(get_deps)):
        """列出可热更的领域包（DB 配置中心 + 内置）。"""
        if d.extractor_provider is not None:
            resolver = d.extractor_provider.resolver
            db_codes = await resolver.list_codes()
            return {"domains": db_codes, "source": "postgres"}
        raise HTTPException(501, "未配置领域配置中心（extractor_provider 为 None）")

    # ------------------------------------------------------------------ 抽取
    @app.post("/extract", response_model=ExtractResponse)
    async def extract(req: ExtractRequest, d: AppDeps = Depends(get_deps)):
        # 优先动态提供方：按请求 domain_code 从 DB 配置中心取最新领域包
        usage = Usage()          # token/缓存命中累加器（见 provider.usage_sink）
        if d.extractor_provider is not None:
            try:
                extractor_fn = await d.extractor_provider.extract(
                    req.domain_code, usage_sink=usage)
            except KeyError as exc:
                raise HTTPException(400, str(exc)) from exc
        elif d.extractor_fn is not None:
            extractor_fn = d.extractor_fn  # 固定抽取器（离线/测试）
            usage = None                   # 契约不回传用量，如实标 None
        else:
            raise HTTPException(501, "未配置抽取器（extractor_fn/provider 为 None）")

        doc_id = req.document_id or (
            "doc-" + hashlib.sha1(req.text.encode()).hexdigest()[:10])

        # L10 抽取-审校闭环（抽取器内部已按领域 schema 约束实体类型与属性）
        reviewed = await run_extraction_team(
            req.text, extractor=extractor_fn, max_iters=3)

        ingest_info = None
        if req.ingest:
            output = ExtractionOutput(
                document_id=doc_id, domain_code=req.domain_code,
                result=reviewed.result, model_name="api",
                usage=(usage or Usage()),
                attempts=reviewed.review.iterations)
            rep = await d.ingestor.ingest(output)
            ingest_info = {
                "entities_written": rep.entities_written,
                "relations_written": rep.relations_written,
                "merged": rep.merged, "queued": rep.queued,
                "created": rep.created,
            }

        return ExtractResponse(
            document_id=doc_id,
            accepted_by_review=reviewed.accepted,
            review_iters=reviewed.review.iterations,
            entities=[e.model_dump() for e in reviewed.result.entities],
            relations=[r.model_dump() for r in reviewed.result.relations],
            ingest=ingest_info,
            usage=(usage.model_dump() if usage is not None else None),
        )

    # ------------------------------------------------------------------ 作业
    @app.post("/jobs")
    async def enqueue_job(req: EnqueueJobRequest, d: AppDeps = Depends(get_deps)):
        if d.job_store is None:
            raise HTTPException(501, "未配置作业库")
        doc_id = req.document_id or (
            "doc-" + hashlib.sha1(req.document_ref.encode()).hexdigest()[:10])
        job_id = "job-" + hashlib.sha1(req.document_ref.encode()).hexdigest()[:10]
        rec, created = await d.job_store.create_job(
            job_id=job_id, document_id=doc_id,
            document_ref=req.document_ref, domain_code=req.domain_code)
        return {"job_id": rec.job_id, "created": created, "status": rec.status}

    @app.get("/jobs")
    async def list_jobs(status: str | None = None, d: AppDeps = Depends(get_deps)):
        if d.job_store is None:
            raise HTTPException(501, "未配置作业库")
        jobs = await d.job_store.list_jobs(status)
        return {"jobs": [j.as_dict() for j in jobs]}

    @app.get("/jobs/{job_id}")
    async def get_job(job_id: str, d: AppDeps = Depends(get_deps)):
        if d.job_store is None:
            raise HTTPException(501, "未配置作业库")
        jobs = {j.job_id: j for j in await d.job_store.list_jobs()}
        if job_id not in jobs:
            raise HTTPException(404, "作业不存在")
        return jobs[job_id].as_dict()

    # ------------------------------------------------------------------ 查询
    @app.get("/entities/locate")
    async def locate(query: str = Query(...),
                     domain_code: str = "general",
                     k: int = 5, d: AppDeps = Depends(get_deps)):
        cands = await d.kb.locate(query, domain_code=domain_code, k=k)
        return {"candidates": [
            {"id": c.id, "name": c.name, "type": c.type,
             "similarity": round(c.similarity, 4), "aliases": c.aliases}
            for c in cands]}

    @app.get("/entities/similar")
    async def similar(query: str = Query(...), entity_type: str | None = None,
                      k: int = 5, min_similarity: float = 0.5,
                      d: AppDeps = Depends(get_deps)):
        vec = d.kb.embedder.embed_one(query)
        hits = await d.store.similar_entities(
            vec, type=entity_type, k=k, max_distance=1 - min_similarity)
        return {"entities": [
            {"id": h.id, "name": h.name, "type": h.type,
             "similarity": round(h.similarity, 4), "aliases": h.aliases}
            for h in hits]}

    @app.get("/entities/{entity_id}/card")
    async def card(entity_id: int, d: AppDeps = Depends(get_deps)):
        c = await d.kb.card(entity_id)
        if c is None:
            raise HTTPException(404, "实体不存在")
        return {
            "id": c.id, "name": c.name, "type": c.type,
            "aliases": c.aliases, "attributes": c.attributes,
            "confidence": c.confidence,
            "outgoing": c.outgoing, "incoming": c.incoming}

    @app.get("/relations")
    async def relations(name: str = Query(...), direction: str = "both",
                        predicate: str | None = None,
                        domain_code: str = "general",
                        d: AppDeps = Depends(get_deps)):
        eid = await d.store.get_entity_id(name, domain_code=domain_code)
        if eid is None:
            cands = await d.kb.locate(name, domain_code=domain_code, k=1)
            eid = cands[0].id if cands else None
        if eid is None:
            return {"relations": []}
        rels = await d.store.relations_of(
            eid, direction=direction, predicate=predicate)
        return {"relations": [{
            "source": r["src_name"], "source_type": r["src_type"],
            "predicate": r["predicate"],
            "target": r["dst_name"], "target_type": r["dst_type"],
            "confidence": round(float(r["confidence"]), 3)} for r in rels]}

    # ------------------------------------------------------------------ HITL
    @app.get("/reviews")
    async def list_reviews(status: str = "pending",
                           limit: int = 50, d: AppDeps = Depends(get_deps)):
        if status == "pending":
            rows = await d.store.pending_reviews(limit=limit)
        else:
            async with d.pool.acquire() as conn:
                rows = [dict(x) for x in await conn.fetch(
                    "SELECT * FROM merge_reviews WHERE status=$1 "
                    "ORDER BY id DESC LIMIT $2", status, limit)]
        return {"reviews": rows}

    @app.post("/reviews/{review_id}/decision")
    async def decide_review(review_id: int, req: ReviewDecisionRequest,
                            d: AppDeps = Depends(get_deps)):
        ok = await d.store.decide_review(review_id, req.approved)
        if not ok:
            raise HTTPException(404, "没有处于 pending 的该审核记录")
        return {"review_id": review_id,
                "status": "approved" if req.approved else "rejected"}

    # ------------------------------------------------------------------ 看板
    @app.get("/metrics/dashboard")
    async def dashboard(domain_code: str | None = None,
                        d: AppDeps = Depends(get_deps)):
        if d.dashboard is None:
            raise HTTPException(501, "未配置看板")
        return await d.dashboard.summary(domain_code=domain_code)

    return app
