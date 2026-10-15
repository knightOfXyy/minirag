"""强类型数据模型（Pydantic）——整个知识库的"契约层"。

抽取 Agent 产出这些对象，存储层消费这些对象，校验逻辑也围绕它们展开。
"""

from .entities import (
    ENTITY_TYPE_GUIDE,
    Entity,
    EntityMention,
    EntityType,
    ExtractionResult,
    Relation,
    Span,
)

__all__ = [
    "ENTITY_TYPE_GUIDE",
    "Entity",
    "EntityMention",
    "EntityType",
    "ExtractionResult",
    "Relation",
    "Span",
]
