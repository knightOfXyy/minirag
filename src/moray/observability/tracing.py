"""
调用链追踪（L13 上）
====================

基于 OpenTelemetry（OTEL）。AgentScope 自带 TracingMiddleware，它在
reply / 模型调用 / 工具执行三个生命周期自动开 span，但前提是进程里已经
初始化了一个【真正的】TracerProvider（否则中间件零开销短路）。

本模块负责初始化：
  · setup_tracing(exporter=...)  配置 TracerProvider 与导出器；
  · build_tracing_middleware()   拿到可塞进 Agent(middlewares=[...]) 的中间件。

三种导出器（离线友好）：
  · "memory"  SpanExporter 收集到内存列表，教学/单测直接断言 span 树；
  · "console" 打印到控制台（OTEL 自带 ConsoleSpanExporter）；
  · "otlp"    推送到 OTLP collector（生产接 Jaeger/Tempo/Phoenix 等），
             地址由环境变量 OTEL_EXPORTER_OTLP_ENDPOINT 决定。

注意：OTEL 追踪回答的是"一次调用经过了哪些环节、各自多久"这种【技术链路】；
业务质量指标（审校返工率、消歧自动率）在 dashboard.py。
"""

from __future__ import annotations

import os
from typing import Literal

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    ConsoleSpanExporter,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)

ExporterKind = Literal["memory", "console", "otlp"]


class InMemorySpanCollector(SpanExporter):
    """把结束的 span 收集进内存列表（教学/测试用，无需外部后端）。"""

    def __init__(self) -> None:
        self.spans: list = []

    def export(self, spans) -> SpanExportResult:
        self.spans.extend(spans)
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:  # noqa: D401
        return None

    def force_flush(self, timeout_millis: float = 30000) -> bool:
        return True

    def names(self) -> list[str]:
        """按结束顺序的 span 名列表。"""
        return [s.name for s in self.spans]


def setup_tracing(
    exporter: ExporterKind = "memory",
    *,
    service_name: str = "moray-ner",
    collector: InMemorySpanCollector | None = None,
) -> InMemorySpanCollector | None:
    """初始化全局 TracerProvider（幂等：重复调用不会叠加 processor）。

    返回内存收集器（exporter='memory' 时），否则返回 None。
    """
    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)

    mem: InMemorySpanCollector | None = None
    if exporter == "memory":
        mem = collector or InMemorySpanCollector()
        provider.add_span_processor(SimpleSpanProcessor(mem))
    elif exporter == "console":
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    elif exporter == "otlp":
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        endpoint = os.environ.get(
            "OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318"
        ).rstrip("/") + "/v1/traces"
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    else:  # pragma: no cover
        raise ValueError(f"未知 exporter: {exporter}")

    # 全局 TracerProvider 只能设置一次：首次创建并 set；之后复用已有的全局
    # provider，把本次的导出 processor 挂上去（每个收集器只收挂载后的 span）。
    existing = trace.get_tracer_provider()
    if isinstance(existing, TracerProvider):
        for proc in provider._active_span_processor._span_processors:  # type: ignore[attr-defined]
            existing.add_span_processor(proc)
    else:
        trace.set_tracer_provider(provider)
    return mem


def build_tracing_middleware():
    """返回 AgentScope 的 OTEL 追踪中间件，放进 Agent(middlewares=[...])。"""
    from agentscope.middleware import TracingMiddleware

    return TracingMiddleware()


def manual_span(name: str, attributes: dict | None = None):
    """给非 Agent 代码段（如批量入库、消歧）手动开 span 的便捷封装。"""
    tracer = trace.get_tracer("moray")
    return tracer.start_as_current_span(name, attributes=attributes or {})
