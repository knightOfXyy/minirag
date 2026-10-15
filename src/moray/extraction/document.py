"""文档与抽取产物的数据模型。

把"输入是什么、输出附带哪些元信息"显式建模，而不是在函数间裸传字符串：
  · Document   带稳定 id / 来源 / 领域，溯源（provenance）从这里开始；
  · ExtractionOutput  = 抽取结果 + 用的领域/模型 + token 用量 + 重试次数，
                       是落盘、入知识库、做评测的统一载体。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..schema import ExtractionResult


class Document(BaseModel):
    """一篇待抽取的文档。"""

    id: str = Field(description="文档稳定唯一 ID，用于实体出处溯源")
    text: str = Field(description="文档正文；字符偏移 span 都相对它计算")
    title: str = ""
    source: str = Field(default="unknown", description="来源，如 news/contract/upload")
    domain_code: str = Field(default="general", description="抽取时使用的领域配置 code")


class Usage(BaseModel):
    """一次或多次模型调用累计的 token 用量（成本核算的原始数据）。"""

    input_tokens: int = 0
    output_tokens: int = 0

    def add(self, other: "Usage | None") -> None:
        if other is None:
            return
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens


class ExtractionOutput(BaseModel):
    """一次文档抽取的完整产物。"""

    document_id: str
    domain_code: str
    result: ExtractionResult
    model_name: str = ""
    usage: Usage = Field(default_factory=Usage)
    attempts: int = Field(default=1, description="实际模型尝试次数（含校验重试）")
