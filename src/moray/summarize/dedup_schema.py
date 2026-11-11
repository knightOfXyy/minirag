"""敌情线索语义消歧的结构化模型（规则向量 + LLM 双保险）。

LLM 只对规则判定的"暧昧区"做 pairwise 裁决，输出受本 schema 约束。
字段 camelCase 对外，经 talent-api 透传前端。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

# 三档：duplicate=规则/LLM 判定重复；suspect=疑似（LLM 不自信，交人工）；fresh=全新
DedupVerdict = Literal["duplicate", "suspect", "fresh"]


class DedupPair(BaseModel):
    """待裁决的一对线索（新线索 vs 最相似的存量线索）。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    new_title: str
    new_reason: str = ""
    existing_title: str
    existing_reason: str = ""
    similarity: float = 0.0


class DedupJudgeResult(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    same: bool = Field(description="两条线索是否指向同一事实/同一敌情")
    confident: bool = Field(description="模型是否有把握；没把握给 false")
    reason: str = Field(default="", description="一句中文裁决理由")
