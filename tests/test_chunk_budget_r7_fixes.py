"""工单 04 第 7 轮评审发现的可离线钉住行为（red → green）。

对应评审编号（r7）：

* S1 ``context_within_window`` 在 provider usage 缺失（0/0 tokens）时不能再
  返回 true，必须 fail closed；
* S2 ``real_doc_coverage`` 不能只写进 gate 报告，必须进 ``checks``：空
  real-doc 批次或没有超过候选预算的长文时，验收 #4 不允许空转；
* N1 ``_raw_entity_by_type_from_rows`` 不再用 ``index % len(cases)`` 静默
  重建；rows 与 cases 不能按轮次对齐时直接报错；
* N2 报告里 strict mean 的 ``-0.54``（gate）与 ``-0.534``（逐轮对齐均值）
  必须显式说明口径，不能在同一份报告里出现互不解释的 ``-0.54`` / ``-0.53``；
* N3 ``overlap_observed`` 不能把单块文档的 0 对重叠误判成失败；单块文档
  不参与最小值，但“没有任何多块文档”仍 fail closed。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from gate_fixtures import (
    GATE_FAILURE_CASES, evaluate_gate, evidence_payload, gate_metrics,
    passing_candidate,
)
from moray.evaluation.metrics import PRF

_REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# S1：0/0 token 不能再算“在上下文内”
# ---------------------------------------------------------------------------
def test_gate_fails_closed_when_token_counts_are_zero():
    gate = evaluate_gate(
        gate_metrics(),
        passing_candidate(
            max_context_input_tokens=0,
            max_context_output_tokens=0,
        ),
    )
    assert gate["checks"]["context_within_window"] is False
    assert gate["passed"] is False


def test_gate_fails_closed_when_output_token_count_is_missing():
    gate = evaluate_gate(
        gate_metrics(),
        passing_candidate(max_context_output_tokens=0),
    )
    assert gate["checks"]["context_within_window"] is False
    assert gate["passed"] is False


# ---------------------------------------------------------------------------
# S2：real-doc 覆盖必须进 checks
# ---------------------------------------------------------------------------
def test_gate_fails_when_real_doc_batch_is_empty():
    gate = evaluate_gate(
        gate_metrics(),
        passing_candidate(
            real_doc_documents=0,
            real_doc_max_document_units=0,
        ),
    )
    assert gate["checks"]["real_doc_coverage_exercises_budget"] is False
    assert gate["passed"] is False


def test_gate_fails_when_real_doc_batch_does_not_exercise_candidate_budget():
    gate = evaluate_gate(
        gate_metrics(),
        passing_candidate(real_doc_max_document_units=341),
    )
    assert gate["checks"]["real_doc_coverage_exercises_budget"] is False
    assert gate["passed"] is False


def test_gate_passes_real_doc_check_when_long_document_is_present():
    gate = evaluate_gate(gate_metrics(), passing_candidate())
    assert gate["checks"]["real_doc_coverage_exercises_budget"] is True
    assert gate["passed"] is True


# ---------------------------------------------------------------------------
# N3：单块文档不参与 overlap 最小值；没有多块文档仍 fail closed
# ---------------------------------------------------------------------------
def test_gate_overlap_ignores_single_chunk_documents():
    gate = evaluate_gate(
        gate_metrics(),
        passing_candidate(
            overlap_pairs_min=1,
            overlap_documents_with_multiple_chunks=2,
        ),
    )
    assert gate["checks"]["overlap_observed"] is True
    assert gate["passed"] is True


def test_gate_overlap_fails_closed_when_no_multi_chunk_document():
    gate = evaluate_gate(
        gate_metrics(),
        passing_candidate(
            overlap_pairs_min=0,
            overlap_documents_with_multiple_chunks=0,
        ),
    )
    assert gate["checks"]["overlap_observed"] is False
    assert gate["passed"] is False


def test_enrichment_excludes_single_chunk_docs_from_overlap_min():
    from tools.chunk_budget_ab import _enrich_result_derived

    result = {"failed_chunks_max": 0, "rounds": [{"cases": [
        {"n_chunks": 1, "overlap_pairs": 0, "overlap_pairs_possible": 0},
        {"n_chunks": 3, "overlap_pairs": 2, "overlap_pairs_possible": 2},
    ]}]}
    _enrich_result_derived(result)
    assert result["overlap_pairs_min"] == 2
    assert result["overlap_documents_with_multiple_chunks"] == 1


# ---------------------------------------------------------------------------
# N1：raw_entity_by_type 重建必须按轮次对齐，不能 modulo 猜
# ---------------------------------------------------------------------------
def test_raw_entity_by_type_rebuild_rejects_misaligned_rows():
    from tools.chunk_budget_ab import _raw_entity_by_type_from_rows

    class _Case:
        gold = None

    rows = [{
        "predicted_entities": [{"type": "PER", "name": "张三"}],
    }]
    # 2 个 case 只给了 1 行：旧实现会 modulo 映射到第 1 个 case，
    # 这里必须显式报错，不能产出看似合法但错配的数字。
    with pytest.raises(ValueError, match="对齐|轮次|cases"):
        _raw_entity_by_type_from_rows(rows, [_Case(), _Case()])


def test_raw_entity_by_type_rebuild_maps_aligned_rows_in_order():
    from moray.schema import Entity, ExtractionResult
    from tools.chunk_budget_ab import _raw_entity_by_type_from_rows

    class _Case:
        def __init__(self, gold):
            self.gold = gold

    cases = [
        _Case(ExtractionResult(entities=[
            Entity(name="张三", type="PER")])),
        _Case(ExtractionResult(entities=[
            Entity(name="李四", type="PER")])),
    ]
    rows = [
        {"predicted_entities": [{"type": "PER", "name": "张三"}]},
        {"predicted_entities": [{"type": "PER", "name": "李四"}]},
    ]
    out = _raw_entity_by_type_from_rows(rows, cases)
    assert out["PER"] == PRF(tp=2, fp=0, fn=0).as_dict()


# ---------------------------------------------------------------------------
# N2：-0.54 / -0.534 口径必须解释
# ---------------------------------------------------------------------------
def test_report_explains_strict_delta_rounding_caliber():
    text = (_REPO_ROOT / "docs/evidence/04-budget-ab.md").read_text(
        encoding="utf-8")
    assert "−0.534" in text
    assert "逐轮对齐" in text
    # gate 用四位存储均值相减得到 −0.54，必须与 CI 表的 −0.53 并列解释。
    assert "−0.54" in text and "−0.53" in text


# ---------------------------------------------------------------------------
# from-json：新 check 必须由工具重生成，空 real-doc 批次要能判失败
# ---------------------------------------------------------------------------
def _run_inline_from_json(payload, tmp_path):
    source = tmp_path / "source.json"
    out = tmp_path / "regen.json"
    source.write_text(json.dumps(payload), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(_REPO_ROOT / "tools/chunk_budget_ab.py"),
         "--from-json", str(source), "--json-out", str(out)],
        cwd=_REPO_ROOT,
        env={"PYTHONPATH": str(_REPO_ROOT / "src"),
             "ARK_API_KEY": "dummy", "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(out.read_text(encoding="utf-8"))


def test_from_json_regenerates_real_doc_and_overlap_checks(tmp_path):
    regen = _run_inline_from_json(evidence_payload(), tmp_path)
    checks = regen["gate"]["checks"]
    assert checks["real_doc_coverage_exercises_budget"] is True
    assert checks["overlap_observed"] is True
    assert regen["gate"]["real_doc_coverage"]["exercises_candidate_budget"] is True
    assert regen["budgets"]["3500"]["overlap_documents_with_multiple_chunks"] == 1


def test_from_json_empty_real_doc_batch_fails_gate(tmp_path):
    payload = evidence_payload()
    payload["real_docs"]["3500"]["rows"] = []
    regen = _run_inline_from_json(payload, tmp_path)
    assert regen["gate"]["checks"][
        "real_doc_coverage_exercises_budget"] is False
    assert regen["gate"]["passed"] is False


def test_shared_failure_cases_cover_new_r7_checks():
    failed = {case[1] for case in GATE_FAILURE_CASES}
    assert {
        "real_doc_coverage_exercises_budget",
        "overlap_observed",
    } <= failed
