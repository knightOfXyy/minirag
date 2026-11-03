"""抽取流水线：文档 -> 结构化实体/关系。"""

from .chunk_metrics import (
    FINISH_LENGTH,
    FINISH_UNKNOWN,
    ChunkCallMetrics,
    DocumentMetrics,
    is_length_finish_reason,
    merge_call_metrics,
)
from .chunking import Chunk, Chunker, estimate_tokens, split_sentences
from .document import Document, ExtractionOutput
from .extractor import ExtractionError, Extractor
from .pipeline import ChunkedExtractionPipeline, naive_merge, remap_spans
from .quality import verify_spans
from .rule_extractor import (
    RuleBasedExtractor,
    clean_tagged_text,
    extract_from_tagged_text,
)
from .sink import JsonFileSink

__all__ = [
    "Document",
    "ExtractionOutput",
    "ExtractionError",
    "Extractor",
    "Chunk",
    "Chunker",
    "estimate_tokens",
    "split_sentences",
    "ChunkedExtractionPipeline",
    "ChunkCallMetrics",
    "DocumentMetrics",
    "FINISH_LENGTH",
    "FINISH_UNKNOWN",
    "is_length_finish_reason",
    "merge_call_metrics",
    "naive_merge",
    "remap_spans",
    "verify_spans",
    "RuleBasedExtractor",
    "extract_from_tagged_text",
    "clean_tagged_text",
    "JsonFileSink",
]
