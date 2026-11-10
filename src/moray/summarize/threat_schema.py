"""敌情线索抽取的结构化输出模型（统一导入入口，服务端 AI 分流）。

与 summarize 的四块 SummarizeResult 并存：本模型专门面向「一条文档 → 多条敌情线索」，
每条线索含 title(线索描述)/reason(判定依据)/level(高|中|低)。
字段名以 camelCase 对外（alias 生成），经 talent-api 透传前端。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class ThreatClue(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    title: str
    reason: str = ""
    level: str = ""


class ThreatClueList(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    # 至少给出空列表，由上层决定空结果如何兜底
    clues: list[ThreatClue] = Field(default_factory=list)
