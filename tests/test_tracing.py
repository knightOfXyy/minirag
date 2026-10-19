"""OTEL 调用链追踪的离线单测（不联网、不起 Agent，用手动 span 验证导出链路）。"""

from opentelemetry import trace

from moray.observability import (
    InMemorySpanCollector,
    build_tracing_middleware,
    setup_tracing,
    manual_span,
)
from agentscope.middleware import TracingMiddleware


def test_setup_returns_collector_and_enables_tracing():
    from agentscope.middleware._tracing._trace import _check_tracing_enabled

    mem = setup_tracing("memory", service_name="moray-test")
    assert isinstance(mem, InMemorySpanCollector)
    assert _check_tracing_enabled() is True
    assert isinstance(build_tracing_middleware(), TracingMiddleware)


def test_spans_record_parent_child():
    mem = setup_tracing("memory", service_name="moray-test-parent")
    with manual_span("root"):
        with manual_span("child"):
            pass
    names = mem.names()
    # SimpleSpanProcessor 在 span 结束时导出：child 先结束
    assert "child" in names and "root" in names
    assert names.index("child") < names.index("root")

    # 校验父子关系
    by_name = {s.name: s for s in mem.spans}
    assert by_name["child"].parent is not None
    assert by_name["child"].parent.span_id == by_name["root"].context.span_id
    assert by_name["root"].parent is None


def test_attributes_attached():
    mem = setup_tracing("memory", service_name="moray-test-attr")
    tracer = trace.get_tracer("moray")
    with tracer.start_as_current_span("marked", attributes={"doc": "d1", "n": 3}):
        pass
    span = next(s for s in mem.spans if s.name == "marked")
    assert span.attributes["doc"] == "d1"
    assert span.attributes["n"] == 3
