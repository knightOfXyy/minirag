"""
抽取指标数据模型与收集器（L06）
================================

中间件只负责"在正确的时机采集"，指标怎么存、怎么聚合由独立的
MetricsCollector 决定——单一职责，也方便以后替换成 Prometheus/OTel。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class ModelCallRecord:
    """一次模型调用的计量记录。"""

    messages_in: int = 0
    tools_offered: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    latency_s: float = 0.0
    ok: bool = True
    error: str = ""


@dataclass
class ToolCallRecord:
    """一次工具执行的审计记录（结构化输出本质也是一次工具调用）。"""

    name: str
    ok: bool
    latency_s: float
    # 结构化抽取结果里的实体/关系计数（仅结构化工具会填）
    n_entities: int | None = None
    n_relations: int | None = None


@dataclass
class ExtractionMetrics:
    """一次完整 reply（可能含多轮模型调用/工具）的汇总指标。"""

    agent_name: str = ""
    started_at: float = 0.0
    duration_s: float = 0.0
    model_calls: list[ModelCallRecord] = field(default_factory=list)
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    n_entities: int = 0
    n_relations: int = 0
    # 质量分（由 ExtractionMetricsMiddleware 依据结果填充）
    span_ratio: float | None = None
    low_confidence_entities: int = 0
    quality_score: float | None = None

    @property
    def input_tokens(self) -> int:
        return sum(c.input_tokens for c in self.model_calls)

    @property
    def output_tokens(self) -> int:
        return sum(c.output_tokens for c in self.model_calls)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def summary(self) -> str:
        q = f"{self.quality_score:.2f}" if self.quality_score is not None else "N/A"
        return (
            f"[{self.agent_name}] 耗时{self.duration_s:.1f}s "
            f"模型调用{len(self.model_calls)}次 "
            f"token={self.total_tokens}(in{self.input_tokens}/out{self.output_tokens}) "
            f"实体{self.n_entities} 关系{self.n_relations} "
            f"低置信{self.low_confidence_entities} 质量分={q}"
        )


class MetricsCollector:
    """收集并保存多次抽取的指标（教学里打印/落 JSON；生产可推监控系统）。

    多个中间件通过 start()/current()/finish() 共享【同一条】记录：
    最外层中间件在一次 reply 开始时 start，其余中间件只是 current() 追加。
    """

    def __init__(self) -> None:
        self.records: list[ExtractionMetrics] = []
        self._active: ExtractionMetrics | None = None

    def start(self, agent_name: str) -> ExtractionMetrics:
        rec = ExtractionMetrics(agent_name=agent_name, started_at=time.time())
        self.records.append(rec)
        self._active = rec
        return rec

    def current(self, agent_name: str = "agent") -> ExtractionMetrics:
        if self._active is None:
            # 没有最外层中间件时也能独立工作
            return self.start(agent_name)
        return self._active

    def finish(self) -> None:
        self._active = None

    # 向后兼容别名
    def new_record(self, agent_name: str) -> ExtractionMetrics:
        return self.start(agent_name)

    def totals(self) -> dict:
        return {
            "runs": len(self.records),
            "input_tokens": sum(r.input_tokens for r in self.records),
            "output_tokens": sum(r.output_tokens for r in self.records),
            "entities": sum(r.n_entities for r in self.records),
            "relations": sum(r.n_relations for r in self.records),
        }
