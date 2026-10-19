"""批量文档任务与可恢复状态（L11）。"""

from .handlers import build_document_handler
from .jobstore import JobRecord, JobStatus, JobStore
from .runner import BatchRunner, BatchRunSummary

__all__ = [
    "JobRecord",
    "JobStatus",
    "JobStore",
    "BatchRunner",
    "BatchRunSummary",
    "build_document_handler",
]
