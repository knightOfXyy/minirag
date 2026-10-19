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
    extractor_fn,
    source: str = "batch",
):
    """构造一个符合 BatchRunner 要求的 async (JobRecord)->dict handler。

    extractor_fn: async (text, feedback) -> ExtractionResult（可注入离线实现）。
    """

    async def handle(job: JobRecord) -> dict:
        path = Path(job["document_ref"])
        text = path.read_text(encoding="utf-8").strip()

        doc = Document(
            id=job["document_id"],
            text=text,
            source=source,
            domain_code=job["domain_code"],
        )

        # ① L10 抽取-审校闭环
        reviewed = await run_extraction_team(
            text, extractor=extractor_fn, max_iters=3)

        # ② 包成 ExtractionOutput 交给 L08/L09 入库（幂等）
        output = ExtractionOutput(
            document_id=doc.id,
            domain_code=doc.domain_code,
            result=reviewed.result,
            model_name="batch-pipeline",
            attempts=reviewed.review.iterations,
        )
        report = await ingestor.ingest(output)

        return {
            "accepted_by_review": reviewed.accepted,
            "review_iters": reviewed.review.iterations,
            "entities_written": report.entities_written,
            "merged": report.merged,
            "queued": report.queued,
            "created": report.created,
            "relations_written": report.relations_written,
            "open_issues": reviewed.review.issues if not reviewed.accepted else [],
        }

    return handle
