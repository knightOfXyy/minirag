"""批量文档任务与可恢复状态（L11；多 worker 租约扩展）。"""

from .handlers import build_document_handler
from .jobstore import JobRecord, JobStatus, JobStore
from .runner import BatchRunner, BatchRunSummary, default_worker_id

__all__ = [
    "JobRecord",
    "JobStatus",
    "JobStore",
    "BatchRunner",
    "BatchRunSummary",
    "build_document_handler",
    "default_worker_id",
]
