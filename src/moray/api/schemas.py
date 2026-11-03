"""HTTP API 的请求/响应模型（L14）。

显式建模而不是裸 dict：FastAPI 会据此生成 OpenAPI 文档与入参校验，
返回结构也稳定，便于前端/外部系统对接。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ExtractRequest(BaseModel):
    text: str = Field(description="待抽取的文档正文")
    document_id: str | None = Field(default=None, description="文档稳定 ID；缺省自动生成")
    domain_code: str = Field(default="general")
    ingest: bool = Field(default=True, description="是否写入知识库")


class ExtractResponse(BaseModel):
    document_id: str
    accepted_by_review: bool
    review_iters: int
    entities: list[dict]
    relations: list[dict]
    ingest: dict | None = None
    # token 用量（含提示缓存命中量 cached_input_tokens 与命中率 cache_hit_rate）。
    # 走固定抽取器（测试/离线）时为 None——契约只回 ExtractionResult，用量捞不回来。
    usage: dict | None = None
    # 工单 01：分块级度量（分块路径才有；整篇/离线闭包路径为 None）。
    # 含文档级摘要 summary（chunk_calls/truncated_chunks/rework_rate/
    # prefill_s/decode_s）与每块一条的 chunks 明细。API 与批量作业消费同一份
    # DocumentMetrics.to_dict()，两条路径字段逐字一致。
    chunk_metrics: dict | None = None
    # 瘦身回填的确定性质量信号（块级置信度/命中/歧义/幻觉别名/关系证据）。
    # 开关关闭或走固定抽取器时为 None。新增字段：老客户端忽略即可。
    backfill: dict | None = None


class EnqueueJobRequest(BaseModel):
    document_ref: str = Field(description="文档位置（路径/来源标识）")
    document_id: str | None = None
    domain_code: str = "general"


class ReviewDecisionRequest(BaseModel):
    approved: bool
