"""RunRecorder.from_pipeline 组装逻辑的离线单测。"""

from types import SimpleNamespace

from moray.extraction.chunk_metrics import ChunkCallMetrics, DocumentMetrics
from moray.observability import RunRecorder
from moray.schema import Entity, ExtractionResult, Relation


def _reviewed(accepted=True, iters=2, span_ratio=0.9, issues=None):
    result = ExtractionResult(
        entities=[Entity(name="A", type="ORG"), Entity(name="B", type="PER")],
        relations=[Relation(head="A", predicate="r", tail="B")],
    )
    review = SimpleNamespace(
        iterations=iters, span_ratio=span_ratio,
        issues=issues if issues is not None else [])
    return SimpleNamespace(
        accepted=accepted, result=result, review=review,
        history=[])


def _ingest(merged=("A",), queued=("C",), created=("A", "B")):
    return SimpleNamespace(
        entities_written=3, relations_written=1,
        merged=list(merged), queued=list(queued), created=list(created),
        entity_ids={})


def test_from_pipeline_passed():
    r = RunRecorder.from_pipeline(
        document_id="d1", reviewed=_reviewed(True, iters=1, span_ratio=1.0),
        ingest_report=_ingest(merged=("X",), queued=()),
        input_tokens=10, output_tokens=20, latency_ms=500, model_name="m")
    assert r.n_entities == 2 and r.n_relations == 1
    assert r.review_passed is True
    assert r.review_iters == 1
    assert r.span_ratio == 1.0
    assert r.open_issues == 0
    assert r.n_auto_merged == 1 and r.n_queued == 0
    assert r.input_tokens == 10 and r.model_name == "m"


def test_from_pipeline_failed_counts_open_issues():
    r = RunRecorder.from_pipeline(
        document_id="d2",
        reviewed=_reviewed(False, iters=3, span_ratio=0.5, issues=["x", "y"]),
        ingest_report=_ingest())
    assert r.review_passed is False
    assert r.open_issues == 2
    assert r.review_iters == 3


def _reviewed_with_metrics(metrics):
    reviewed = _reviewed(True, iters=1, span_ratio=1.0)
    reviewed.document_metrics = metrics
    return reviewed


def test_unmeasured_phase_timings_stay_none_not_zero():
    """F2：生产非流式拿不到分段耗时，看板记录必须是 None，不能写 0。"""
    dm = DocumentMetrics(document_id="d", n_chunks=1)
    dm.add(ChunkCallMetrics(
        chunk_id="c0", attempts=1, finish_reason="stop",
        input_tokens=10, output_tokens=5, cached_input_tokens=0,
        wait_s=0.01, prefill_s=None, decode_s=None))
    r = RunRecorder.from_pipeline(
        document_id="d", reviewed=_reviewed_with_metrics(dm),
        ingest_report=_ingest())
    assert r.wait_ms == 10
    assert r.prefill_ms is None and r.decode_ms is None


def test_recorder_marks_source_and_failure_metrics():
    """F7/F5：来源可区分；失败记录也带截断信号与分块明细。"""
    dm = DocumentMetrics(document_id="d", n_chunks=1)
    dm.add(ChunkCallMetrics(
        chunk_id="c0", attempts=2, finish_reason="length",
        input_tokens=10, output_tokens=20, cached_input_tokens=0,
        wait_s=0.0, prefill_s=None, decode_s=None,
        attempt_finish_reasons=["length", "length"],
        truncated_attempts=2, error="校验失败"))
    ok = RunRecorder.from_pipeline(
        document_id="ok", reviewed=_reviewed_with_metrics(dm),
        ingest_report=_ingest(), source="api")
    assert ok.source == "api"
    failed = RunRecorder.from_failure(
        document_id="bad", document_metrics=dm, error="永久失败",
        source="job")
    assert failed.source == "job"
    assert failed.review_passed is False
    assert failed.truncated_chunks == 1
    assert failed.chunk_metrics_json[0]["truncated"] is True


def test_from_failure_with_empty_metrics_is_safe():
    """失败发生在任何块度量之前时，空 DocumentMetrics 不能让看板组装炸。"""
    r = RunRecorder.from_failure(
        document_id="bad", document_metrics=DocumentMetrics(
            document_id="bad", n_chunks=2), error="模型超时")
    assert r.chunk_calls == 0
    assert r.wait_ms == 0
    assert r.prefill_ms is None and r.decode_ms is None
    assert r.chunk_metrics_json == []


def test_from_failure_aggregates_tokens_from_failed_chunks():
    """S3：永久失败行的看板 token 成本不能丢。

    pipeline 已把失败块烧掉的 token 累进 document_metrics.chunks，但
    from_failure 之前把 input/output/cached 全写成 0，成本看板系统性低估。
    """
    dm = DocumentMetrics(document_id="bad", n_chunks=2)
    dm.add(ChunkCallMetrics(
        chunk_id="bad#c0000", attempts=1, finish_reason="stop",
        input_tokens=100, output_tokens=20, cached_input_tokens=40,
        wait_s=0.0, prefill_s=None, decode_s=None))
    dm.add(ChunkCallMetrics(
        chunk_id="bad#c0001", attempts=2, finish_reason="length",
        input_tokens=200, output_tokens=400, cached_input_tokens=60,
        wait_s=0.0, prefill_s=None, decode_s=None,
        attempt_finish_reasons=["length", "length"], truncated_attempts=2,
        error="校验失败"))
    r = RunRecorder.from_failure(
        document_id="bad", document_metrics=dm, error="永久失败")
    assert r.input_tokens == 300
    assert r.output_tokens == 420
    assert r.cached_input_tokens == 100
    # 显式传入的 usage（短文档失败时异常上没有 document_metrics）优先
    r2 = RunRecorder.from_failure(
        document_id="short", document_metrics=None, error="失败",
        input_tokens=7, output_tokens=3, cached_input_tokens=2)
    assert (r2.input_tokens, r2.output_tokens, r2.cached_input_tokens) == (7, 3, 2)


def test_run_recorder_maps_non_streaming_wall_clock_latency():
    """S1：非流式可测的 chunk_latency_ms 单独一列，prefill/decode 仍为 None。"""
    dm = DocumentMetrics(document_id="d", n_chunks=1)
    dm.add(ChunkCallMetrics(
        chunk_id="c0", attempts=1, finish_reason="stop",
        input_tokens=10, output_tokens=5, cached_input_tokens=0,
        wait_s=0.0, prefill_s=None, decode_s=None, latency_s=0.4))
    r = RunRecorder.from_pipeline(
        document_id="d", reviewed=_reviewed_with_metrics(dm),
        ingest_report=_ingest())
    assert r.chunk_latency_ms == 400
    assert r.prefill_ms is None and r.decode_ms is None
    failed = RunRecorder.from_failure(
        document_id="d", document_metrics=dm, error="x")
    assert failed.chunk_latency_ms == 400
