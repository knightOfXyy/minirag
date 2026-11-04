"""工单 04：分块预算 1500→3500 的配置锁定与 A/B 判据（离线，不联网）。

覆盖三层可观察行为：

1. 配置层：默认预算/重叠不变；3500 可经现有配置项设置，且只改预算、不动 overlap；
2. 几何层：跨块关系（头尾无法落进同一块）与「文档级远距首现」被显式统计，
   残余项不静默丢弃；
3. 判据层：A/B 门槛（实体 raw 与 normalized 两套 macro-F1 的 mean/worst 都跌
    ≤0.5pt、全量关系 F1 不降、金标覆盖子集 recall 不降且 precision 跌 ≤0.5pt、
   截断 0、overlap 不变、调用数下降、两档预算不同）任一不满足都必须判失败——
   防止后续为了让结果过关而放宽门槛或只报辅助口径。
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from moray.config import chunk_settings
from moray.evaluation.stress.budget import (
    evaluate_budget_gate,
    relation_chunk_profile,
)
from moray.extraction import Chunker, estimate_tokens

from gate_fixtures import (
    GATE_FAILURE_CASES, evaluate_gate, gate_metrics, passing_candidate,
)


def _clear_chunk_env(monkeypatch):
    for name in (
        "MORAY_CHUNK_MAX_UNITS",
        "MORAY_CHUNK_OVERLAP_UNITS",
        "MORAY_CHUNK_THRESHOLD_UNITS",
        "MORAY_CHUNK_CONCURRENCY",
    ):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# 配置层
# ---------------------------------------------------------------------------
def test_default_budget_and_overlap_are_unchanged(monkeypatch):
    """未切换默认值时线上行为不变：1500 / 200 与改造前逐字一致。"""
    _clear_chunk_env(monkeypatch)
    cs = chunk_settings()
    assert cs["max_units"] == 1500
    assert cs["overlap_units"] == 200
    # Chunker 的默认值也必须与配置默认一致，避免两处默认悄悄漂移
    assert Chunker().max_units == cs["max_units"]
    assert Chunker().overlap_units == cs["overlap_units"]


def test_3500_is_settable_via_existing_config_without_touching_overlap(monkeypatch):
    """现有配置项 MORAY_CHUNK_MAX_UNITS=3500 可设；overlap 仍是 200。"""
    _clear_chunk_env(monkeypatch)
    monkeypatch.setenv("MORAY_CHUNK_MAX_UNITS", "3500")
    cs = chunk_settings()
    assert cs["max_units"] == 3500
    assert cs["overlap_units"] == 200  # 本单明确不动 overlap

    text = "临江宏远实业集团有限公司注册地位于临江区。" * 900
    chunks = Chunker(cs["max_units"], cs["overlap_units"]).chunk_text(text)
    assert len(chunks) > 1
    for c in chunks:
        # 偏移不变量：每块恰为原文切片，且不超预算（重叠句可能让它略越界，
        # 与 1500 档同一条既有语义，见 tests/test_chunking.py）。
        assert text[c.start:c.end] == c.text
        assert c.n_units <= cs["max_units"] + cs["overlap_units"]
    # 相邻块**每一对**都带 overlap（不是 0），且重叠长度不超过配置值，
    # 证明行为没有被本单改掉（评审 r1 N1：不再用 any/首字符糊弄）。
    for a, b in zip(chunks, chunks[1:]):
        assert b.start > a.start
        overlap_chars = a.end - b.start
        assert 0 < overlap_chars <= cs["overlap_units"]
        assert text[b.start:a.end] == b.text[:overlap_chars]


def test_larger_budget_reduces_chunk_calls_with_same_overlap(monkeypatch):
    """同一正文：3500 档块数严格少于 1500 档，overlap 配置相同。"""
    _clear_chunk_env(monkeypatch)
    text = "甲公司昨日在临江区签约。乙公司同日到访。丙公司随后回应。" * 800
    small = Chunker(1500, 200).chunk_text(text)
    big = Chunker(3500, 200).chunk_text(text)
    assert len(big) < len(small)
    for grid in (small, big):
        for c in grid:
            assert text[c.start:c.end] == c.text
        # 两档都仍是同一套 200-unit overlap 行为，不是“块变大就偷偷去重叠”
        for a, b in zip(grid, grid[1:]):
            assert 0 < a.end - b.start <= 200


# ---------------------------------------------------------------------------
# 几何层：残余跨块关系必须显式统计
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _Rel:
    head: str
    predicate: str
    tail: str


@dataclass(frozen=True)
class _Case:
    name: str
    text: str
    relations: list[_Rel]


def _synthetic_case():
    # 近距关系：头尾同一句，任何预算都能同块
    near = "张三与李四签约。"
    # 两个远距关系：头在开头，尾在大量填充句之后（超过 3500 units）
    filler = "无关叙述用于拉开距离。" * 2000  # 约 22000 units
    text = (
        near
        + "王五出现在卷首。"
        + filler
        + "赵六出现在卷尾。"
        + "孙七也出现在卷尾。"
    )
    return _Case(
        name="synthetic",
        text=text,
        relations=[
            _Rel("张三", "签约", "李四"),
            _Rel("王五", "关联", "赵六"),
            _Rel("王五", "关联", "孙七"),
        ],
    )


def test_relation_chunk_profile_counts_cross_chunk_residual():
    case = _synthetic_case()
    profile = relation_chunk_profile([case], max_units=1500, overlap_units=200)
    assert profile["total_relations"] == 3
    assert profile["co_chunk_relations"] == 1
    assert profile["cross_chunk_relations"] == 2
    assert profile["cross_chunk_ratio"] == pytest.approx(2 / 3, abs=1e-6)
    # 两条残余必须逐条列出（头/谓词/尾/首现间隔），不能只给一个比例
    residual = profile["residual_relations"]
    assert len(residual) == 2
    assert {r["tail"] for r in residual} == {"赵六", "孙七"}
    assert all(r["first_occurrence_gap_units"] > 1500 for r in residual)
    # 「首现相隔超过一个块预算」的文档级类别也要单独统计
    assert profile["far_first_occurrence_relations"] == 2


def test_relation_chunk_profile_at_3500_still_lists_document_level_residual():
    """预算变大后残余可能为 0，但文档级远距类别仍要被统计，不能静默丢弃。"""
    case = _synthetic_case()
    profile = relation_chunk_profile([case], max_units=3500, overlap_units=200)
    # 22000 units 的间隔放不进 3500 块 → 两条仍跨块
    assert profile["cross_chunk_relations"] == 2
    assert profile["cross_chunk_ratio"] == pytest.approx(2 / 3, abs=1e-6)
    assert profile["far_first_occurrence_relations"] == 2


def test_relation_chunk_profile_uses_any_occurrence_pair():
    """卷尾重复点名可救回远距关系：任一头/尾出现同块即算可覆盖。"""
    text = (
        "王五出现在卷首。"
        + "无关叙述。" * 3000
        + "卷尾再次提到王五，并与赵六同处一段。"
    )
    case = _Case(
        name="repeat",
        text=text,
        relations=[_Rel("王五", "关联", "赵六")],
    )
    profile = relation_chunk_profile([case], max_units=1500, overlap_units=200)
    assert profile["total_relations"] == 1
    assert profile["co_chunk_relations"] == 1
    assert profile["cross_chunk_relations"] == 0


# ---------------------------------------------------------------------------
# 判据层：门槛不可被悄悄放宽
# ---------------------------------------------------------------------------
def test_budget_gate_passes_only_when_all_thresholds_met():
    gate = evaluate_gate(gate_metrics(), passing_candidate())
    assert gate["passed"] is True
    assert all(gate["checks"].values())
    assert gate["entity_macro_f1_mean_delta_pt"] == pytest.approx(-0.4)
    assert gate["overlap_unchanged"] is True
    # 全量口径与覆盖子集并列列报；全量 F1 也参与放行（F2）
    assert gate["relation_full_mean_delta_pt"] == pytest.approx(0.0)
    assert gate["checks"]["relation_full_f1_mean_not_lower"] is True


@pytest.mark.parametrize(("overrides", "failed_check"), GATE_FAILURE_CASES)
def test_budget_gate_fails_when_any_threshold_breaks(overrides, failed_check):
    gate = evaluate_gate(gate_metrics(), passing_candidate(**overrides))
    assert gate["passed"] is False
    assert gate["checks"][failed_check] is False


def test_budget_gate_boundary_exactly_half_point_passes():
    gate = evaluate_gate(gate_metrics(), passing_candidate(
        entity_macro_f1_mean=0.8950,
        entity_macro_f1_worst=0.8750,
        raw_entity_macro_f1_mean=0.8950,
        raw_entity_macro_f1_worst=0.8750,
        relation_covered_precision_mean=0.8950,
        relation_covered_precision_worst=0.8450,
    ))
    assert gate["passed"] is True
    assert gate["entity_macro_f1_mean_delta_pt"] == pytest.approx(-0.5)


def test_budget_gate_prefers_explicit_worst_round_delta():
    """worst-of-N 用逐轮对齐差（严格），不能拿两档各自最差值相减糊弄过去。"""
    baseline_rounds = [
        {"entity_macro_f1": 0.9000, "raw_entity_macro_f1": 0.9000,
         "entity_strict_f1": 0.9600, "relation_f1": 0.5000,
         "raw_relation_f1": 0.5000, "relation_covered_recall": 0.8000,
         "relation_covered_precision": 0.9000},
        {"entity_macro_f1": 0.9000, "raw_entity_macro_f1": 0.9000,
         "entity_strict_f1": 0.9600, "relation_f1": 0.5000,
         "raw_relation_f1": 0.5000, "relation_covered_recall": 0.8000,
         "relation_covered_precision": 0.9000},
    ]
    candidate_rounds = [
        {"entity_macro_f1": 0.9000, "raw_entity_macro_f1": 0.9000,
         "entity_strict_f1": 0.9600, "relation_f1": 0.5000,
         "raw_relation_f1": 0.5000, "relation_covered_recall": 0.8000,
         "relation_covered_precision": 0.9000},
        {"entity_macro_f1": 0.8920, "raw_entity_macro_f1": 0.8920,
         "entity_strict_f1": 0.9520, "relation_f1": 0.5000,
         "raw_relation_f1": 0.5000, "relation_covered_recall": 0.7980,
         "relation_covered_precision": 0.9000},
    ]
    gate = evaluate_gate(gate_metrics(), passing_candidate(
        entity_macro_f1_worst_delta_pt=-0.8,
        raw_entity_macro_f1_worst_delta_pt=-0.8,
        relation_covered_recall_worst_delta_pt=-0.2,
    ), baseline_rounds=baseline_rounds, candidate_rounds=candidate_rounds)
    assert gate["entity_macro_f1_worst_delta_pt"] == pytest.approx(-0.8)
    assert gate["raw_entity_macro_f1_worst_delta_pt"] == pytest.approx(-0.8)
    assert gate["checks"]["entity_normalized_worst_within_0_5pt"] is False
    assert gate["checks"]["entity_raw_worst_within_0_5pt"] is False
    assert gate["checks"]["relation_covered_recall_worst_not_lower"] is False
    assert gate["passed"] is False


def test_token_estimation_is_not_mistaken_for_real_tokenizer():
    """报告口径自检：估算器只用于切块，不能当作「真实 tokenizer」数字。"""
    from tools.chunk_budget_ab import _safety_margin

    text = "临江宏远实业集团有限公司注册地位于临江区。"
    # 估算器把每个汉字/标点各算 1 unit；真实 tokenizer 由 provider usage 提供，
    # 二者不同，报告必须分开标注。
    assert estimate_tokens(text) == len(text)
    # 余量必须真的读 provider usage 口径，并用固定输入验证减法，而不是同义反复。
    out = _safety_margin(
        {"max_chunk_input_tokens": 100, "max_chunk_output_tokens": 20},
        {"max_chunk_input_tokens": 150, "max_chunk_output_tokens": 30},
        {"context_window": 1000, "max_output_tokens": 500},
    )
    assert "provider usage" in out["source"]
    assert out["max_chunk_input_tokens"] == 150
    assert out["input_margin_tokens"] == 850
    assert out["input_plus_output_margin_tokens"] == 820
