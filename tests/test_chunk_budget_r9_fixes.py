"""工单 04 第 9 轮评审发现的可离线钉住行为（red → green）。

对应评审编号（r9）：

* S1 gate 只检查候选档 ``failed_chunks``；基线档若已有失败块（实测
  ``failed_chunks=5``），相对 F1 判据的分母/口径就不再可比，gate 仍会
  ``passed=true``。基线档失败块同样必须为 0。
* S2 ``--from-json`` 重建失败块时用 ``row.get("failed_chunks", 0)``，逐篇行
  缺字段会被静默当成 0（实测删光失败证据仍 ``passed=true``），与注释
  “缺失直接报错”相反；压力语料与真实文档两处都要严格取值。
* N1 overlap 聚合仍有两份实现：``_enrich_result_derived`` 自己算一遍，
  live 走 ``_overlap_aggregate``；收敛到只有后者一份。
* N2 空真实文档批次 ``_run_real_docs([])`` 不返回 ``failed_chunks_total``，
  live 的 ``_gate_input`` 直接 ``real["failed_chunks_total"]`` 会 KeyError。
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from gate_fixtures import (
    evaluate_gate, evidence_payload, gate_metrics, passing_candidate,
)
from moray.evaluation.stress.budget import evaluate_budget_gate

_REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# S1：基线档失败块也必须为 0，否则相对 F1 判据不可比
# ---------------------------------------------------------------------------
def test_gate_fails_when_baseline_has_failed_chunks():
    gate = evaluate_gate(
        gate_metrics(failed_chunks=5),
        passing_candidate(failed_chunks=0),
    )
    assert gate["checks"]["baseline_failed_chunks_zero"] is False
    assert gate["checks"]["failed_chunks_zero"] is True
    assert gate["passed"] is False


def test_gate_requires_failed_chunks_on_baseline_too():
    baseline = gate_metrics()
    baseline.pop("failed_chunks")
    with pytest.raises(ValueError, match="failed_chunks"):
        evaluate_budget_gate(baseline, passing_candidate())


# ---------------------------------------------------------------------------
# S2：--from-json 重建失败块时，逐篇行缺字段必须报错而不是按 0
# ---------------------------------------------------------------------------
def _run_from_json(payload, tmp_path):
    source = tmp_path / "source.json"
    out = tmp_path / "regen.json"
    source.write_text(json.dumps(payload), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(_REPO_ROOT / "tools/chunk_budget_ab.py"),
         "--from-json", str(source), "--json-out", str(out)],
        cwd=_REPO_ROOT,
        env={"PYTHONPATH": str(_REPO_ROOT / "src"),
             "ARK_API_KEY": "dummy", "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True, timeout=120,
    )


def test_from_json_rejects_missing_failed_chunks_in_stress_rows(tmp_path):
    payload = evidence_payload()
    payload["budgets"]["3500"].pop("failed_chunks_max")
    for rnd in payload["budgets"]["3500"]["rounds"]:
        for row in rnd["cases"]:
            row.pop("failed_chunks")
    proc = _run_from_json(payload, tmp_path)
    assert proc.returncode != 0
    assert "failed_chunks" in proc.stderr


def test_from_json_rejects_missing_failed_chunks_in_real_rows(tmp_path):
    payload = evidence_payload()
    payload["real_docs"]["3500"].pop("failed_chunks_total")
    for row in payload["real_docs"]["3500"]["rows"]:
        row.pop("failed_chunks", None)
    proc = _run_from_json(payload, tmp_path)
    assert proc.returncode != 0
    assert "failed_chunks" in proc.stderr


# ---------------------------------------------------------------------------
# N1：overlap 聚合只有 _overlap_aggregate 一份实现
# ---------------------------------------------------------------------------
def test_enrichment_delegates_overlap_aggregation(monkeypatch):
    from tools import chunk_budget_ab

    calls = []

    def fake_aggregate(rounds):
        calls.append(rounds)
        return {
            "overlap_documents_with_multiple_chunks": 11,
            "overlap_pairs_min": 4,
        }

    monkeypatch.setattr(
        chunk_budget_ab, "_overlap_aggregate", fake_aggregate)
    result = {"rounds": [{"cases": []}]}
    chunk_budget_ab._enrich_result_derived(result)
    assert result["overlap_documents_with_multiple_chunks"] == 11
    assert result["overlap_pairs_min"] == 4
    assert calls


# ---------------------------------------------------------------------------
# N2：空真实文档批次也必须带 failed_chunks_total，live 不能 KeyError
# ---------------------------------------------------------------------------
def test_empty_real_docs_return_failed_chunks_total_zero():
    from tools.chunk_budget_ab import _run_real_docs

    result = asyncio.run(_run_real_docs(
        [], budget=3500, overlap=200, concurrency=1, domain=None))
    assert result["failed_chunks_total"] == 0


def test_gate_input_handles_empty_real_docs_without_keyerror():
    from tools.chunk_budget_ab import _gate_input, _run_real_docs

    real = asyncio.run(_run_real_docs(
        [], budget=3500, overlap=200, concurrency=1, domain=None))
    result = {
        "overlap_pairs_min": 0,
        "overlap_documents_with_multiple_chunks": 0,
        "chunk_calls_total_mean": 1.0,
        "truncated_chunks_max": 0,
        "failed_chunks_max": 0,
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
        result, budget=3500, real=real, overlap_units=200)
    assert gate_input["failed_chunks"] == 0


# ---------------------------------------------------------------------------
# 报告必须记录本轮处置
# ---------------------------------------------------------------------------
def test_report_documents_r9_fixes():
    text = (_REPO_ROOT / "docs/evidence/04-budget-ab.md").read_text(
        encoding="utf-8")
    assert "评审 r9 修复" in text
    assert "baseline_failed_chunks_zero" in text
    assert "缺失直接报错" in text
    assert "_overlap_aggregate" in text
