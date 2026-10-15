"""抽取流水线：文档 -> 结构化实体/关系。"""

from .document import Document, ExtractionOutput
from .extractor import ExtractionError, Extractor
from .quality import verify_spans
from .sink import JsonFileSink

__all__ = [
    "Document",
    "ExtractionOutput",
    "ExtractionError",
    "Extractor",
    "verify_spans",
    "JsonFileSink",
]
