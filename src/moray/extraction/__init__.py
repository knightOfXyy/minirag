"""抽取流水线：文档 -> 结构化实体/关系。"""

from .chunking import Chunk, Chunker, estimate_tokens, split_sentences
from .cache import (
    CacheBackend,
    ContentResultCache,
    InMemoryCacheBackend,
    PostgresCacheBackend,
    build_cache_key,
)
from .document import Document, ExtractionOutput
from .extractor import ExtractionError, Extractor
from .local_backfill import (
    AmbiguousMention,
    BackfillReport,
    backfill_provenance,
)
from .chunk_metrics import (
    FINISH_CACHE_HIT,
    FINISH_LENGTH,
    FINISH_UNKNOWN,
    ChunkCallMetrics,
    DocumentMetrics,
    is_length_finish_reason,
)
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
    "FINISH_CACHE_HIT",
    "FINISH_LENGTH",
    "FINISH_UNKNOWN",
    "is_length_finish_reason",
    "naive_merge",
    "remap_spans",
    "ContentResultCache",
    "InMemoryCacheBackend",
    "PostgresCacheBackend",
    "build_cache_key",
    "verify_spans",
    "backfill_provenance",
    "BackfillReport",
    "AmbiguousMention",
    "RuleBasedExtractor",
    "extract_from_tagged_text",
    "clean_tagged_text",
    "JsonFileSink",
]
