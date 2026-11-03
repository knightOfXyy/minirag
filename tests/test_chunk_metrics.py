"""工单 01：分块级抽取度量与输出截断护栏（离线，不联网）。

覆盖（对验收条目）：
  · 每个分块产出一条无缺省字段的分块调用记录（输入/输出 token、attempts、
    结束原因、排队/prefill/decode 耗时、命中缓存输入 token）；
  · 文档级聚合：调用数=块数、总 prefill/decode、返工率、截断块数；
  · finish_reason=length 被显式识别计数并产出告警信号，而不是伪装成校验失败；
  · 第 2 次尝试才过校验：attempts=2，两次 token 都被累计。

时间字段在假模型路径可精确注入；真实网关路径拿不到分段计时时如实记 None，
绝不用总耗时冒充 prefill/decode。
"""

import logging
from types import SimpleNamespace

import pytest

from moray.extraction import Document, Extractor
from moray.extraction.chunk_metrics import (
    FINISH_LENGTH,
    FINISH_UNKNOWN,
    ChunkCallMetrics,
    DocumentMetrics,
)


# ---------------------------------------------------------------------------
# 假模型：脚本化每次尝试的返回，可带结束原因与分段计时
# ---------------------------------------------------------------------------
class _ProbeExtractor(Extractor):
    """按队列返回假回复；每条回复可以是 dict（内容）或 (dict, attrs)。

    attrs 支持 finish_reason / prefill_s / decode_s，模拟模型边界采集到的信号。
    """

    def __init__(self, replies=None, *, max_retries=1, default_finish="stop"):
        super().__init__("general", max_retries=max_retries)
        self._script = list(replies or [])
        self._default_finish = default_finish

    def _build_agent(self):
        return SimpleNamespace()

    async def _call_structured(self, agent, content):
        item = self._script.pop(0) if self._script else (
            {"entities": [], "relations": []}, {})
        if isinstance(item, tuple):
            reply, attrs = item
        else:
            reply, attrs = item, {}
        return SimpleNamespace(
            structured_output=reply,
            usage=SimpleNamespace(
                input_tokens=attrs.get("input_tokens", 100),
                output_tokens=attrs.get("output_tokens", 10),
                cache_input_tokens=attrs.get("cache_input_tokens", 0)),
            finish_reason=attrs.get("finish_reason", self._default_finish),
            timing=SimpleNamespace(
                prefill_s=attrs.get("prefill_s"),
                decode_s=attrs.get("decode_s")),
        )


def test_chunk_record_requires_no_field_defaults_for_core_fields():
    """记录的核心字段（token/attempts/结束原因）必须显式给出，不允许缺省静默为 0。

    构造时不给 finish_reason/attempts/usage 必须报错——这正是"字段无缺省"
    验收条目的防线：拿不到结束原因就写 'unknown'，不允许悄悄当成功。
    """
    with pytest.raises(TypeError):
        ChunkCallMetrics(chunk_id="c0")  # type: ignore[call-arg]


def test_document_aggregation_counts_calls_rework_and_truncation():
    """文档级摘要：调用数=块数、总 prefill/decode、返工率、截断块数。"""
    docs = DocumentMetrics(document_id="d1", n_chunks=4)
    # 4 个块：2 个一次过、1 个返工 1 次、1 个被截断
    docs.add(ChunkCallMetrics(
        chunk_id="c0", attempts=1, finish_reason="stop",
        input_tokens=100, output_tokens=40, cached_input_tokens=80,
        wait_s=0.01, prefill_s=0.20, decode_s=0.30))
    docs.add(ChunkCallMetrics(
        chunk_id="c1", attempts=1, finish_reason="stop",
        input_tokens=120, output_tokens=50, cached_input_tokens=90,
        wait_s=0.02, prefill_s=0.22, decode_s=0.35))
    docs.add(ChunkCallMetrics(
        chunk_id="c2", attempts=2, finish_reason="stop",
        input_tokens=260, output_tokens=90, cached_input_tokens=100,
        wait_s=0.03, prefill_s=0.40, decode_s=0.60))
    docs.add(ChunkCallMetrics(
        chunk_id="c3", attempts=1, finish_reason=FINISH_LENGTH,
        input_tokens=110, output_tokens=200, cached_input_tokens=70,
        wait_s=0.04, prefill_s=0.21, decode_s=0.90))

    assert len(docs.chunks) == 4
    s = docs.summary()
    assert s["chunk_calls"] == 4
    assert s["truncated_chunks"] == 1
    # 返工块 1 / 调用块 4 = 0.25（返工 = attempts>1 的块）
    assert s["rework_rate"] == 0.25
    assert s["total_attempts"] == 5
    assert s["prefill_s"] == pytest.approx(1.03)
    assert s["decode_s"] == pytest.approx(2.15)
    assert s["wait_s"] == pytest.approx(0.10)
    assert s["input_tokens"] == 590
    assert s["output_tokens"] == 380
    assert s["cached_input_tokens"] == 340


def test_length_finish_reason_is_truncation():
    """finish_reason 的各种 length 形态都识别为截断。"""
    assert ChunkCallMetrics(
        chunk_id="c", attempts=1, finish_reason="length",
        input_tokens=1, output_tokens=1, cached_input_tokens=0,
        wait_s=0.0, prefill_s=None, decode_s=None).truncated is True
    assert ChunkCallMetrics(
        chunk_id="c", attempts=1, finish_reason="max_tokens",
        input_tokens=1, output_tokens=1, cached_input_tokens=0,
        wait_s=0.0, prefill_s=None, decode_s=None).truncated is True
    assert ChunkCallMetrics(
        chunk_id="c", attempts=1, finish_reason="stop",
        input_tokens=1, output_tokens=1, cached_input_tokens=0,
        wait_s=0.0, prefill_s=None, decode_s=None).truncated is False


def test_summary_is_json_serializable_with_none_phase_timings():
    """真实路径拿不到分段计时时为 None，序列化不炸、聚合跳过 None。"""
    import json

    docs = DocumentMetrics(document_id="d", n_chunks=1)
    docs.add(ChunkCallMetrics(
        chunk_id="c0", attempts=1, finish_reason="unknown",
        input_tokens=10, output_tokens=5, cached_input_tokens=0,
        wait_s=0.0, prefill_s=None, decode_s=None))
    s = docs.summary()
    json.dumps(s)  # 不抛即可
    assert s["prefill_s"] is None and s["decode_s"] is None
    assert s["truncated_chunks"] == 0


# ---------------------------------------------------------------------------
# Extractor：每次尝试的结束原因/token/分段计时进入 output.call_metrics
# ---------------------------------------------------------------------------
_GOOD = {"entities": [], "relations": []}
_BAD = {"entities": [{"name": "x", "type": "NOT_A_TYPE"}], "relations": []}


@pytest.mark.asyncio
async def test_extractor_records_finish_reason_and_phase_timing_per_attempt():
    """成功路径：一次尝试，finish_reason 与 prefill/decode 分段计时都被记录。"""
    ex = _ProbeExtractor([
        (_GOOD, {"finish_reason": "stop", "input_tokens": 100,
                 "output_tokens": 20, "cache_input_tokens": 40,
                 "prefill_s": 0.21, "decode_s": 0.33})])
    out = await ex.extract(Document(id="d", text="某正文。"))
    m = out.call_metrics
    assert m.attempts == 1
    assert m.finish_reason == "stop"
    assert m.attempt_finish_reasons == ["stop"]
    assert m.input_tokens == 100 and m.output_tokens == 20
    assert m.cached_input_tokens == 40
    assert m.prefill_s == pytest.approx(0.21)
    assert m.decode_s == pytest.approx(0.33)
    assert m.truncated is False


@pytest.mark.asyncio
async def test_extractor_second_attempt_passes_both_attempts_counted():
    """验收：第 2 次尝试才过校验 -> attempts=2，两次 token 都被累计。"""
    ex = _ProbeExtractor([
        (_BAD, {"finish_reason": "stop", "input_tokens": 100,
                "output_tokens": 30, "prefill_s": 0.2, "decode_s": 0.4}),
        (_GOOD, {"finish_reason": "stop", "input_tokens": 130,
                 "output_tokens": 25, "prefill_s": 0.3, "decode_s": 0.5}),
    ], max_retries=1)
    out = await ex.extract(Document(id="d", text="某正文。"))
    m = out.call_metrics
    assert out.attempts == 2 and m.attempts == 2
    assert m.attempt_finish_reasons == ["stop", "stop"]
    assert m.input_tokens == 230 and m.output_tokens == 55
    assert m.prefill_s == pytest.approx(0.5)
    assert m.decode_s == pytest.approx(0.9)
    assert m.error == ""


@pytest.mark.asyncio
async def test_extractor_flags_length_finish_as_truncation_with_warning(caplog):
    """验收：finish_reason=length 必须显式识别为截断并打 WARNING，含专用信号字段。

    截断的 JSON 同时会让 schema 校验失败，但护栏信号不能因此被吞掉：
    即使随后返工，第一次 length 也要在 truncated_attempts 里计数并告警。
    """
    ex = _ProbeExtractor([
        (_BAD, {"finish_reason": FINISH_LENGTH, "input_tokens": 100,
                "output_tokens": 200}),
        (_GOOD, {"finish_reason": "stop", "input_tokens": 100,
                 "output_tokens": 20}),
    ], max_retries=1)
    with caplog.at_level(logging.WARNING, logger="moray.extraction.extractor"):
        out = await ex.extract(Document(id="d1", text="某正文。"))
    m = out.call_metrics
    assert m.attempts == 2
    assert m.truncated_attempts == 1
    assert "length" in m.attempt_finish_reasons
    # 告警日志里必须带 document_id、attempt 与显式的截断事件字段
    warn_text = "\n".join(r.getMessage() for r in caplog.records
                         if r.levelno >= logging.WARNING)
    assert "d1" in warn_text and "length" in warn_text
    assert any(getattr(r, "truncated", False) for r in caplog.records), \
        "WARNING 记录必须挂 truncated=True 信号字段，不能只是普通文本日志"


# ---------------------------------------------------------------------------
# ChunkedExtractionPipeline：排队耗时 + 文档级分块度量组装
# ---------------------------------------------------------------------------
from moray.extraction import ChunkedExtractionPipeline, Chunker  # noqa: E402


@pytest.mark.asyncio
async def test_pipeline_emits_document_metrics_one_record_per_chunk():
    """验收：多块文档每块一条记录（输入/输出 token、attempts、结束原因无缺省），

    并可读文档级聚合：调用数=块数、总 prefill/decode、返工率、截断块数。
    """
    text = "华为技术有限公司宣布与贵阳市政府合作。" * 8
    chunks = Chunker(max_units=40, overlap_units=0).chunk_text(text)
    assert len(chunks) >= 3
    ex = _ProbeExtractor([
        (_GOOD, {"finish_reason": "stop", "input_tokens": 100 + i,
                 "output_tokens": 20 + i, "prefill_s": 0.1 + 0.01 * i,
                 "decode_s": 0.2 + 0.01 * i})
        for i in range(len(chunks))
    ])
    out = await ChunkedExtractionPipeline(ex, concurrency=2).run(
        Document(id="doc1", text=text), chunks)

    assert out.document_metrics is not None
    dm = out.document_metrics
    assert dm.document_id == "doc1"
    assert dm.n_chunks == len(chunks)
    assert len(dm.chunks) == len(chunks)
    ids = {c.chunk_id for c in dm.chunks}
    assert ids == {c.id for c in chunks}
    for rec in dm.chunks:
        # 核心字段无缺省：结束原因非空、attempts>=1、token 显式给出
        assert rec.finish_reason == "stop"
        assert rec.attempts >= 1
        assert rec.input_tokens > 0 and rec.output_tokens > 0
        assert rec.wait_s >= 0.0
        assert rec.prefill_s is not None and rec.decode_s is not None

    s = dm.summary()
    assert s["chunk_calls"] == len(chunks)
    assert s["truncated_chunks"] == 0
    assert s["rework_rate"] == 0.0
    assert s["prefill_s"] > 0 and s["decode_s"] > 0


@pytest.mark.asyncio
async def test_pipeline_summary_counts_truncated_and_rework_chunks():
    """4 块里 1 块截断（最终仍失败计数）、1 块第 2 次尝试通过。"""
    text = "甲公司与乙公司在上海签约共建数据中心。" * 8
    chunks = Chunker(max_units=40, overlap_units=0).chunk_text(text)
    assert len(chunks) == 4
    # 每块独立一个 Extractor 实例，按块 id 编排脚本更直观
    scripted = {
        0: [(_GOOD, {"finish_reason": "stop"})],
        1: [(_BAD, {"finish_reason": "stop"}),
            (_GOOD, {"finish_reason": "stop"})],
        2: [(_GOOD, {"finish_reason": "stop"})],
        # 截断且两次都没救回 -> ExtractionError，但度量仍要保留
        3: [(_BAD, {"finish_reason": FINISH_LENGTH, "output_tokens": 200}),
            (_BAD, {"finish_reason": FINISH_LENGTH, "output_tokens": 200})],
    }

    class _ScriptedById(_ProbeExtractor):
        async def extract(self, document):  # type: ignore[override]
            idx = int(document.id.rsplit("#c", 1)[1].lstrip("0") or "0")
            self._script = list(scripted[idx])
            self.max_retries = 1
            return await super().extract(document)

    ex = _ScriptedById(max_retries=1)
    out = await ChunkedExtractionPipeline(ex, concurrency=1).run(
        Document(id="doc2", text=text), chunks, raise_on_chunk_error=False)

    s = out.document_metrics.summary()
    assert s["chunk_calls"] == 4
    assert s["truncated_chunks"] == 1          # c3 最终 finish_reason=length
    assert s["truncation_signals"] == 1
    assert s["reworked_chunks"] == 2           # c1 返工通过 + c3 返工失败
    assert s["rework_rate"] == 0.5
    assert s["total_attempts"] == 6            # 1+2+1+2
    failed = [c for c in out.document_metrics.chunks if c.error]
    assert len(failed) == 1 and failed[0].chunk_id.endswith("#c0003")
    # 失败块已经烧掉的 token 也要累计进文档用量（否则成本被系统性低估）
    assert out.usage.input_tokens == 600      # 100 + 200 + 100 + 200
    assert out.usage.output_tokens == 440     # 10 + 20 + 10 + 2×200


@pytest.mark.asyncio
async def test_pipeline_raises_by_default_but_metrics_still_attached():
    """默认行为不变：单块失败抛异常；异常上带该块度量（截断信号不丢）。"""
    from moray.extraction import ExtractionError

    text = "甲公司与乙公司签约。" * 8
    chunks = Chunker(max_units=40, overlap_units=0).chunk_text(text)

    class _AlwaysTruncated(_ProbeExtractor):
        async def extract(self, document):  # type: ignore[override]
            try:
                return await super().extract(document)
            except ExtractionError as exc:
                assert exc.metrics is not None
                assert exc.metrics.truncated_attempts >= 1
                raise

    ex = _AlwaysTruncated(
        [(_BAD, {"finish_reason": FINISH_LENGTH})] * 10, max_retries=1)
    with pytest.raises(ExtractionError) as excinfo:
        await ChunkedExtractionPipeline(ex, concurrency=1).run(
            Document(id="doc3", text=text), chunks)
    dm = getattr(excinfo.value, "document_metrics", None)
    assert dm is not None
    assert dm.summary()["truncation_signals"] >= 1


# ---------------------------------------------------------------------------
# run_chunked_team：分块文档级度量透传到编排产物（API/批量共用同一入口）
# ---------------------------------------------------------------------------
from moray.orchestration import run_chunked_team  # noqa: E402


class _ScriptedProvider:
    """模拟 DynamicExtractorProvider：按 chunk 文档 id 给脚本化回复。"""

    def __init__(self, replies_by_index):
        self.replies = replies_by_index

    async def get_extractor(self, domain_code):
        outer = self

        class _Extractor(_ProbeExtractor):
            async def extract(self, document):  # type: ignore[override]
                idx = int(document.id.rsplit("#c", 1)[1])
                self._script = list(outer.replies[idx])
                self.max_retries = 1
                return await super().extract(document)

        return _Extractor(max_retries=1), SimpleNamespace(code="general")


@pytest.mark.asyncio
async def test_run_chunked_team_carries_document_metrics():
    """端到端（不经 HTTP/DB）：分块路径的 ReviewedExtraction 带文档级度量。"""
    text = "甲公司与乙公司在上海签约共建数据中心。" * 8
    assert Chunker(max_units=40, overlap_units=0).count(text) > 40

    async def extractor_fn(text, feedback=None):
        raise AssertionError("provider 路径不应走闭包 extractor_fn")

    provider = _ScriptedProvider({
        i: [(_GOOD, {"finish_reason": "stop", "input_tokens": 100 + i,
                     "output_tokens": 20, "prefill_s": 0.1, "decode_s": 0.2})]
        for i in range(4)})
    reviewed = await run_chunked_team(
        text, extractor=extractor_fn, provider=provider,
        chunk_threshold_units=10, chunk_max_units=40, chunk_overlap_units=0)
    assert reviewed.document_metrics is not None
    s = reviewed.document_metrics.summary()
    assert s["chunk_calls"] == 4 and s["truncated_chunks"] == 0
    assert s["input_tokens"] == sum(100 + i for i in range(4))


# ---------------------------------------------------------------------------
# RunRecorder：文档级分块度量进入看板数据面（纯组装，无需 DB）
# ---------------------------------------------------------------------------
from moray.observability import RunRecorder  # noqa: E402


def test_run_recorder_carries_chunk_summary_fields():
    """看板记录必须带分块度量列：块调用数/截断块数/截断信号/返工率/分段耗时。"""
    reviewed = SimpleNamespace(
        result=SimpleNamespace(entities=[], relations=[]),
        accepted=True,
        review=SimpleNamespace(span_ratio=1.0, iterations=1, issues=[]),
        document_metrics=DocumentMetrics(document_id="d", n_chunks=3),
    )
    reviewed.document_metrics.chunks = [
        ChunkCallMetrics(chunk_id=f"c{i}", attempts=(2 if i == 1 else 1),
                         finish_reason="stop",
                         input_tokens=100, output_tokens=20, cached_input_tokens=0,
                         wait_s=0.01,
                         prefill_s=(0.2 if i == 1 else 0.1),
                         decode_s=(0.4 if i == 1 else 0.2))
        for i in range(3)]
    ingest_report = SimpleNamespace(merged=[], queued=[], created=[])
    rec = RunRecorder.from_pipeline(
        document_id="d", reviewed=reviewed, ingest_report=ingest_report,
        input_tokens=300, output_tokens=60)
    assert rec.chunk_calls == 3
    assert rec.truncated_chunks == 0
    assert rec.truncation_signals == 0
    assert rec.rework_rate == pytest.approx(1 / 3)
    assert rec.prefill_ms == 400 and rec.decode_ms == 800
    # 分块明细随看板记录一起落库（下钻用）
    assert rec.chunk_metrics_json is not None
    assert {c["chunk_id"] for c in rec.chunk_metrics_json} == {"c0", "c1", "c2"}


@pytest.mark.asyncio
async def test_run_chunked_team_non_chunked_has_no_metrics():
    """短文本走旧闭环时 document_metrics 为 None（不伪造分块度量）。"""
    async def extractor_fn(text, feedback=None):
        from moray.schema import ExtractionResult
        return ExtractionResult(entities=[], relations=[])

    reviewed = await run_chunked_team(
        "短文本", extractor=extractor_fn, provider=None)
    assert reviewed.document_metrics is None


@pytest.mark.asyncio
async def test_extractor_missing_finish_reason_is_unknown_not_silent_stop():
    """老/假返回没有结束原因时显式记 unknown，不允许静默当成功。"""
    class _NoFinish(_ProbeExtractor):
        async def _call_structured(self, agent, content):
            return SimpleNamespace(
                structured_output=_GOOD,
                usage=SimpleNamespace(input_tokens=1, output_tokens=1,
                                      cache_input_tokens=0),
                finish_reason=None, timing=None)

    out = await _NoFinish().extract(Document(id="d", text="正文。"))
    assert out.call_metrics.finish_reason == FINISH_UNKNOWN


@pytest.mark.asyncio
async def test_run_chunked_team_keeps_raising_on_permanent_chunk_failure():
    """只加观测：分块永久失败时仍像改动前一样整篇抛错（不吞成部分结果）。

    度量不能因此丢：异常上带文档级度量，截断块数在调用方仍可读。
    """
    from moray.extraction import ExtractionError

    text = "甲公司与乙公司在上海签约共建数据中心。" * 8
    replies = {
        i: [(_GOOD, {"finish_reason": "stop"})] for i in range(4)}
    # c0 两次尝试都撞长度上限（校验也失败）-> 该块永久失败
    replies[0] = [(_BAD, {"finish_reason": FINISH_LENGTH})] * 2
    provider = _ScriptedProvider(replies)

    async def extractor_fn(text, feedback=None):
        raise AssertionError("provider 路径不应走闭包 extractor_fn")

    with pytest.raises(ExtractionError) as excinfo:
        await run_chunked_team(
            text, extractor=extractor_fn, provider=provider,
            chunk_threshold_units=10, chunk_max_units=40,
            chunk_overlap_units=0)

    dm = getattr(excinfo.value, "document_metrics", None)
    assert dm is not None, "失败也要带文档级度量，截断信号不能被异常吞掉"
    assert dm.summary()["truncated_chunks"] == 1
