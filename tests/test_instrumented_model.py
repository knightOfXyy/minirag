"""工单 01：模型边界埋点单测（不联网）。

agentscope 2.0 的响应解析丢弃了供应商原生 finish_reason（只留
interrupted/completed），且非流式拿不到首 token 时间点。Instrumented* 模型在
我们这一层把它们捞回来：

  · finish_reason：OpenAI 兼容网关的 stop/length/tool_calls/... 原样挂到
    ChatResponse.metadata["finish_reason"]（再由 Extractor 提到 reply 上）；
  · 流式分段计时：首 token=请求发出到第一个非空 delta，decode=首 token 到结束，
    挂到 ChatResponse.metadata["timing"]；
  · 非流式：只挂 finish_reason，分段计时不给（None），不用总耗时冒充。
"""

from datetime import datetime
from types import SimpleNamespace

import pytest

from moray.config_instrumented import (
    LastCallSignals,
    _NativeStreamTee,
    attach_response_signals,
    instrumented_model_class,
    instrument_stream_with_signals,
)


def _choice(finish_reason, content="x", **kw):
    return SimpleNamespace(
        finish_reason=finish_reason,
        message=SimpleNamespace(content=content, tool_calls=None,
                                reasoning_content=None, **kw),
    )


def test_non_streaming_completion_carries_native_finish_reason():
    """length 必须原样带出来（agentscope 默认解析会丢掉它）。"""
    from agentscope.model import ChatResponse

    resp = ChatResponse(content=[], is_last=True)
    completion = SimpleNamespace(
        choices=[_choice("length", content="")],
        usage=None, id="r1")
    attach_response_signals(resp, completion)
    assert resp.metadata["finish_reason"] == "length"


def test_non_streaming_stop_finish_reason():
    from agentscope.message import TextBlock
    from agentscope.model import ChatResponse

    resp = ChatResponse(content=[TextBlock(text="ok")], is_last=True)
    completion = SimpleNamespace(
        choices=[_choice("stop", content="ok")], usage=None, id="r2")
    attach_response_signals(resp, completion)
    assert resp.metadata["finish_reason"] == "stop"
    # 非流式不伪造首 token 计时
    assert resp.metadata.get("timing", {}).get("prefill_s") is None


class _FakeRawChunk:
    def __init__(self, finish_reason):
        self.choices = [SimpleNamespace(finish_reason=finish_reason)]


async def _base_stream_from(texts):
    """模拟基类解析器：吐携带 TextBlock 的 ChatResponse delta。"""
    from agentscope.message import TextBlock
    from agentscope.model import ChatResponse

    for text in texts:
        content = [TextBlock(text=text)] if text else []
        yield ChatResponse(content=content, is_last=False)


@pytest.mark.asyncio
async def test_streaming_wraps_chunks_with_phase_timing(monkeypatch):
    """流式：首个非空 delta 定 prefill 边界，结束时给 decode。"""
    # 帧序列：首内容帧（prefill 边界 100.2）-> 次内容帧（100.9，decode 终点）
    # -> 收尾（不再计入 decode）
    ticks = iter([100.2, 100.9, 101.5])
    monkeypatch.setattr(
        "moray.config_instrumented.time.perf_counter", lambda: next(ticks))

    class _Raw:
        def __aiter__(self):
            return self

        async def __anext__(self):
            data = [_FakeRawChunk(None), _FakeRawChunk(None),
                    _FakeRawChunk("stop")]
            if not hasattr(self, "_i"):
                self._i = 0
            if self._i >= len(data):
                raise StopAsyncIteration
            c = data[self._i]
            self._i += 1
            return c

    tee = _NativeStreamTee(_Raw())
    # tee 与 base 解析器"并行"消费同一逻辑：这里手工驱动 tee 抓 finish_reason
    for _ in range(3):
        await tee.__anext__()

    sink = LastCallSignals()
    base = _base_stream_from(["甲", "乙"])
    out = [d async for d in instrument_stream_with_signals(
        base, tee, sink, started=100.0)]

    last = out[-1]
    assert last.metadata["finish_reason"] == "stop"
    timing = last.metadata["timing"]
    assert timing["prefill_s"] == pytest.approx(0.2)   # 100.2 - 100.0
    assert timing["decode_s"] == pytest.approx(0.7)    # 100.9 - 100.2（末帧不重置）
    assert sink.finish_reason == "stop"
    assert sink.prefill_s == pytest.approx(0.2)
    assert sink.decode_s == pytest.approx(0.7)


@pytest.mark.asyncio
async def test_streaming_detects_length_finish():
    class _Raw:
        def __aiter__(self):
            return self

        async def __anext__(self):
            if getattr(self, "_done", False):
                raise StopAsyncIteration
            self._done = True
            return _FakeRawChunk("length")

    tee = _NativeStreamTee(_Raw())
    await tee.__anext__()
    sink = LastCallSignals()
    base = _base_stream_from([""])
    out = [d async for d in instrument_stream_with_signals(base, tee, sink)]
    assert out[-1].metadata["finish_reason"] == "length"
    assert sink.finish_reason == "length"


@pytest.mark.asyncio
async def test_native_stream_tee_captures_finish_reason():
    """Tee 透传原生 chunk 的同时记下末个 finish_reason。"""

    class _Raw:
        def __aiter__(self):
            return self

        async def __anext__(self):
            data = [_FakeRawChunk(None), _FakeRawChunk("length")]
            if not hasattr(self, "_i"):
                self._i = 0
            if self._i >= len(data):
                raise StopAsyncIteration
            c = data[self._i]
            self._i += 1
            return c

    tee = _NativeStreamTee(_Raw())
    seen = [c async for c in tee]
    assert len(seen) == 2
    assert tee.native_finish == "length"


def test_instrumented_model_class_resolves_known_providers():
    from agentscope.model import (
        DeepSeekChatModel,
        OpenAIChatModel,
        VolcengineChatModel,
    )

    assert issubclass(
        instrumented_model_class(VolcengineChatModel), VolcengineChatModel)
    assert issubclass(
        instrumented_model_class(DeepSeekChatModel), DeepSeekChatModel)
    assert issubclass(
        instrumented_model_class(OpenAIChatModel), OpenAIChatModel)


def test_openai_parse_accepts_base_class_three_positional_args():
    """F1 回归：OpenAI 基类按 3 个位置参数调用解析器，覆盖不能窄化签名。

    只断言 issubclass 会漏掉 TypeError；这里按基类真实调用形态走一遍。
    """
    from agentscope.model import OpenAIChatModel

    cls = instrumented_model_class(OpenAIChatModel)
    model = object.__new__(cls)
    model.moray_last_signals = LastCallSignals()
    completion = SimpleNamespace(
        choices=[_choice("length", content="x")],
        usage=None, id="openai-r1")
    start = datetime.now()
    resp = model._parse_completion_response(start, completion, "wav")
    assert resp.metadata["finish_reason"] == "length"
    assert model.moray_last_signals.finish_reason == "length"


def test_last_call_signals_reset_finish_reason_between_attempts():
    """F6：供应商这次没回 finish_reason 时，不能沿用上一轮的 length。"""
    sink = LastCallSignals()
    sink.update(finish_reason="length", prefill_s=0.1, decode_s=0.2)
    sink.update(finish_reason=None, prefill_s=None, decode_s=None)
    assert sink.finish_reason is None
    assert sink.prefill_s is None and sink.decode_s is None


@pytest.mark.asyncio
async def test_real_agent_reply_propagates_signals_via_model_sink(monkeypatch):
    """端到端接缝：真实 Agent.reply + 假模型实例，Extractor 能从 Msg 读到信号。

    agentscope 的最终 Msg 重建时丢 ChatResponse.metadata；验证我们的模型槽位
    + _call_structured 桥接能把 finish_reason 送到 Extractor 的度量层。
    """
    from agentscope.agent import Agent
    from agentscope.message import ToolCallBlock
    from agentscope.model import ChatResponse
    from moray.extraction import Document, Extractor
    from moray.extraction.extractor import _finish_reason_from_msg

    tool_input = ('{"entities": [], "relations": []}')

    class _FakeInstrumented:
        """最小 duck-type：Agent 只调 model(...) 与字段；带信号槽。"""

        def __init__(self, finish):
            self.moray_last_signals = SimpleNamespace(
                finish_reason=finish, prefill_s=0.21, decode_s=0.31)
            self.formatter = SimpleNamespace(
                supported_input_media_types=None)
            self.context_size = 1_000_000
            self.model = "fake-model"

        def format(self, *a, **k):
            return a[0] if a else []

        async def count_tokens(self, *args, **kwargs):
            return 10

        async def __call__(self, messages, tools=None, tool_choice=None):
            return ChatResponse(
                content=[ToolCallBlock(
                    id="t", name="GenerateStructuredOutput",
                    input=tool_input)],
                is_last=True,
                metadata={"finish_reason": self.moray_last_signals.finish_reason})

    class _RealBridgeExtractor(Extractor):
        def _build_agent(self):
            return Agent(name="Extractor", model=_FakeInstrumented("length"),
                         system_prompt="x")

    ex = _RealBridgeExtractor("general", max_retries=0)
    out = await ex.extract(Document(id="doc", text="正文"))
    assert out.call_metrics.finish_reason == "length"
    assert out.call_metrics.truncated_attempts == 1
    assert out.call_metrics.prefill_s == pytest.approx(0.21)
    assert out.call_metrics.decode_s == pytest.approx(0.31)


@pytest.mark.asyncio
async def test_second_model_call_in_same_reply_does_not_hide_length_signal(
        caplog):
    """B1 回归：同一次 reply 内截断后自纠的第 2 次模型调用不能覆盖截断信号。

    agentscope 的 ReAct 循环里，首次输出撞 max_tokens -> 工具参数 JSON 残缺 ->
    工具报错 -> 同一个 reply 内再调一次模型。单槽 LastCallSignals 只留最后一次
    stop，截断计数与 WARNING 会全静默；信号必须按"模型调用"累积。
    """
    import logging

    from agentscope.agent import Agent
    from agentscope.message import ToolCallBlock
    from agentscope.model import ChatResponse
    from moray.extraction import Document, Extractor

    class _TruncatedThenFixed:
        """真 Agent 用的最小模型：第一次残缺 JSON+length，第二次合法+stop。"""

        def __init__(self):
            self.calls = 0
            self.moray_last_signals = LastCallSignals()
            self.formatter = SimpleNamespace(
                supported_input_media_types=None)
            self.context_size = 1_000_000
            self.model = "fake-model"

        def format(self, *a, **k):
            return a[0] if a else []

        async def count_tokens(self, *args, **kwargs):
            return 10

        async def __call__(self, messages, tools=None, tool_choice=None):
            self.calls += 1
            if self.calls == 1:
                self.moray_last_signals.update(
                    finish_reason="length", prefill_s=0.5, decode_s=1.0)
                return ChatResponse(
                    content=[ToolCallBlock(
                        id="t1", name="GenerateStructuredOutput",
                        input='{"entities": [{"name": ')],
                    is_last=True, metadata={"finish_reason": "length"})
            self.moray_last_signals.update(
                finish_reason="stop", prefill_s=0.2, decode_s=0.3)
            return ChatResponse(
                content=[ToolCallBlock(
                    id="t2", name="GenerateStructuredOutput",
                    input='{"entities": [], "relations": []}')],
                is_last=True, metadata={"finish_reason": "stop"})

    model = _TruncatedThenFixed()

    class _Bridge(Extractor):
        def _build_agent(self):
            return Agent(name="Extractor", model=model, system_prompt="x")

    with caplog.at_level(logging.WARNING, logger="moray.extraction.extractor"):
        out = await _Bridge("general", max_retries=0).extract(
            Document(id="b1-doc", text="正文"))

    assert model.calls == 2, "前提：同一次 reply 内确实发生了两次模型调用"
    m = out.call_metrics
    assert out.attempts == 1
    assert m.attempt_finish_reasons == ["length", "stop"]
    assert m.truncated_attempts == 1
    # 两次模型调用的分段计时都应累计（不能只留最后一次）
    assert m.prefill_s == pytest.approx(0.7)
    assert m.decode_s == pytest.approx(1.3)
    warn_records = [r for r in caplog.records
                    if getattr(r, "truncated", False)]
    assert warn_records, "截断必须打出带 truncated=True 的告警，不能被第二次调用吞掉"
    assert warn_records[0].finish_reason == "length"

    from moray.extraction.chunk_metrics import DocumentMetrics
    dm = DocumentMetrics(document_id="b1-doc", n_chunks=1)
    dm.add(m)
    assert dm.summary()["truncation_signals"] == 1


@pytest.mark.asyncio
async def test_non_streaming_call_records_wall_clock_latency():
    """S1：生产非流式也能给出可归因的耗时（墙钟），但绝不冒充 prefill/decode。

    非流式网关一次性返回，协议层没有首 token 时间点；补一个诚实的
    ``latency_s``（一次模型调用墙钟）让"时间花在哪"至少可测，
    prefill_s/decode_s 仍如实为 None。
    """
    import asyncio

    from agentscope.agent import Agent
    from agentscope.message import ToolCallBlock
    from agentscope.model import ChatResponse
    from moray.extraction import Document, Extractor

    class _SlowModel:
        def __init__(self):
            self.moray_last_signals = LastCallSignals()
            self.formatter = SimpleNamespace(
                supported_input_media_types=None)
            self.context_size = 1_000_000
            self.model = "fake-model"

        def format(self, *a, **k):
            return a[0] if a else []

        async def count_tokens(self, *args, **kwargs):
            return 10

        async def __call__(self, messages, tools=None, tool_choice=None):
            await asyncio.sleep(0.02)
            self.moray_last_signals.update(
                finish_reason="tool_calls", prefill_s=None, decode_s=None)
            return ChatResponse(
                content=[ToolCallBlock(
                    id="t", name="GenerateStructuredOutput",
                    input='{"entities": [], "relations": []}')],
                is_last=True, metadata={"finish_reason": "tool_calls"})

    class _Bridge(Extractor):
        def _build_agent(self):
            return Agent(name="Extractor", model=_SlowModel(),
                         system_prompt="x")

    out = await _Bridge("general", max_retries=0).extract(
        Document(id="lat-doc", text="正文"))
    m = out.call_metrics
    assert m.latency_s is not None and m.latency_s >= 0.015
    assert m.prefill_s is None and m.decode_s is None
