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

from pathlib import Path

from ..extraction.document import Document, ExtractionOutput
from ..kb import EntityStore, Ingestor
from ..orchestration import run_extraction_team
from .jobstore import JobRecord


async def build_document_handler(
    *,
    store: EntityStore,
    ingestor: Ingestor,
    extractor_fn=None,
    extractor_provider=None,
    source: str = "batch",
    dashboard=None,
    model_name: str = "batch-pipeline",
):
    """构造一个符合 BatchRunner 要求的 async (JobRecord)->dict handler。

    extractor_fn: 固定的 async (text, feedback)->ExtractionResult（离线/兼容）。
    extractor_provider: DynamicExtractorProvider；给定时每个作业按其
        domain_code 从 DB 配置中心取最新领域包（支持热更属性），优先于
        extractor_fn。
    dashboard: 可选 observability.Dashboard；每处理一篇写一行 pipeline_runs。
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
        if extractor_provider is not None:
            active_extractor = await extractor_provider.extract(domain_code)
        else:
            active_extractor = extractor_fn

        # ① L10 抽取-审校闭环
        reviewed = await run_extraction_team(
            text, extractor=active_extractor, max_iters=3)

        # ② 包成 ExtractionOutput 交给 L08/L09 入库（幂等）
        output = ExtractionOutput(
            document_id=doc.id,
            domain_code=doc.domain_code,
            result=reviewed.result,
            model_name=model_name,
            attempts=reviewed.review.iterations,
        )
        report = await ingestor.ingest(output)
        latency_ms = int((time.perf_counter() - t0) * 1000)

        # ③ L13 质量看板（可选）
        if dashboard is not None:
            from ..observability import RunRecorder

            await dashboard.record(RunRecorder.from_pipeline(
                document_id=doc.id, reviewed=reviewed, ingest_report=report,
                domain_code=doc.domain_code, model_name=model_name,
                latency_ms=latency_ms))

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
        }

    return handle
