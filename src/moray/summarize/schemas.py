"""文档总结的结构化输出模型（接口文档 v1.1 §3.14）。

字段名以 camelCase 对外（alias 生成），与 talent-api 转发契约一致。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class PersonMention(BaseModel):
    name: str
    identity: str
    role: str


class RiskClue(BaseModel):
    clue: str
    reason: str
    level: str


class SummarizeResult(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    summary: str
    key_points: list[str]
    persons: list[PersonMention]
    risk_clues: list[RiskClue]
