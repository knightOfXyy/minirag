"""工单 04 第 4 轮评审发现的可离线钉住行为（red → green）。

对应评审编号：
* B1  gate 实体轴必须同时要求 raw / normalized 两套口径，任一超 0.5pt 即 fail；
* S1  evidence JSON 的 thresholds 必须写“全量关系 F1 参与放行”，不能与实现相反；
* N3  gate 不信任 1500-vs-1500，也不无条件信任调用方给的 explicit worst delta；
* N6  关系端点回退只对文档里实际出现的声明类型剥前缀，避免未来类型假 TP；
* S2  DEPLOYMENT 的切换步骤必须同时改 config 与 Chunker 两处默认（文档级断言）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from gate_fixtures import GATE_FAILURE_CASES, gate_metrics, passing_candidate
from moray.evaluation.stress import evaluate_case
from moray.evaluation.stress.budget import evaluate_budget_gate
from moray.schema import Entity, ExtractionResult, Relation

_REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# B1：实体轴必须双口径，raw 不得被归一化辅助口径替代
# ---------------------------------------------------------------------------
def test_gate_includes_raw_and_normalized_entity_checks():
    gate = evaluate_budget_gate(gate_metrics(), passing_candidate())
    for name in (
        "entity_raw_mean_within_0_5pt",
        "entity_raw_worst_within_0_5pt",
        "entity_normalized_mean_within_0_5pt",
        "entity_normalized_worst_within_0_5pt",
    ):
        assert gate["checks"][name] is True
    # 两轴都显式标注，读证据的人不会再把辅助口径当验收口径。
    assert "raw" in gate["entity_caliber"]["acceptance"]
    assert "normalized" in gate["entity_caliber"]["auxiliary"]
    assert gate["passed"] is True


def test_gate_fails_when_raw_entity_drops_even_if_normalized_passes():
    candidate = passing_candidate(
        # 归一化辅助口径全绿（+），但原始验收口径掉 3pt。
        entity_macro_f1_mean=0.9300,
        entity_macro_f1_worst=0.9100,
        raw_entity_macro_f1_mean=0.8700,
        raw_entity_macro_f1_worst=0.8500,
    )
    gate = evaluate_budget_gate(gate_metrics(), candidate)
    assert gate["checks"]["entity_normalized_mean_within_0_5pt"] is True
    assert gate["checks"]["entity_raw_mean_within_0_5pt"] is False
    assert gate["checks"]["entity_raw_worst_within_0_5pt"] is False
    assert gate["passed"] is False


def test_gate_requires_raw_entity_fields():
    baseline = gate_metrics()
    candidate = passing_candidate()
    baseline.pop("raw_entity_macro_f1_mean")
    with pytest.raises(ValueError, match="raw_entity_macro_f1_mean"):
        evaluate_budget_gate(baseline, candidate)


# ---------------------------------------------------------------------------
# N3：gate 输入自证
# ---------------------------------------------------------------------------
def test_gate_rejects_same_budget_on_both_sides():
    gate = evaluate_budget_gate(
        gate_metrics(), passing_candidate(budget=1500))
    assert gate["checks"]["budgets_differ"] is False
    assert gate["passed"] is False


def test_gate_recomputes_explicit_worst_delta_from_rounds():
    baseline_rounds = [{"entity_macro_f1": 0.9000, "raw_entity_macro_f1": 0.9000,
                        "relation_f1": 0.5000, "raw_relation_f1": 0.5000,
                        "relation_covered_recall": 0.8000,
                        "relation_covered_precision": 0.9000}]
    candidate_rounds = [{"entity_macro_f1": 0.8800, "raw_entity_macro_f1": 0.8500,
                         "relation_f1": 0.4990, "raw_relation_f1": 0.4900,
                         "relation_covered_recall": 0.7990,
                         "relation_covered_precision": 0.8990}]
    with pytest.raises(ValueError, match="relation_f1_worst_delta_pt"):
        evaluate_budget_gate(
            gate_metrics(), passing_candidate(relation_f1_worst_delta_pt=0.0),
            baseline_rounds=baseline_rounds, candidate_rounds=candidate_rounds)


def test_gate_uses_round_recomputed_worst_when_no_explicit_delta():
    baseline_rounds = [
        {"entity_macro_f1": 0.9000, "raw_entity_macro_f1": 0.9000,
         "relation_f1": 0.5000, "raw_relation_f1": 0.5000,
         "relation_covered_recall": 0.8000,
         "relation_covered_precision": 0.9000},
        {"entity_macro_f1": 0.8900, "raw_entity_macro_f1": 0.8900,
         "relation_f1": 0.4900, "raw_relation_f1": 0.4900,
         "relation_covered_recall": 0.7900,
         "relation_covered_precision": 0.8900},
    ]
    candidate_rounds = [
        {"entity_macro_f1": 0.8960, "raw_entity_macro_f1": 0.8960,
         "relation_f1": 0.5000, "raw_relation_f1": 0.5000,
         "relation_covered_recall": 0.8000,
         "relation_covered_precision": 0.8950},
        {"entity_macro_f1": 0.8760, "raw_entity_macro_f1": 0.8760,
         "relation_f1": 0.4490, "raw_relation_f1": 0.4490,
         "relation_covered_recall": 0.7500,
         "relation_covered_precision": 0.8450},
    ]
    gate = evaluate_budget_gate(
        gate_metrics(), passing_candidate(),
        baseline_rounds=baseline_rounds, candidate_rounds=candidate_rounds)
    # 第 2 轮全量关系 F1 -4.10pt；不能退回两档各自最差值相减。
    assert gate["relation_full_worst_delta_pt"] == pytest.approx(-4.1)
    assert gate["checks"]["relation_full_f1_worst_not_lower"] is False


# ---------------------------------------------------------------------------
# N6：关系端点回退只对文档中实际出现的声明类型生效
# ---------------------------------------------------------------------------
def test_relation_endpoint_fallback_does_not_strip_when_type_is_absent():
    text = "编号 X 是机构代号，王五负责对接。"
    pred = ExtractionResult(
        entities=[Entity(name="王五", type="PER")],
        relations=[Relation(head="王五", predicate="对接", tail="编号 X")],
    )
    gold = ExtractionResult(
        entities=[Entity(name="王五", type="PER")],
        relations=[Relation(head="王五", predicate="对接", tail="X")],
    )
    ev = evaluate_case("no-event", 1, pred, gold, text=text,
                       normalize_type_labels=True)
    # 文档里没有 EVENT 实体：不能把 PER/MISC 的“编号 X”也剥成“X”造假 TP。
    assert ev.report.relation.tp == 0
    assert ev.report.relation.fp == 1
    assert ev.report.relation.fn == 1


# ---------------------------------------------------------------------------
# S1：证据 JSON 的 gate 描述必须与实现一致
# ---------------------------------------------------------------------------
def test_evidence_thresholds_describe_full_relation_f1_as_gating():
    from tools.chunk_budget_ab import gate_thresholds

    thresholds = gate_thresholds()
    assert "参与放行" in thresholds["relation_full_scope"]
    assert "只列报，不参与放行" not in thresholds["relation_full_scope"]
    assert "raw" in thresholds["entity_macro_f1_note"]


# ---------------------------------------------------------------------------
# S1/B1：--from-json 可在不调 LLM 的情况下重生成 gate 段
# ---------------------------------------------------------------------------
def test_from_json_regenerates_gate_without_llm(tmp_path):
    out = tmp_path / "regen.json"
    source = _REPO_ROOT / "docs/evidence/04-budget-ab.json"
    env = {"PYTHONPATH": str(_REPO_ROOT / "src"), "ARK_API_KEY": "dummy"}
    proc = subprocess.run(
        [sys.executable, str(_REPO_ROOT / "tools/chunk_budget_ab.py"),
         "--from-json", str(source), "--json-out", str(out)],
        cwd=_REPO_ROOT, env={**env, "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(out.read_text(encoding="utf-8"))
    gate = payload["gate"]
    assert "entity_raw_mean_within_0_5pt" in gate["checks"]
    assert "entity_normalized_mean_within_0_5pt" in gate["checks"]
    assert gate["checks"]["entity_raw_mean_within_0_5pt"] is False
    assert gate["passed"] is False
    assert payload["gate_recompute"]["source_git_sha"]


# ---------------------------------------------------------------------------
# S2：切换步骤必须点名两处默认值
# ---------------------------------------------------------------------------
def test_deployment_switch_steps_name_both_default_sites():
    text = (_REPO_ROOT / "docs/DEPLOYMENT.md").read_text(encoding="utf-8")
    block = text.split("## 2c.")[1].split("## 3.")[0]
    assert "Chunker" in block
    assert "test_chunk_budget" in block


# ---------------------------------------------------------------------------
# N3：共享夹具本身要覆盖新增的两条实体 check
# ---------------------------------------------------------------------------
def test_shared_gate_cases_cover_both_entity_axes():
    failed = {case[1] for case in GATE_FAILURE_CASES}
    assert {"entity_raw_mean_within_0_5pt",
            "entity_raw_worst_within_0_5pt",
            "entity_normalized_mean_within_0_5pt",
            "entity_normalized_worst_within_0_5pt"} <= failed
