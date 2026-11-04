"""工单 04 第 6 轮评审发现的可离线钉住行为（red → green）。

对应评审编号：

* F1 gate 必须检查验收 #5 的上下文余量；能力缺失时 fail closed；
* F2 未提供 rounds 时 ``*_worst`` 不得退化为两档各自最差值相减，直接报错；
* F3 strict 实体 F1 的 mean/worst 落盘并进 gate；
* F4 raw 验收口径也要有按实体类型的 P/R（``raw_entity_by_type``）；
* N3 ``_overlap_stats`` 的分母字段名必须说明它是“比例最小那篇”的分母；
* N4 删除只写不读的 ``overlap_pairs_possible_min`` / ``overlap_pairs_ratio_min``；
* N5 ``gate_thresholds`` 复用 budget 模块的阈值常量，不再写死 0.5；
* N1/N2 文档里的复现命令与单位口径修正。
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
from moray.evaluation.stress.budget import (
    MAX_ENTITY_F1_DROP_PT,
    MAX_RELATION_PRECISION_DROP_PT,
    evaluate_budget_gate,
)
from moray.schema import Entity, ExtractionResult

_REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# F1：上下文余量必须进 gate；能力缺失 fail closed
# ---------------------------------------------------------------------------
def test_gate_checks_context_headroom():
    gate = evaluate_gate(gate_metrics(), passing_candidate())
    assert gate["checks"]["context_within_window"] is True

    too_big = passing_candidate(max_context_input_tokens=1_048_576)
    gate = evaluate_gate(gate_metrics(), too_big)
    assert gate["checks"]["context_within_window"] is False
    assert gate["passed"] is False


def test_gate_fails_closed_when_context_capability_is_missing():
    candidate = passing_candidate()
    candidate.pop("context_window")
    gate = evaluate_gate(gate_metrics(), candidate)
    assert gate["checks"]["context_within_window"] is False
    assert gate["passed"] is False


def test_gate_fails_closed_when_context_capability_is_none():
    candidate = passing_candidate(context_window=None)
    gate = evaluate_gate(gate_metrics(), candidate)
    assert gate["checks"]["context_within_window"] is False
    assert gate["passed"] is False


# ---------------------------------------------------------------------------
# F2：没有 rounds 就不允许算 worst delta
# ---------------------------------------------------------------------------
def test_gate_rejects_worst_delta_without_rounds():
    with pytest.raises(ValueError, match="rounds"):
        evaluate_budget_gate(gate_metrics(), passing_candidate())


def test_gate_rejects_explicit_worst_delta_without_rounds():
    with pytest.raises(ValueError, match="rounds"):
        evaluate_budget_gate(
            gate_metrics(),
            passing_candidate(entity_macro_f1_worst_delta_pt=-0.4))


# ---------------------------------------------------------------------------
# F3：strict 实体 F1 的 mean/worst 都进 gate
# ---------------------------------------------------------------------------
def test_gate_requires_and_checks_strict_entity_fields():
    baseline = gate_metrics()
    candidate = passing_candidate()
    baseline.pop("entity_strict_f1_worst")
    with pytest.raises(ValueError, match="entity_strict_f1_worst"):
        # 缺字段检查先于 rounds 校验；这里故意不传 rounds。
        evaluate_budget_gate(baseline, candidate)

    gate = evaluate_gate(
        gate_metrics(),
        passing_candidate(
            entity_strict_f1_mean=0.9573,   # -0.51pt
            entity_strict_f1_worst=0.9549,  # -0.51pt
        ),
    )
    assert gate["checks"]["entity_strict_mean_within_0_5pt"] is False
    assert gate["checks"]["entity_strict_worst_within_0_5pt"] is False


def test_shared_failure_cases_cover_strict_and_context():
    failed = {case[1] for case in GATE_FAILURE_CASES}
    assert {"entity_strict_mean_within_0_5pt",
            "entity_strict_worst_within_0_5pt",
            "context_within_window"} <= failed


# ---------------------------------------------------------------------------
# F4：raw 按类型 P/R
# ---------------------------------------------------------------------------
def test_raw_entity_by_type_aggregates_from_persisted_rows():
    from tools.chunk_budget_ab import _raw_entity_by_type_from_rows

    class _Case:
        gold = ExtractionResult(entities=[
            Entity(name="A2024-942", type="EVENT"),
            Entity(name="张三", type="PER"),
        ])

    rows = [{
        "predicted_entities": [
            # 原始验收口径：EVENT 名带类型前缀，匹配不上 gold。
            {"type": "EVENT", "name": "案件编号 A2024-942"},
            {"type": "PER", "name": "张三"},
        ],
    }]
    out = _raw_entity_by_type_from_rows(rows, [_Case()])
    assert out["EVENT"] == PRF(tp=0, fp=1, fn=1).as_dict()
    assert out["PER"] == PRF(tp=1, fp=0, fn=0).as_dict()


def test_from_json_regenerates_gate_with_new_context_and_strict_checks(tmp_path):
    out = tmp_path / "regen.json"
    payload = evidence_payload()
    source = tmp_path / "source.json"
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
    regen = json.loads(out.read_text(encoding="utf-8"))
    assert "context_within_window" in regen["gate"]["checks"]
    assert "entity_strict_mean_within_0_5pt" in regen["gate"]["checks"]
    # JSON 里没有 corpus 时不能静默丢字段：rows 已持久化就照常聚合。
    assert regen["budgets"]["3500"]["raw_entity_by_type"][
        "EVENT"]["precision"] == 0.0


# ---------------------------------------------------------------------------
# N3/N4：overlap 字段命名与死字段
# ---------------------------------------------------------------------------
def test_overlap_stats_names_denominator_as_min_ratio_document():
    from tools.chunk_budget_ab import _overlap_stats

    stats = _overlap_stats({"rounds": [{"cases": [
        {"n_chunks": 9, "overlap_pairs": 7},
        {"n_chunks": 3, "overlap_pairs": 0},
    ]}]})
    assert stats["min_overlap_pairs"] == 0
    assert stats["min_ratio_document_possible_pairs"] == 2
    assert "min_possible_pairs" not in stats


def test_enrichment_does_not_write_dead_overlap_fields():
    from tools.chunk_budget_ab import _enrich_result_derived

    result = {"rounds": [{"cases": [
        {"n_chunks": 9, "overlap_pairs": 7}]}]}
    _enrich_result_derived(result)
    assert "overlap_pairs_possible_min" not in result
    assert "overlap_pairs_ratio_min" not in result


# ---------------------------------------------------------------------------
# N5：gate 阈值复用常量
# ---------------------------------------------------------------------------
def test_gate_thresholds_reuse_budget_constants(monkeypatch):
    import tools.chunk_budget_ab as tool

    monkeypatch.setattr(tool, "MAX_ENTITY_F1_DROP_PT", 0.25)
    monkeypatch.setattr(tool, "MAX_RELATION_PRECISION_DROP_PT", 0.25)
    thresholds = tool.gate_thresholds()
    assert thresholds["entity_macro_f1_max_drop_pt"] == 0.25
    assert thresholds["relation_covered_precision_max_drop_pt"] == 0.25
    # 默认常量本身仍与工单的 0.5pt 一致。
    assert MAX_ENTITY_F1_DROP_PT == 0.5
    assert MAX_RELATION_PRECISION_DROP_PT == 0.5


# ---------------------------------------------------------------------------
# N1/N2：证据文档修正
# ---------------------------------------------------------------------------
def test_r5_fix_evidence_uses_existing_venv_path():
    text = (_REPO_ROOT / "docs/evidence/04-r5-fix-evidence.md").read_text(
        encoding="utf-8")
    assert "src .venv/bin/python" not in text
    assert "../moray/.venv/bin/python" in text


def test_report_separates_estimated_units_from_real_tokens():
    text = (_REPO_ROOT / "docs/evidence/04-budget-ab.md").read_text(
        encoding="utf-8")
    assert "最大块估算 3,597 units 来自压力语料" in text
    assert "真实 tokenizer 计数 4,448 tokens 来自真实长文" in text
