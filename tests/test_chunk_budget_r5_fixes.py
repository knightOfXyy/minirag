"""工单 04 第 5 轮评审发现的可离线钉住行为（red → green）。

对应评审编号：

* S1 关系轴的 gate 必须同时要求原始（未归一化）与归一化两套全量 F1，
  原始口径不能只列报；3500 档原始关系 F1 实际 mean −10.88pt / worst −19.28pt；
* S2 “相邻块逐对重叠”的验收表述与压力语料不符：实际 1500 8/25、3500 7/8，
  长句会使 overlap seed 为空；gate 的 ``overlap_observed`` 只是 >0 冒烟检查；
* N1 ``metrics.py`` 的 docstring 必须描述“只对文档内实际出现的类型回退”，
  不再声称对前缀表全表回退；
* N2 真实文档覆盖说明必须按每一档自己的预算计算，不能两档共用候选预算；
* N3 报告不能再声称 JSON 含“逐块明细”（实际是逐篇聚合 + 单块极值）；
* N4 两份手写的 17 键 gate 输入收敛到 ``_gate_input``，新增口径只改一处。
"""

from __future__ import annotations

import inspect
import json
import subprocess
import sys
from pathlib import Path

import pytest

from gate_fixtures import (
    GATE_FAILURE_CASES, evaluate_gate, evidence_payload, gate_metrics,
    passing_candidate,
)
from moray.evaluation.stress.budget import evaluate_budget_gate

_REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# S1：关系轴必须 raw + normalized 双口径，raw 不得只列报
# ---------------------------------------------------------------------------
def test_gate_requires_raw_relation_fields():
    baseline = gate_metrics()
    candidate = passing_candidate()
    baseline.pop("raw_relation_f1_mean")
    with pytest.raises(ValueError, match="raw_relation_f1_mean"):
        evaluate_budget_gate(baseline, candidate)


def test_gate_fails_when_raw_relation_drops_even_if_normalized_passes():
    candidate = passing_candidate(
        # 归一化辅助口径持平/更好，但原始验收口径全量关系 F1 掉 2pt。
        relation_f1_mean=0.5200,
        relation_f1_worst=0.4700,
        raw_relation_f1_mean=0.4800,
        raw_relation_f1_worst=0.4300,
    )
    gate = evaluate_gate(gate_metrics(), candidate)
    assert gate["checks"]["relation_full_f1_mean_not_lower"] is True
    assert gate["checks"]["relation_full_f1_worst_not_lower"] is True
    assert gate["checks"]["relation_raw_full_f1_mean_not_lower"] is False
    assert gate["checks"]["relation_raw_full_f1_worst_not_lower"] is False
    assert gate["relation_raw_full_mean_delta_pt"] == pytest.approx(-2.0)
    assert gate["relation_raw_full_worst_delta_pt"] == pytest.approx(-2.0)
    assert gate["passed"] is False


def test_gate_recomputes_raw_relation_worst_delta_from_rounds():
    baseline_rounds = [
        {"entity_macro_f1": 0.9000, "raw_entity_macro_f1": 0.9000,
         "entity_strict_f1": 0.9000, "relation_f1": 0.5000, "raw_relation_f1": 0.5000,
         "relation_covered_recall": 0.8000,
         "relation_covered_precision": 0.9000},
        {"entity_macro_f1": 0.9000, "raw_entity_macro_f1": 0.9000,
         "entity_strict_f1": 0.9000, "relation_f1": 0.5000, "raw_relation_f1": 0.5000,
         "relation_covered_recall": 0.8000,
         "relation_covered_precision": 0.9000},
    ]
    candidate_rounds = [
        {"entity_macro_f1": 0.8960, "raw_entity_macro_f1": 0.8960,
         "entity_strict_f1": 0.8960, "relation_f1": 0.5000, "raw_relation_f1": 0.4900,
         "relation_covered_recall": 0.8000,
         "relation_covered_precision": 0.8950},
        {"entity_macro_f1": 0.8960, "raw_entity_macro_f1": 0.8960,
         "entity_strict_f1": 0.8960, "relation_f1": 0.5000, "raw_relation_f1": 0.4800,
         "relation_covered_recall": 0.8000,
         "relation_covered_precision": 0.8950},
    ]
    gate = evaluate_gate(
        gate_metrics(), passing_candidate(),
        baseline_rounds=baseline_rounds, candidate_rounds=candidate_rounds)
    assert gate["relation_raw_full_worst_delta_pt"] == pytest.approx(-2.0)
    assert gate["checks"]["relation_raw_full_f1_worst_not_lower"] is False


def test_evidence_thresholds_describe_raw_relation_gate():
    from tools.chunk_budget_ab import gate_thresholds

    thresholds = gate_thresholds()
    scope = thresholds["relation_full_scope"]
    assert "raw" in scope and "normalized" in scope
    assert "参与放行" in scope


# ---------------------------------------------------------------------------
# S2：overlap 证据要给出实际对数/可重叠对数，不能宣称逐对
# ---------------------------------------------------------------------------
def test_from_json_reports_overlap_pairs_over_possible(tmp_path):
    # 评审 r6 N6：不再从 2MB 生成证据里读死数字，改用内联小 payload。
    regen = _run_inline_from_json(
        evidence_payload(overlap={1500: (8, 25), 3500: (7, 8)}), tmp_path)
    observed = regen["overlap_observed"]
    assert observed["baseline"]["min_overlap_pairs"] == 8
    assert observed["baseline"]["min_ratio_document_possible_pairs"] == 25
    assert observed["candidate"]["min_overlap_pairs"] == 7
    assert observed["candidate"]["min_ratio_document_possible_pairs"] == 8
    assert "not every" in observed["note"] or "不是每对" in observed["note"]
    assert "smoke" in observed["gate_check"] or "冒烟" in observed["gate_check"]


# ---------------------------------------------------------------------------
# S1 + N2：--from-json 必须无 LLM 重生成新口径与逐档覆盖说明
# ---------------------------------------------------------------------------
def test_from_json_regenerates_raw_relation_gate_and_per_budget_coverage(
        tmp_path):
    regen = _run_inline_from_json(
        evidence_payload(candidate_overrides={
            "raw_relation_f1_mean": 0.4800,
            "raw_relation_f1_worst": 0.4300,
        }), tmp_path)
    gate = regen["gate"]
    assert gate["checks"]["relation_raw_full_f1_mean_not_lower"] is False
    assert gate["checks"]["relation_raw_full_f1_worst_not_lower"] is False
    assert gate["relation_raw_full_mean_delta_pt"] == pytest.approx(
        -2.0, abs=0.01)
    assert gate["relation_raw_full_worst_delta_pt"] == pytest.approx(
        -2.0, abs=0.01)
    assert regen["budgets"]["3500"]["raw_relation_f1_mean"] == pytest.approx(
        0.48, abs=0.001)
    assert regen["real_doc_coverage"]["1500"]["candidate_budget"] == 1500
    assert regen["real_doc_coverage"]["3500"]["candidate_budget"] == 3500
    assert gate["passed"] is False


def _run_inline_from_json(payload: dict, tmp_path: Path) -> dict:
    source = tmp_path / "source.json"
    out = tmp_path / "regen.json"
    source.write_text(json.dumps(payload), encoding="utf-8")
    env = {"PYTHONPATH": str(_REPO_ROOT / "src"), "ARK_API_KEY": "dummy",
           "PATH": "/usr/bin:/bin"}
    proc = subprocess.run(
        [sys.executable, str(_REPO_ROOT / "tools/chunk_budget_ab.py"),
         "--from-json", str(source), "--json-out", str(out)],
        cwd=_REPO_ROOT, env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(out.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# N1：过期 docstring
# ---------------------------------------------------------------------------
def test_metrics_docstring_does_not_claim_full_table_fallback():
    from moray.evaluation.stress.metrics import apply_type_label_aliases

    doc = inspect.getdoc(apply_type_label_aliases) or ""
    assert "全表回退" not in doc
    assert "实际出现" in doc


# ---------------------------------------------------------------------------
# N3：报告措辞
# ---------------------------------------------------------------------------
def test_report_does_not_claim_per_chunk_details_or_pairwise_overlap():
    text = (_REPO_ROOT / "docs/evidence/04-budget-ab.md").read_text(
        encoding="utf-8")
    assert "逐块明细" not in text
    assert "相邻块逐对重叠 0<x≤200" not in text
    assert "8/25" in text and "7/8" in text


# ---------------------------------------------------------------------------
# N4：gate 输入只写一份
# ---------------------------------------------------------------------------
def test_gate_input_helper_fills_all_required_keys_from_one_place():
    from tools.chunk_budget_ab import _gate_input
    from moray.evaluation.stress.budget import _REQUIRED_GATE_KEYS

    result = {
        "budget": 1500,
        "overlap_units": 200,
        "overlap_pairs_min": 8,
        "chunk_calls_total_mean": 26.0,
        "truncated_chunks_max": 0,
        "entity_macro_f1_mean": 0.9,
        "entity_macro_f1_worst": 0.9,
        "raw_entity_macro_f1_mean": 0.9,
        "raw_entity_macro_f1_worst": 0.9,
        "entity_strict_f1_mean": 0.96,
        "entity_strict_f1_worst": 0.96,
        "relation_covered_recall_mean": 0.8,
        "relation_covered_recall_worst": 0.8,
        "relation_covered_precision_mean": 0.9,
        "relation_covered_precision_worst": 0.9,
        "relation_f1_mean": 0.7,
        "relation_f1_worst": 0.7,
        "raw_relation_f1_mean": 0.7,
        "raw_relation_f1_worst": 0.7,
    }
    gate_input = _gate_input(
        result, budget=1500, real={"truncated_chunks_total": 0},
        overlap_units=200)
    assert set(_REQUIRED_GATE_KEYS) <= set(gate_input)
    assert gate_input["truncated_chunks"] == 0


# ---------------------------------------------------------------------------
# 共享夹具必须覆盖新增的两条 raw relation check
# ---------------------------------------------------------------------------
def test_shared_gate_cases_cover_raw_relation_checks():
    failed = {case[1] for case in GATE_FAILURE_CASES}
    assert {"relation_raw_full_f1_mean_not_lower",
            "relation_raw_full_f1_worst_not_lower"} <= failed
