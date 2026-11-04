"""工单 04 第 1 轮评审发现的可离线钉住行为（red → green）。

对应评审编号：
* B2  实体类型前缀标签归一化（``案件编号 A2024-942`` → ``A2024-942``），
      且 strict 口径不被别名改写；
* B1  关系新增「金标覆盖子集」口径，与全量口径并存；
* S1  逐篇预测面持久化（工具层）；
* S2  真实文档是否真正压到候选预算（工具层显式标注，不冒充）；
* S3  overlap 判据用冻结基线 + 实际重叠对数，不再自证；
* S5  N 轮重复的均值差 95% CI，且标签为「独立重复」而非「配对」；
* S4  ``relation_chunk_profile`` 接受任意 Iterable，重名文档不互相覆盖；
* N2  压力语料与真实文档共用同一个 ``_doc_row``；
* N4  实体门槛不可被调用方放宽。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from moray.evaluation.metrics import evaluate
from moray.evaluation.stress import (
    evaluate_case,
    normalize_entity_name_aliases,
    relation_chunk_profile,
)
from moray.evaluation.stress.budget import evaluate_budget_gate
from moray.schema import Entity, EntityMention, ExtractionResult, Relation, Span

from gate_fixtures import GATE_FAILURE_CASES, gate_metrics, passing_candidate

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# B2：实体类型前缀标签归一化
# ---------------------------------------------------------------------------
_TEXT_EVENT = "案件编号 A2024-942 涉及张三等人员。"


def _event_results():
    pred = ExtractionResult(entities=[Entity(
        name="案件编号 A2024-942", type="EVENT",
        mentions=[EntityMention(
            surface="案件编号 A2024-942",
            span=Span(start=0, end=14, text="案件编号 A2024-942"))],
    )])
    gold = ExtractionResult(entities=[Entity(
        name="A2024-942", type="EVENT",
        mentions=[EntityMention(
            surface="A2024-942",
            span=Span(start=5, end=14, text="A2024-942"))],
    )])
    return pred, gold


def test_type_label_prefix_is_normalized_for_relaxed_entity_match():
    pred, gold = _event_results()
    ev = evaluate_case("event", 1, pred, gold, text=_TEXT_EVENT,
                       normalize_type_labels=True)
    assert ev.report.entity_relaxed.tp == 1
    assert ev.report.entity_relaxed.fp == 0
    assert ev.report.entity_relaxed.fn == 0
    # 原始未归一化口径仍可复算（报告要双口径并列）
    raw = evaluate(pred, gold)
    assert raw.entity_relaxed.tp == 0
    assert raw.entity_relaxed.fp == 1
    assert raw.entity_relaxed.fn == 1


def test_type_label_prefix_does_not_change_strict_surface_match():
    pred, gold = _event_results()
    ev = evaluate_case("event", 1, pred, gold, text=_TEXT_EVENT,
                       normalize_type_labels=True)
    # strict 比的是 mention.surface，前缀仍是模型原文，不能被别名“救活”
    assert ev.report.entity_strict.tp == 0
    assert ev.report.entity_strict.fp == 1
    assert ev.report.entity_strict.fn == 1


def test_type_label_prefix_only_strips_labels_of_matching_type():
    pred = ExtractionResult(entities=[Entity(
        name="案件编号中心", type="ORG",
        mentions=[EntityMention(surface="案件编号中心")],
    )])
    gold = ExtractionResult(entities=[Entity(
        name="中心", type="ORG",
        mentions=[EntityMention(surface="中心")],
    )])
    ev = evaluate_case(
        "org", 1, pred, gold, text="案件编号中心。",
        normalize_type_labels=True)
    # ORG 的类型前缀表为空：`案件编号中心` 不能被剥成 `中心`。
    assert ev.report.entity_relaxed.tp == 0
    assert ev.report.entity_relaxed.fp == 1
    assert ev.report.entity_relaxed.fn == 1
    # 直测同一规则，避免再次出现 pred/gold 完全相同而不可能失败的用例。
    assert normalize_entity_name_aliases("案件编号中心", "ORG") == "案件编号中心"
    assert normalize_entity_name_aliases("案件编号中心", "EVENT") == "中心"


# ---------------------------------------------------------------------------
# B1：关系「金标覆盖子集」口径
# ---------------------------------------------------------------------------
_TEXT_COVERED = (
    "张三与李四签约。\n"
    "案件编号 A2024-942 涉及王五等人员，关联单位包括甲公司。\n"
)


def _covered_results():
    gold = ExtractionResult(relations=[
        Relation(head="张三", predicate="签约", tail="李四"),
    ])
    pred = ExtractionResult(relations=[
        Relation(head="张三", predicate="签约", tail="李四"),
        Relation(head="王五", predicate="涉案关联", tail="A2024-942"),
    ])
    return pred, gold


def test_gold_covered_subset_excludes_unlabeled_sentence_predictions():
    pred, gold = _covered_results()
    ev = evaluate_case("covered", 1, pred, gold, text=_TEXT_COVERED)
    # 全量口径：未标注句面照旧记 FP
    assert ev.report.relation.tp == 1
    assert ev.report.relation.fp == 1
    # 金标覆盖子集：只保留 gold 标注覆盖到的句子
    assert ev.relation_covered.tp == 1
    assert ev.relation_covered.fp == 0
    assert ev.relation_covered.fn == 0
    assert ev.gold_covered_sentences == 1


def test_gold_covered_subset_keeps_gold_recall_denominator():
    pred = ExtractionResult(relations=[
        Relation(head="王五", predicate="涉案关联", tail="A2024-942"),
    ])
    gold = ExtractionResult(relations=[
        Relation(head="张三", predicate="签约", tail="李四"),
    ])
    ev = evaluate_case("covered", 1, pred, gold, text=_TEXT_COVERED)
    # 预测只落在未标注句子：子集里是一漏报，不能被“过滤掉”当成没发生
    assert ev.relation_covered.tp == 0
    assert ev.relation_covered.fp == 0
    assert ev.relation_covered.fn == 1


# ---------------------------------------------------------------------------
# 判据层：新门槛（实体双口径 + 关系覆盖子集 + 全量 F1）
# ---------------------------------------------------------------------------
def test_gate_passes_with_normalized_entity_and_covered_relation_caliber():
    gate = evaluate_budget_gate(gate_metrics(), passing_candidate())
    assert gate["passed"] is True
    assert all(gate["checks"].values())
    # 全量口径仍出现在结果里，且全量 F1 也参与放行（F2）
    assert gate["relation_full_mean_delta_pt"] == pytest.approx(0.0)
    assert gate["checks"]["relation_full_f1_mean_not_lower"] is True
    assert gate["relation_covered_recall_mean_delta_pt"] == pytest.approx(0.0)


@pytest.mark.parametrize(("overrides", "failed_check"), GATE_FAILURE_CASES)
def test_gate_fails_when_any_new_threshold_breaks(overrides, failed_check):
    gate = evaluate_budget_gate(gate_metrics(), passing_candidate(**overrides))
    assert gate["passed"] is False
    assert gate["checks"][failed_check] is False


def test_gate_entity_threshold_cannot_be_loosened_by_caller():
    with pytest.raises(ValueError):
        evaluate_budget_gate(
            gate_metrics(), passing_candidate(entity_macro_f1_mean=0.8500),
            max_entity_drop_pt=99.0)


# ---------------------------------------------------------------------------
# S4：Iterable + 重名文档
# ---------------------------------------------------------------------------
class _Rel:
    def __init__(self, head, predicate, tail):
        self.head, self.predicate, self.tail = head, predicate, tail


class _Case:
    def __init__(self, name, text, relations):
        self.name, self.text, self.relations = name, text, relations


def _cases_with_duplicate_names():
    yield _Case("dup", "张三与李四签约。",
                [_Rel("张三", "签约", "李四")])
    yield _Case(
        "dup",
        "王五出现在卷首。" + "无关叙述。" * 3000 + "赵六出现在卷尾。",
        [_Rel("王五", "关联", "赵六")],
    )


def test_relation_chunk_profile_accepts_generator_and_duplicate_names():
    profile = relation_chunk_profile(
        _cases_with_duplicate_names(), max_units=1500, overlap_units=200)
    # 生成器不能被第一遍 dict 推导耗尽；重名也不能让两篇互相覆盖
    assert profile["documents"] == 2
    assert profile["total_relations"] == 2
    assert profile["co_chunk_relations"] == 1
    assert profile["cross_chunk_relations"] == 1


# ---------------------------------------------------------------------------
# S1/S2/S5/N2：工具层可观测面
# ---------------------------------------------------------------------------
def test_doc_row_is_shared_by_stress_and_real_doc_paths():
    """两种批次共用同一行构造器；字段集合是公开可核对的行为面。"""
    from moray.extraction import Chunker
    from tools.chunk_budget_ab import _doc_row

    class _Metrics:
        chunks: list = []

        def summary(self):
            return {"chunk_calls": 2, "input_tokens": 100, "output_tokens": 20,
                    "cached_input_tokens": 0, "truncated_chunks": 0,
                    "truncation_signals": 0, "reworked_chunks": 0}

    class _Out:
        document_metrics = _Metrics()

    class _Doc:
        id = "doc"
        text = "张三与李四签约。乙公司到访。"

    chunks = Chunker(1500, 200).chunk_text(_Doc.text)
    row = _doc_row(_Doc(), chunks, _Out(), 1.25)
    for key in ("n_chunks", "chunk_calls", "input_tokens", "output_tokens",
                "truncated_chunks", "max_chunk_input_tokens",
                "max_chunk_output_tokens", "max_chunk_units",
                "max_chunk_chars", "overlap_pairs", "wall_s"):
        assert key in row
    assert row["n_chunks"] == len(chunks)


def test_delta_summary_reports_unpaired_ci_and_worst_round():
    from tools.chunk_budget_ab import _delta_summary_pt

    summary = _delta_summary_pt([-2.06, -10.36, -10.02])
    assert summary["n"] == 3
    assert summary["mean_pt"] == pytest.approx(-7.48, abs=0.01)
    assert summary["min_pt"] == pytest.approx(-10.36)
    assert summary["ci95_low_pt"] <= summary["mean_pt"] <= summary["ci95_high_pt"]
    assert "unpaired" in summary["label"]


def test_real_doc_coverage_flags_when_candidate_budget_is_not_exercised():
    from tools.chunk_budget_ab import _real_doc_coverage

    out = _real_doc_coverage(
        {"rows": [{"max_chunk_units": 1423}], "max_chunk_units": 1423},
        candidate_budget=3500)
    assert out["max_chunk_units"] == 1423
    assert out["exercises_candidate_budget"] is False
    assert "3500" in out["note"]
