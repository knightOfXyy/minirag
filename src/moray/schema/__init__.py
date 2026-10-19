"""强类型数据模型（Pydantic）——整个知识库的"契约层"。"""

from .domains import (
    DOMAIN_REGISTRY,
    FINANCE_DOMAIN,
    GENERAL_DOMAIN,
    AttrSpec,
    DomainSpec,
    TypeSpec,
    domain_from_dict,
    domain_to_dict,
    get_domain,
)
from .entities import (
    Entity,
    EntityMention,
    ExtractionResult,
    Relation,
    Span,
)
from .factory import (
    build_extraction_model,
    build_system_prompt,
    to_core_result,
    validate_against_domain,
)

__all__ = [
    # 稳定内核
    "Entity",
    "EntityMention",
    "ExtractionResult",
    "Relation",
    "Span",
    # 领域配置
    "DomainSpec",
    "TypeSpec",
    "DOMAIN_REGISTRY",
    "GENERAL_DOMAIN",
    "FINANCE_DOMAIN",
    "get_domain",
    # 动态工厂
    "build_extraction_model",
    "build_system_prompt",
    "to_core_result",
    "validate_against_domain",
]


def ENTITY_TYPE_GUIDE():
    return None