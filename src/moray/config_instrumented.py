"""模型边界埋点（工单 01）
========================

为什么需要这一层：agentscope 2.0 的各家 ChatModel 在解析供应商响应时
  1. 把 OpenAI 兼容协议原生的 ``finish_reason``（stop/length/tool_calls/...）
     丢弃，只保留自己抽象的 interrupted/completed——于是"输出撞长度上限被
     截断"在应用层彻底不可见，截断的 JSON 伪装成普通 schema 校验失败；
  2. 非流式调用一次性返回，框架里没有任何"首 token 时间点"，prefill/decode
     分段耗时无从测起。

本模块不改抽取结果、不改请求参数，只在响应经过模型子类时附加观测信号：
  * ``ChatResponse.metadata["finish_reason"]``：供应商原生结束原因；
  * ``ChatResponse.metadata["timing"] = {"prefill_s", "decode_s"}``：
    仅流式可测；非流式两者均为 None（不用总延迟冒充）。

信号随后由 :class:`moray.extraction.extractor.Extractor` 读取并计入
ChunkCallMetrics。三家 provider（火山方舟/DeepSeek/OpenAI 兼容）都走
OpenAI 风格的 completion 对象，故埋点逻辑共用一份。
"""

from __future__ import annotations

import inspect
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, AsyncIterator


@dataclass
class LastCallSignals:
    """模型实例上"最近一次响应"的观测信号（线程/实例内单写者）。

    为什么不用 ChatResponse.metadata 一路透传：agentscope 的 agent.reply
    走 ReAct + 内置结构化工具路径，最终 Msg 由框架重建，metadata 在这一步
    被丢弃；但同一个 model 实例的响应只有当前这一次在途调用（Extractor
    每篇/每块 await 完成后才读），挂实例槽位等价于"随响应一起返回"，
    又不用改 agentscope。
    """

    finish_reason: str | None = None
    prefill_s: float | None = None
    decode_s: float | None = None

    def update(self, *, finish_reason=None, prefill_s=None, decode_s=None) -> None:
        # 每次调用都重置，不能沿用上一次尝试的值：供应商某次没回 finish_reason
        # 时若保留上一轮的 length，会把一次正常返工误记成截断（信号虚高）。
        self.finish_reason = (
            str(finish_reason).strip().lower()
            if finish_reason is not None else None)
        self.prefill_s = prefill_s
        self.decode_s = decode_s


def attach_response_signals(
    resp: Any,
    completion: Any,
    *,
    start: datetime,
    native_finish_reason: str | None = None,
) -> Any:
    """把供应商原生 finish_reason 挂到一帧（非流式）ChatResponse.metadata。

    completion：openai 类型的 ChatCompletion（取 choices[0].finish_reason）；
    native_finish_reason：已经取好的值（流式末帧）直接传，跳过 choices。
    """
    reason = native_finish_reason
    if reason is None:
        choices = getattr(completion, "choices", None)
        if choices:
            reason = getattr(choices[0], "finish_reason", None)
    meta = dict(getattr(resp, "metadata", None) or {})
    if reason is not None:
        meta["finish_reason"] = str(reason).strip().lower()
    # 非流式没有首 token 时间点，显式给 None（与流式区分，不伪造 0）
    meta.setdefault("timing", {"prefill_s": None, "decode_s": None})
    resp.metadata = meta
    return resp


class _NativeStreamTee:
    """包一层供应商原生异步流：透传 chunk，同时记下原生 finish_reason。

    基类解析器会消费这个流（它只看到普通 async iterator 接口）；消费完后
    ``self.native_finish`` 上留有最后一个 choice chunk 的 finish_reason。
    """

    def __init__(self, raw: Any) -> None:
        self._raw = raw
        self.native_finish: str | None = None

    def __aiter__(self) -> "_NativeStreamTee":
        return self

    async def __anext__(self) -> Any:
        chunk = await self._raw.__anext__()
        choices = getattr(chunk, "choices", None)
        if choices:
            reason = getattr(choices[0], "finish_reason", None)
            if reason:
                self.native_finish = reason
        return chunk

    # openai AsyncStream 通过 async with 管理；透传上下文协议
    async def __aenter__(self) -> "_NativeStreamTee":
        cm = getattr(self._raw, "__aenter__", None)
        if cm is not None:
            await cm()
        return self

    async def __aexit__(self, *exc) -> None:
        cm = getattr(self._raw, "__aexit__", None)
        if cm is not None:
            await cm(*exc)


async def instrument_stream_with_signals(
    base_stream: AsyncIterator[Any],
    tee: "_NativeStreamTee",
    sink: "LastCallSignals",
    *,
    started: float | None = None,
):
    """透传基类已解析好的 ChatResponse 流，结束后把信号写进 ``sink``。

    是 async generator：逐帧 yield 给基类的累积器；信号在流真正结束后才算齐，
    所以写入发生在 GeneratorExit/正常耗尽时（调用方读完所有帧）。
    """
    started = started if started is not None else time.perf_counter()
    first_token_at: float | None = None
    last_content_at: float | None = None
    last_frame = None
    try:
        async for frame in base_stream:
            if getattr(frame, "content", None):
                now = time.perf_counter()
                if first_token_at is None:
                    first_token_at = now
                last_content_at = now
            last_frame = frame
            yield frame
    finally:
        # decode 到最后一个内容帧为止：usage-only/空末帧是协议开销，不算解码。
        ended = (last_content_at if last_content_at is not None
                 else first_token_at)
        if last_frame is not None:
            meta = dict(getattr(last_frame, "metadata", None) or {})
            if tee.native_finish:
                meta["finish_reason"] = str(tee.native_finish).strip().lower()
            meta["timing"] = {
                "prefill_s": (round(first_token_at - started, 6)
                              if first_token_at is not None else None),
                "decode_s": (round(ended - first_token_at, 6)
                             if first_token_at is not None else None),
            }
            last_frame.metadata = meta
        sink.update(
            finish_reason=(str(tee.native_finish).strip().lower()
                           if tee.native_finish else None),
            prefill_s=(round(first_token_at - started, 6)
                       if first_token_at is not None else None),
            decode_s=(round(ended - first_token_at, 6)
                      if first_token_at is not None else None),
        )


def _make_instrumented(base_cls: type):
    """生成一个在响应解析处补信号的 ChatModel 子类（每家共用）。"""

    class _InstrumentedChatModel(base_cls):  # type: ignore[misc, valid-type]
        """同构于父类，仅在响应帧上附加 finish_reason/分段计时观测信号。"""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            # 最近一次响应的信号槽（Extractor 在每次 await 完成后读取）
            self.moray_last_signals = LastCallSignals()

        def _parse_completion_response(
                self, start_datetime, response, *args, **kwargs):
            # 各家签名不一致：Volcengine 的同名方法是 async def，DeepSeek/
            # OpenAI 是普通 def。统一 await（非协程直接取值），三份子类共用。
            # OpenAI 基类按 3 个位置参数调用（多一个 audio_format），必须
            # 原样转发 *args/**kwargs，否则 LLM_PROVIDER=openai 每次非流式
            # 调用都会 TypeError。
            result = super()._parse_completion_response(
                start_datetime, response, *args, **kwargs)

            async def _awrap(coro):
                resp = await coro
                attach_response_signals(resp, response, start=start_datetime)
                self.moray_last_signals.update(
                    finish_reason=resp.metadata.get("finish_reason"))
                return resp

            if inspect.isawaitable(result):
                return _awrap(result)
            attach_response_signals(result, response, start=start_datetime)
            self.moray_last_signals.update(
                finish_reason=result.metadata.get("finish_reason"))
            return result

        async def _parse_stream_response(self, start_datetime, response):
            tee = _NativeStreamTee(response)
            started = time.perf_counter()
            base_stream = super()._parse_stream_response(start_datetime, tee)
            async for frame in instrument_stream_with_signals(
                    base_stream, tee, self.moray_last_signals, started=started):
                yield frame


    _InstrumentedChatModel.__name__ = f"Instrumented{base_cls.__name__}"
    _InstrumentedChatModel.__qualname__ = _InstrumentedChatModel.__name__
    return _InstrumentedChatModel


def instrumented_model_class(base_cls: type) -> type:
    """公开入口：按基类取（或生成并缓存）带埋点的子类。"""
    cache = getattr(instrumented_model_class, "_cache", {})
    cls = cache.get(base_cls)
    if cls is None:
        cls = _make_instrumented(base_cls)
        cache[base_cls] = cls
        instrumented_model_class._cache = cache  # type: ignore[attr-defined]
    return cls
