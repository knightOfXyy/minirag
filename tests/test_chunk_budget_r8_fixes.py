"""工单 04 第 8 轮评审发现的可离线钉住行为（red → green）。

对应评审编号（r8）：

* S1 真实文档批次用 ``raise_on_chunk_error=False`` 逐块容错，失败块只写进
  ``failed_chunks_total``。失败块不会产生 finish_reason，因此 ``truncated=0``
  仍成立，real-doc 覆盖也仍可由原文 units 满足——验收 #4/#5 的证据可整批
  空转。本轮把压力语料的 ``failed_chunks_max`` 与真实文档的
  ``failed_chunks_total`` 合起来进 gate 的 ``failed_chunks_zero``；
* N1 报告头部引用仓库里不存在的提交 ``9400e6d``，且复现说明仍写“评审 r4”；
* N2 ``_overlap_stats.documents_with_multiple_chunks`` 是 5 轮重复后的
  case-run 数（15），与 ``budgets.*.overlap_documents_with_multiple_chunks``
  （每轮篇数，3）同名不同基数；改名为 ``case_runs_with_multiple_chunks``；
* N3 “可重叠对数”推算复制三份，``_run_corpus`` 少 ``n_chunks-1`` 回退；
  收敛到 ``_possible_pairs`` 一份，live 聚合也用它。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from gate_fixtures import (
    GATE_FAILURE_CASES, evaluate_gate, evidence_payload, gate_metrics,
    passing_candidate,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# S1：失败块必须为 0，否则验收 #4/#5 的证据可空转
# ---------------------------------------------------------------------------
def test_gate_fails_when_candidate_has_failed_chunks():
    gate = evaluate_gate(
        gate_metrics(),
        passing_candidate(failed_chunks=1),
    )
    assert gate["checks"]["failed_chunks_zero"] is False
    assert gate["passed"] is False


def test_gate_passes_failed_chunks_check_when_zero():
    gate = evaluate_gate(gate_metrics(), passing_candidate(failed_chunks=0))
    assert gate["checks"]["failed_chunks_zero"] is True
    assert gate["passed"] is True


# ---------------------------------------------------------------------------
# N2：overlap_observed 的多块计数是 case-run 数，必须与每轮篇数区分
# ---------------------------------------------------------------------------
def test_overlap_stats_names_case_run_count_distinctly():
    from tools.chunk_budget_ab import _overlap_stats

    case = {"n_chunks": 2, "overlap_pairs": 1, "overlap_pairs_possible": 1}
    result = {"rounds": [{"cases": [dict(case) for _ in range(3)]}
                         for _ in range(5)]}
    stats = _overlap_stats(result)
    # 5 轮 × 3 篇 = 15 个 case-run，不是 15 篇不同的多块文档。
    assert stats["case_runs_with_multiple_chunks"] == 15
    assert "documents_with_multiple_chunks" not in stats


def test_overlap_stats_empty_result_uses_new_field_name():
    from tools.chunk_budget_ab import _overlap_stats

    stats = _overlap_stats({"rounds": []})
    assert stats["case_runs_with_multiple_chunks"] == 0
    assert "documents_with_multiple_chunks" not in stats


# ---------------------------------------------------------------------------
# N3：可重叠对数只有一份实现，缺字段按 n_chunks-1 回退
# ---------------------------------------------------------------------------
def test_possible_pairs_falls_back_to_n_chunks_minus_one():
    from tools.chunk_budget_ab import _possible_pairs

    assert _possible_pairs({"n_chunks": 3}) == 2
    assert _possible_pairs({"n_chunks": 1}) == 0
    # 显式 0 是权威值（例如单块文档），不能被回退覆盖成 n_chunks-1。
    assert _possible_pairs({"n_chunks": 3, "overlap_pairs_possible": 0}) == 0
    assert _possible_pairs({"n_chunks": 3, "overlap_pairs_possible": 2}) == 2


def test_live_overlap_aggregate_uses_same_fallback_as_from_json():
    from tools.chunk_budget_ab import _overlap_aggregate

    # 旧证据/未来新增字段遗漏时没有 overlap_pairs_possible，但有 n_chunks；
    # live 聚合必须和 from-json 一样按 n_chunks-1 计入，不能当成单块文档。
    rounds = [{"cases": [{"n_chunks": 3, "overlap_pairs": 2}]}]
    aggregate = _overlap_aggregate(rounds)
    assert aggregate["overlap_documents_with_multiple_chunks"] == 1
    assert aggregate["overlap_pairs_min"] == 2


def test_enrichment_and_live_aggregate_agree_without_possible_field():
    from tools.chunk_budget_ab import _enrich_result_derived, _overlap_aggregate

    rounds = [{"cases": [
        {"n_chunks": 1, "overlap_pairs": 0},
        {"n_chunks": 3, "overlap_pairs": 2},
        {"n_chunks": 2, "overlap_pairs": 1},
    ]}]
    result = {"failed_chunks_max": 0, "rounds": rounds}
    _enrich_result_derived(result)
    aggregate = _overlap_aggregate(rounds)
    assert result["overlap_documents_with_multiple_chunks"] == 2
    assert result["overlap_pairs_min"] == 1
    assert aggregate["overlap_documents_with_multiple_chunks"] == 2
    assert aggregate["overlap_pairs_min"] == 1


# ---------------------------------------------------------------------------
# N1：报告头部必须引用真实存在的证据提交，旧标签要更新
# ---------------------------------------------------------------------------
def test_report_header_references_existing_evidence_commits():
    text = (_REPO_ROOT / "docs/evidence/04-budget-ab.md").read_text(
        encoding="utf-8")
    header = "\n".join(text.splitlines()[:12])
    # 头部不能再把 9400e6d 当成 r7 的证据提交（正文的修复说明可以提到它）。
    assert "9400e6d" not in header
    assert "a7dbdfe" in header and "ff0f33c" in header
    assert "评审 r4 只改判据" not in text
    assert "评审 r4–r9 只改判据" in text


# ---------------------------------------------------------------------------
# from-json：新 check 必须由工具重生成，失败块要能判失败
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


def test_from_json_regenerates_failed_chunks_and_overlap_case_runs(tmp_path):
    regen = _run_inline_from_json(evidence_payload(), tmp_path)
    checks = regen["gate"]["checks"]
    assert checks["failed_chunks_zero"] is True
    assert regen["gate"]["failed_chunks"] == 0
    assert regen["overlap_observed"]["candidate"][
        "case_runs_with_multiple_chunks"] == 5
    assert "documents_with_multiple_chunks" not in regen["overlap_observed"][
        "candidate"]


def test_from_json_stress_corpus_failed_chunks_fails_gate(tmp_path):
    payload = evidence_payload()
    payload["budgets"]["3500"]["failed_chunks_max"] = 2
    regen = _run_inline_from_json(payload, tmp_path)
    assert regen["gate"]["checks"]["failed_chunks_zero"] is False
    assert regen["gate"]["passed"] is False


def test_from_json_real_doc_failed_chunks_fails_gate(tmp_path):
    payload = evidence_payload()
    payload["real_docs"]["3500"]["failed_chunks_total"] = 1
    regen = _run_inline_from_json(payload, tmp_path)
    assert regen["gate"]["checks"]["failed_chunks_zero"] is False
    assert regen["gate"]["passed"] is False


def test_from_json_rebuilds_failed_chunks_from_old_evidence_rows(tmp_path):
    # 旧证据没有顶层 failed_chunks_max，但逐篇行有 failed_chunks；不能按 0 放行。
    payload = evidence_payload()
    payload["budgets"]["3500"].pop("failed_chunks_max")
    payload["budgets"]["3500"]["rounds"][0]["cases"][0]["failed_chunks"] = 3
    regen = _run_inline_from_json(payload, tmp_path)
    assert regen["budgets"]["3500"]["failed_chunks_max"] == 3
    assert regen["gate"]["checks"]["failed_chunks_zero"] is False
    assert regen["gate"]["passed"] is False


def test_shared_failure_cases_cover_failed_chunks_check():
    failed = {case[1] for case in GATE_FAILURE_CASES}
    assert "failed_chunks_zero" in failed
