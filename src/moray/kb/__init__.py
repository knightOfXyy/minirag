"""实体知识库存储层（PostgreSQL + pgvector）。"""

from .ingestor import IngestReport, Ingestor
from .query import EntityCard, KnowledgeBase
from .store import EntityStore, SimilarEntity, close_pool, init_pool

__all__ = [
    "EntityStore",
    "SimilarEntity",
    "init_pool",
    "close_pool",
    "Ingestor",
    "IngestReport",
    "KnowledgeBase",
    "EntityCard",
]
