"""抽取流水线：文档 -> 结构化实体/关系。"""

from .chunking import Chunk, Chunker, estimate_tokens, split_sentences
from .cache import (
    CacheBackend,
    ClaimableBackend,
    ContentResultCache,
    CorruptibleBackend,
    InMemoryCacheBackend,
    PostgresCacheBackend,
    build_cache_key,
)
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
    "naive_merge",
    "remap_spans",
    "CacheBackend",
    "ClaimableBackend",
    "CorruptibleBackend",
    "ContentResultCache",
    "InMemoryCacheBackend",
    "PostgresCacheBackend",
    "build_cache_key",
    "verify_spans",
    "RuleBasedExtractor",
    "extract_from_tagged_text",
    "clean_tagged_text",
    "JsonFileSink",
]
