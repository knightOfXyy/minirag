"""RunRecorder.from_pipeline 组装逻辑的离线单测。"""

from types import SimpleNamespace

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
        input_tokens=10, output_tokens=20, latency_ms=500, model_name="m",
        cache_hits=3)
    assert r.n_entities == 2 and r.n_relations == 1
    assert r.review_passed is True
    assert r.review_iters == 1
    assert r.span_ratio == 1.0
    assert r.open_issues == 0
    assert r.n_auto_merged == 1 and r.n_queued == 0
    assert r.input_tokens == 10 and r.model_name == "m"
    assert r.cache_hits == 3


def test_from_pipeline_failed_counts_open_issues():
    r = RunRecorder.from_pipeline(
        document_id="d2",
        reviewed=_reviewed(False, iters=3, span_ratio=0.5, issues=["x", "y"]),
        ingest_report=_ingest())
    assert r.review_passed is False
    assert r.open_issues == 2
    assert r.review_iters == 3
