"""实体知识库存储层（PostgreSQL + pgvector）。"""

from .store import EntityStore, SimilarEntity, init_pool, close_pool

__all__ = ["EntityStore", "SimilarEntity", "init_pool", "close_pool"]
