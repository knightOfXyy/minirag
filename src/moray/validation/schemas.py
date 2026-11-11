"""校验模块的 HTTP 契约模型（camelCase 对外，与 moray 既有 schema 风格一致）。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class _Camel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class EntityIn(_Camel):
    """待校验的单个实体（对齐 moray.schema.entities.Entity 的最小必要字段）。"""

    name: str
    type: str
    aliases: list[str] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    attributes: dict = Field(default_factory=dict)
    description: str = ""


class RelationIn(_Camel):
    """待校验的单条关系（head/predicate/tail 用实体标准名引用）。"""

    head: str
    predicate: str
    tail: str
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class ValidationRequest(_Camel):
    """POST /validate 请求体：一次抽取产物（实体 + 关系）。"""

    entities: list[EntityIn] = Field(default_factory=list)
    relations: list[RelationIn] = Field(default_factory=list)
    # 领域代码，决定加载哪套 SHACL 形状；默认通用领域
    domain_code: str = "general"


class Violation(_Camel):
    """一条可定位到字段/实体的 SHACL 违约（供前端字段级高亮）。"""

    focus_node: str = Field(description="违约主体（实体 IRI 或名称）")
    path: str = Field(default="", description="违约属性路径，如 schema:name")
    constraint: str = Field(description="SHACL 约束组件，如 MinCountConstraintComponent")
    message: str = Field(default="", description="人类可读的中文/英文说明")
    value: str | None = Field(default=None, description="触发违约的实际值")


class ValidationResponse(_Camel):
    """校验结果。conforms=true 时 violations 为空。"""

    conforms: bool
    violations: list[Violation] = Field(default_factory=list)
    entity_count: int = 0
    relation_count: int = 0
    # 校验器自身故障时为 true（已降级为仅 Pydantic 结构校验，未跑 SHACL）
    degraded: bool = False
