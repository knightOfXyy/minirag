"""
可观测性：L06 的成本/审计/质量中间件，+ L13 的调用链追踪与质量看板。

- middlewares：审计日志、token 成本、抽取质量（中间件，采集到 MetricsCollector）；
- tracing：基于 OpenTelemetry，配合 agentscope.TracingMiddleware 记录调用链；
- dashboard：业务级质量指标落库聚合（审校返工、消歧自动率、成本）。
"""

from .metrics import ExtractionMetrics, MetricsCollector
from .middlewares import (
    AuditMiddleware,
    CostAccountingMiddleware,
    ExtractionMetricsMiddleware,
    build_observability_stack,
)
from .tracing import (
    InMemorySpanCollector,
    build_tracing_middleware,
    manual_span,
    setup_tracing,
)
from .dashboard import Dashboard, RunRecorder, pipeline_runs_schema

__all__ = [
    # L06
    "ExtractionMetrics",
    "MetricsCollector",
    "AuditMiddleware",
    "CostAccountingMiddleware",
    "ExtractionMetricsMiddleware",
    "build_observability_stack",
    # L13
    "InMemorySpanCollector",
    "build_tracing_middleware",
    "setup_tracing",
    "manual_span",
    "Dashboard",
    "RunRecorder",
    "pipeline_runs_schema",
]
