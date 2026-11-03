"""文档与抽取产物的数据模型。

把"输入是什么、输出附带哪些元信息"显式建模，而不是在函数间裸传字符串：
  · Document   带稳定 id / 来源 / 领域，溯源（provenance）从这里开始；
  · ExtractionOutput  = 抽取结果 + 用的领域/模型 + token 用量 + 重试次数，
                       是落盘、入知识库、做评测的统一载体。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, computed_field

from ..schema import ExtractionResult
from .chunk_metrics import ChunkCallMetrics, DocumentMetrics


class Document(BaseModel):
    """一篇待抽取的文档。"""

    id: str = Field(description="文档稳定唯一 ID，用于实体出处溯源")
    text: str = Field(description="文档正文；字符偏移 span 都相对它计算")
    # 返工轮会把审校反馈追加到 text 末尾（让模型看到要改什么），但 mention/span
    # 与关系证据必须相对【原始正文】回填，否则反馈里的实体名/三元组会生成
    # 越界 span。source_text 为空时语义等同 text（普通路径无需传）。
    source_text: str | None = Field(
        default=None,
        description="回填/审计所用的原始正文；为空时等于 text",
    )
    title: str = ""
    source: str = Field(default="unknown", description="来源，如 news/contract/upload")
    domain_code: str = Field(default="general", description="抽取时使用的领域配置 code")

    @property
    def audit_text(self) -> str:
        """回填/审计坐标系：有 ``source_text`` 用它，否则回退到 ``text``。

        ``source_text`` 可能是空字符串（合法空正文），所以不能写成
        ``source_text or text``——那会把空正文错误替换成带返工反馈的 text。
        API/作业/抽取器/分块流水线共用这一处口径（r15 F8）。
        """
        return self.source_text if self.source_text is not None else self.text


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
    # 瘦身回填模式下的确定性质量信号（块级置信度/命中/歧义/幻觉别名/关系证据）。
    # 关闭模式恒为 None；它是旁路可观测信息，不属于稳定内核 Entity/Relation。
    backfill: dict | None = Field(
        default=None,
        description="本地确定性回填信号（仅 MORAY_LOCAL_BACKFILL 打开时有值）",
    )
    # 分块级度量（工单 01）：对单次抽取而言是"这一块"的调用记录；
    # 整篇/分块流水线在更上层聚合成 DocumentMetrics。默认 None 以兼容
    # 不经 Extractor 构造输出的离线假路径；真实路径恒有值。
    call_metrics: ChunkCallMetrics | None = Field(
        default=None,
        description="本次（=单块）模型调用的分块级度量：结束原因/分段耗时/截断信号")
    document_metrics: DocumentMetrics | None = Field(
        default=None,
        description="分块流水线文档级度量：每块一条记录 + 聚合摘要（工单 01）")

    def model_post_init(self, __context: Any) -> None:
        # pydantic v2 不会把 dataclass 当嵌套模型校验，显式转换保持类型可信
        if self.call_metrics is not None and not isinstance(
                self.call_metrics, ChunkCallMetrics):
            object.__setattr__(
                self, "call_metrics",
                ChunkCallMetrics(**dict(self.call_metrics)))
        if self.document_metrics is not None and not isinstance(
                self.document_metrics, DocumentMetrics):
            data = dict(self.document_metrics)
            data["chunks"] = [
                c if isinstance(c, ChunkCallMetrics)
                else ChunkCallMetrics(**dict(c))
                for c in data.get("chunks", [])]
            object.__setattr__(
                self, "document_metrics", DocumentMetrics(**data))
