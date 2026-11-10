"""通用文档总结（摘要/要点/人物/风险线索，ADR 0004）。"""

from .schemas import PersonMention, RiskClue, SummarizeResult
from .service import summarize_document

__all__ = [
    "PersonMention",
    "RiskClue",
    "SummarizeResult",
    "summarize_document",
]
