"""文档与抽取产物的数据模型。

把"输入是什么、输出附带哪些元信息"显式建模，而不是在函数间裸传字符串：
  · Document   带稳定 id / 来源 / 领域，溯源（provenance）从这里开始；
  · ExtractionOutput  = 抽取结果 + 用的领域/模型 + token 用量 + 重试次数，
                       是落盘、入知识库、做评测的统一载体。
"""

from __future__ import annotations

from pydantic import BaseModel, Field, computed_field

from ..schema import ExtractionResult


class Document(BaseModel):
    """一篇待抽取的文档。"""

    id: str = Field(description="文档稳定唯一 ID，用于实体出处溯源")
    text: str = Field(description="文档正文；字符偏移 span 都相对它计算")
    title: str = ""
    source: str = Field(default="unknown", description="来源，如 news/contract/upload")
    domain_code: str = Field(default="general", description="抽取时使用的领域配置 code")


class CacheSavings(BaseModel):
    """命中【分块结果内容级缓存】时“本可花费”的用量估算。

    故意不叫 ``*_tokens`` 也不放在 Usage 顶层：任何对未来所有顶层 token 字段
    求和的新看板/评测都不会把它误算成真实成本（N2）。它只用于估算省了多少。
    """

    input_tokens: int = 0
    output_tokens: int = 0


class Usage(BaseModel):
    """一次或多次模型调用累计的 token 用量（成本核算的原始数据）。

    cached_input_tokens：其中命中【提示缓存】(prompt cache) 的输入 token 数。
    托管网关按 cached_tokens 上报（agentscope 已解析进 Msg.usage.cache_input_tokens）。
    命中率 = cached_input_tokens / input_tokens；缓存 token 单价通常远低于普通输入，
    不计这一段，成本看板会系统性高估。
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    # 命中【分块结果内容级缓存】的次数：这些分块没有发起任何模型调用，因此
    # 不产生 input/output token。与 cached_input_tokens（提示缓存命中的真实
    # 计费 token）严格区分：成本/看板统计只汇总前三个真实计量字段。
    cache_hits: int = 0
    # 命中时缓存里还原出来的“本可花费”的用量：嵌在独立命名空间里，绝不加进
    # input_tokens/output_tokens，也不与真实 token 字段同级（N2）。
    cache_savings: CacheSavings = Field(default_factory=CacheSavings)

    @computed_field
    @property
    def cache_hit_rate(self) -> float:
        """前缀缓存命中率（0~1）；无输入时记 0。

        做成 computed_field：任何把 usage 序列化成 JSON 的地方
        （作业结果、API 响应、评测报告）都自动带上这一列，不用各处手算。
        """
        return (self.cached_input_tokens / self.input_tokens
                if self.input_tokens else 0.0)

    def add(self, other: "Usage | None") -> None:
        if other is None:
            return
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cached_input_tokens += other.cached_input_tokens
        self.cache_hits += other.cache_hits
        self.cache_savings.input_tokens += other.cache_savings.input_tokens
        self.cache_savings.output_tokens += other.cache_savings.output_tokens


class ExtractionOutput(BaseModel):
    """一次文档抽取的完整产物。"""

    document_id: str
    domain_code: str
    result: ExtractionResult
    model_name: str = ""
    usage: Usage = Field(default_factory=Usage)
    attempts: int = Field(default=1, description="实际模型尝试次数（含校验重试）")
