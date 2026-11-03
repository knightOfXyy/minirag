"""
文档任务的处理函数（L11）
========================

BatchRunner 只负责"认领任务、跑 handler、记状态"；具体怎么处理一篇文档在这里，
把前面所有课串成一条可批量、可恢复的产线：

    读文档
      -> L10 抽取团队（抽取 + 审校返工）
      -> L08/L09 入库（Ingestor，带消歧；天然幂等）
      -> 返回统计

因为 Ingestor 幂等，handler 即便因崩溃被重跑，也不会产生重复实体/关系——
这正是 BatchRunner 敢于把 running 孤儿任务收回重试的原因。

为支持离线教学，extractor/reviewer 都可注入：
  · 线上：真实 Extractor（LLM）+ 规则审校；
  · 离线：传入确定性假抽取，零花费演示批量与续跑。
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..extraction import ExtractionError
from ..extraction.document import Document, ExtractionOutput, Usage
from ..kb import EntityStore, Ingestor
from ..orchestration import run_chunked_team
from .jobstore import JobRecord

logger = logging.getLogger(__name__)


async def build_document_handler(
    *,
    store: EntityStore,
    ingestor: Ingestor,
    extractor_fn=None,
    extractor_provider=None,
    source: str = "batch",
    dashboard=None,
    model_name: str = "batch-pipeline",
    result_cache=None,
):
    """构造一个符合 BatchRunner 要求的 async (JobRecord)->dict handler。

    extractor_fn: 固定的 async (text, feedback)->ExtractionResult（离线/兼容）。
    extractor_provider: DynamicExtractorProvider；给定时每个作业按其
        domain_code 从 DB 配置中心取最新领域包（支持热更属性），优先于
        extractor_fn。
    dashboard: 可选 observability.Dashboard；每处理一篇写一行 pipeline_runs。
    result_cache: 可选分块结果内容级缓存。应在 worker 进程内复用同一个实例，
        断点续跑/失败重试/重复提交时相同分块命中缓存、不再调 LLM；命中用量只
        记 cache_hits，不产生真实 token。None（默认）= 完全不缓存。
    """
    import time

    async def handle(job: JobRecord) -> dict:
        t0 = time.perf_counter()
        path = Path(job["document_ref"])
        text = path.read_text(encoding="utf-8").strip()
        domain_code = job["domain_code"]

        doc = Document(
            id=job["document_id"],
            text=text,
            source=source,
            domain_code=domain_code,
        )

        # 该作业用哪个抽取器：优先按领域动态取（配置中心热更）
        # usage 收集器：抽取函数契约不返回用量，这里挂个累加器把 token
        # （含提示缓存命中量）捞回来，写进看板与作业结果。
        usage = Usage()
        backfill_sink: dict = {}
        if extractor_provider is not None:
            active_extractor = await extractor_provider.extract(
                domain_code, usage_sink=usage, backfill_sink=backfill_sink)
        else:
            active_extractor = extractor_fn

        # ① 抽取-审校闭环。波次 4：批处理长文同样分块 map→归并→单轮审校；
        # 无 provider（离线固定假抽取）或短文本时委托旧闭环，逐字节兼容。
        from ..config import chunk_settings as _chunk_settings

        _cs = _chunk_settings()
        try:
            reviewed = await run_chunked_team(
                text, extractor=active_extractor,
                provider=extractor_provider, domain_code=domain_code,
                max_iters=3, usage=usage, document_id=doc.id,
                metrics_sink=[],
                chunk_threshold_units=_cs["threshold_units"],
                chunk_max_units=_cs["max_units"],
                chunk_overlap_units=_cs["overlap_units"],
                chunk_concurrency=_cs["concurrency"],
                result_cache=result_cache)
        except ExtractionError as exc:
            # 批量路径同样不能把失败块的截断/返工信号丢掉（F5）：
            # 先落一行失败观测再原样抛给 BatchRunner 做重试/终态裁决。
            if dashboard is not None:
                from ..observability import RunRecorder

                try:
                    await dashboard.record(RunRecorder.from_failure(
                        document_id=doc.id, domain_code=doc.domain_code,
                        document_metrics=getattr(exc, "document_metrics", None),
                        error=str(exc), source="job", model_name=model_name,
                        # 短文档直接抛错时没有分块度量，用 usage 兜底成本
                        input_tokens=usage.input_tokens,
                        output_tokens=usage.output_tokens,
                        cached_input_tokens=usage.cached_input_tokens,
                        latency_ms=int((time.perf_counter() - t0) * 1000)))
                except Exception:  # noqa: BLE001 观测失败不能掩盖原始抽取错误
                    logger.warning(
                        "失败作业写看板失败 document_id=%s", doc.id,
                        exc_info=True)
            raise

        # ② 包成 ExtractionOutput 交给 L08/L09 入库（幂等）
        # 分块路径的回填信号在 reviewed.backfill；整篇路径（provider 闭包）
        # 通过 backfill_sink 回收。两条路都不能在作业主链丢掉。
        backfill = (reviewed.backfill if reviewed.backfill is not None
                    else backfill_sink.get("backfill"))
        output = ExtractionOutput(
            document_id=doc.id,
            domain_code=doc.domain_code,
            result=reviewed.result,
            model_name=model_name,
            usage=usage,
            attempts=reviewed.review.iterations,
            backfill=backfill,
        )
        report = await ingestor.ingest(output)
        latency_ms = int((time.perf_counter() - t0) * 1000)

        # ③ L13 质量看板（可选）
        if dashboard is not None:
            from ..observability import RunRecorder

            await dashboard.record(RunRecorder.from_pipeline(
                document_id=doc.id, reviewed=reviewed, ingest_report=report,
                domain_code=doc.domain_code, source="job",
                model_name=model_name,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cached_input_tokens=usage.cached_input_tokens,
                cache_hits=usage.cache_hits,
                latency_ms=latency_ms))

        # 工单 01：分块度量与 API 路径消费同一份 DocumentMetrics 序列化结果
        doc_metrics = getattr(reviewed, "document_metrics", None)
        return {
            "accepted_by_review": reviewed.accepted,
            "review_iters": reviewed.review.iterations,
            "entities_written": report.entities_written,
            "merged": report.merged,
            "queued": report.queued,
            "created": report.created,
            "relations_written": report.relations_written,
            "open_issues": reviewed.review.issues if not reviewed.accepted else [],
            "latency_ms": latency_ms,
            # 成本/缓存可观测：cached_input_tokens 与 cache_hit_rate 直接进作业结果
            "usage": usage.model_dump(),
            # 分块级度量（整篇旧闭环路径为 None）；截断块数在 summary 里一目了然
            "chunk_metrics": doc_metrics.to_dict() if doc_metrics is not None else None,
            # 瘦身回填确定性信号（开关关闭时为 None）；新增字段，与度量并列
            "backfill": output.backfill,
        }

    return handle
