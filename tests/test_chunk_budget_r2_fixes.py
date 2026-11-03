"""工单 04 第 2 轮评审发现的可离线钉住行为（red → green）。

对应评审编号：
* F1 关系头/尾按「实体类型前缀标签」归一化（含实体缺失时的全表回退）；
* F2 放行 gate 必须包含全量关系 F1 不下降（覆盖子集只作并列列报）；
* F3 真实文档批次可显式追加长文，并如实标注是否压到候选预算；
* F4 实体/关系归一化是 04 A/B 的 opt-in，默认不改变 02/03 既有口径；
* F6 逐篇块按序号归属，重名文档不能互相覆盖；
* F7 gate 显式标注实体 macro / 关系覆盖子集 micro 的口径差异；
* F11 远距首现样例截断时给出总条数/截断标记；
* F13 跨句 gold 关系被显式计入「未覆盖」而不是静默丢出分母。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from moray.evaluation.metrics import evaluate
from moray.evaluation.stress import (
    aggregate_cases,
    evaluate_case,
    normalize_entity_name_aliases,
    relation_chunk_profile,
)
from moray.evaluation.stress.budget import evaluate_budget_gate
from moray.evaluation.stress.metrics import relation_covered_stats
from moray.schema import Entity, EntityMention, ExtractionResult, Relation, Span

# ---------------------------------------------------------------------------
# F1：关系端点归一化
# ---------------------------------------------------------------------------
_TEXT_EVENT_REL = "案件编号 A2024-942 涉及王五等人员。"
_PREFIX = "案件编号 A2024-942"


def _event_relation_results():
    pred = ExtractionResult(
        entities=[Entity(
            name=_PREFIX, type="EVENT",
            mentions=[EntityMention(
                surface=_PREFIX,
                span=Span(start=0, end=len(_PREFIX), text=_PREFIX))],
        )],
        relations=[Relation(
            head="王五", predicate="涉案关联", tail=_PREFIX)],
    )
    gold = ExtractionResult(
        entities=[Entity(
            name="A2024-942", type="EVENT",
            mentions=[EntityMention(
                surface="A2024-942",
                span=Span(start=5, end=len(_PREFIX), text="A2024-942"))],
        )],
        relations=[Relation(
            head="王五", predicate="涉案关联", tail="A2024-942")],
    )
    return pred, gold


def test_relation_endpoints_are_normalized_with_entity_type_prefix():
    pred, gold = _event_relation_results()
    ev = evaluate_case(
        "event-rel", 1, pred, gold, text=_TEXT_EVENT_REL,
        normalize_type_labels=True)
    # 修复前 tail 带前缀，关系永远匹配不上 gold：tp=0 / fp=1 / fn=1。
    assert ev.report.relation.tp == 1
    assert ev.report.relation.fp == 0
    assert ev.report.relation.fn == 0
    assert ev.relation_covered.tp == 1
    assert ev.relation_covered.fp == 0
    assert ev.relation_covered.fn == 0


def test_relation_endpoint_normalization_falls_back_to_prefix_table():
    text = "案件编号 B2025-1 涉及王五。"
    pred = ExtractionResult(relations=[
        Relation(head="王五", predicate="涉案关联",
                 tail="案件编号 B2025-1")])
    gold = ExtractionResult(relations=[
        Relation(head="王五", predicate="涉案关联", tail="B2025-1")])
    ev = evaluate_case(
        "fallback", 1, pred, gold, text=text,
        normalize_type_labels=True)
    # 关系端点在实体列表里查不到类型时，按前缀表全表剥。
    assert ev.report.relation.tp == 1
    assert ev.report.relation.fp == 0
    assert ev.report.relation.fn == 0


# ---------------------------------------------------------------------------
# F4：归一化必须 opt-in，默认保持 02/03 既有口径
# ---------------------------------------------------------------------------
def test_evaluate_case_default_keeps_legacy_raw_caliber():
    pred, gold = _event_relation_results()
    ev = evaluate_case("legacy", 1, pred, gold, text=_TEXT_EVENT_REL)
    # 默认不归一化：实体和关系都按原始名字比较。
    assert ev.report.entity_relaxed.tp == 0
    assert ev.report.entity_relaxed.fp == 1
    assert ev.report.entity_relaxed.fn == 1
    assert ev.report.relation.tp == 0
    assert ev.report.relation.fp == 1
    assert ev.report.relation.fn == 1
    # 与直接调用底层 evaluate 的历史口径一致。
    raw = evaluate(pred, gold)
    assert raw.entity_relaxed == ev.report.entity_relaxed
    assert raw.relation == ev.report.relation


# ---------------------------------------------------------------------------
# F2：全量关系 F1 也必须参与放行（覆盖子集不能替代）
# ---------------------------------------------------------------------------
def _metrics(**overrides):
    base = {
        "budget": 1500,
        "overlap_units": 200,
        "overlap_pairs_min": 8,
        "chunk_calls": 14,
        "truncated_chunks": 3,
        "entity_macro_f1_mean": 0.9000,
        "entity_macro_f1_worst": 0.8800,
        "relation_covered_recall_mean": 0.8000,
        "relation_covered_recall_worst": 0.7500,
        "relation_covered_precision_mean": 0.9000,
        "relation_covered_precision_worst": 0.8500,
        "relation_f1_mean": 0.5000,
        "relation_f1_worst": 0.4500,
    }
    base.update(overrides)
    return base


def _passing_candidate(**overrides):
    candidate = _metrics(
        budget=3500,
        overlap_pairs_min=7,
        chunk_calls=6,
        truncated_chunks=0,
        entity_macro_f1_mean=0.8960,
        entity_macro_f1_worst=0.8760,
        relation_covered_recall_mean=0.8000,
        relation_covered_recall_worst=0.7500,
        relation_covered_precision_mean=0.8950,
        relation_covered_precision_worst=0.8450,
        relation_f1_mean=0.5000,
        relation_f1_worst=0.4500,
    )
    candidate.update(overrides)
    return candidate


def test_gate_passes_only_when_full_relation_f1_also_holds():
    gate = evaluate_budget_gate(_metrics(), _passing_candidate())
    assert gate["passed"] is True
    assert gate["checks"]["relation_full_f1_mean_not_lower"] is True
    assert gate["checks"]["relation_full_f1_worst_not_lower"] is True


def test_gate_fails_when_full_relation_f1_drops_even_if_covered_subset_passes():
    gate = evaluate_budget_gate(
        _metrics(), _passing_candidate(
            relation_f1_mean=0.4300, relation_f1_worst=0.4000))
    # 覆盖子集全过，但全量关系 F1 掉 7pt：工单要求“关系 F1 不下降”，必须拦下。
    assert gate["checks"]["relation_covered_recall_mean_not_lower"] is True
    assert gate["checks"]["relation_full_f1_mean_not_lower"] is False
    assert gate["checks"]["relation_full_f1_worst_not_lower"] is False
    assert gate["passed"] is False


def test_gate_requires_full_relation_f1_fields():
    baseline = _metrics()
    candidate = _passing_candidate()
    baseline.pop("relation_f1_mean")
    with pytest.raises(ValueError, match="relation_f1_mean"):
        evaluate_budget_gate(baseline, candidate)


# ---------------------------------------------------------------------------
# F3：真实文档批次可追加长文，并如实标注覆盖面
# ---------------------------------------------------------------------------
def test_load_real_docs_accepts_extra_long_document(tmp_path: Path):
    from tools.chunk_budget_ab import _load_real_docs

    long_path = tmp_path / "long_case.txt"
    text = "临江宏远实业集团有限公司注册地位于临江区。" * 900
    long_path.write_text(text, encoding="utf-8")

    docs = _load_real_docs(
        str(tmp_path / "missing-gold"),
        str(tmp_path / "missing-samples"),
        extra_docs=[str(long_path)],
    )
    assert len(docs) == 1
    assert docs[0].text == text
    assert docs[0].source == "real-doc"
    assert "long_case.txt" in docs[0].id


def test_real_doc_coverage_marks_long_document_as_exercising_budget():
    from tools.chunk_budget_ab import _real_doc_coverage

    out = _real_doc_coverage(
        {"rows": [
            {"document_units": 341, "max_chunk_units": 341},
            {"document_units": 5360, "max_chunk_units": 3483},
        ]},
        candidate_budget=3500)
    assert out["exercises_candidate_budget"] is True
    assert out["max_document_units"] == 5360
    assert out["max_chunk_units"] == 3483


# ---------------------------------------------------------------------------
# F6：重名文档的块不能互相覆盖
# ---------------------------------------------------------------------------
def test_chunk_cases_does_not_collide_on_duplicate_names():
    from tools.chunk_budget_ab import _chunk_cases

    @dataclass(frozen=True)
    class _Doc:
        name: str
        text: str

    short = _Doc("dup", "乙公司到访。")
    long = _Doc("dup", "甲公司签约。" * 400)
    grids = _chunk_cases([short, long], budget=1500, overlap=200)
    assert len(grids) == 2
    assert len(grids[0]) == 1
    assert len(grids[1]) > 1
    assert grids[0] is not grids[1]


# ---------------------------------------------------------------------------
# F7：gate 显式标注两轴口径
# ---------------------------------------------------------------------------
def test_gate_reports_macro_and_micro_calibers():
    gate = evaluate_budget_gate(_metrics(), _passing_candidate())
    assert "macro" in gate["caliber"]["entity_macro_f1"]
    assert "micro" in gate["caliber"]["relation_covered_prf"]
    assert "macro" in gate["caliber"]["relation_full_f1"]


# ---------------------------------------------------------------------------
# F11：远距样例被截断时给出总条数
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


def test_relation_chunk_profile_reports_far_example_truncation():
    heads = [f"H{i}" for i in range(21)]
    tails = [f"T{i}" for i in range(21)]
    text = (
        "".join(f"{h}出现在卷首。" for h in heads)
        + "无关叙述。" * 2000
        + "".join(f"{t}出现在卷尾。" for t in tails)
    )
    case = _Case(
        name="far",
        text=text,
        relations=[_Rel(h, "关联", t) for h, t in zip(heads, tails)],
    )
    profile = relation_chunk_profile([case], max_units=500, overlap_units=50)
    assert profile["far_first_occurrence_relations"] == 21
    assert len(profile["far_first_occurrence_examples"]) == 20
    assert profile["far_first_occurrence_examples_total"] == 21
    assert profile["far_first_occurrence_examples_truncated"] is True


# ---------------------------------------------------------------------------
# F13：跨句 gold 关系显式计入未覆盖
# ---------------------------------------------------------------------------
def test_cross_sentence_gold_relations_are_reported_as_uncovered():
    text = "张三与李四签约。\n中间一句。\n王五出现在卷首。\n赵六出现在卷尾。"
    pred = ExtractionResult(relations=[
        Relation(head="张三", predicate="签约", tail="李四"),
    ])
    gold = ExtractionResult(relations=[
        Relation(head="张三", predicate="签约", tail="李四"),
        Relation(head="王五", predicate="关联", tail="赵六"),
    ])
    stats = relation_covered_stats(text, pred, gold)
    assert stats.covered_sentences >= 1
    assert stats.gold_relations_total == 2
    # 跨句关系既不在 covered spans，也不进 recall 分母；必须显式计数。
    assert stats.uncovered_gold_relations == 1

    ev = evaluate_case("cross-sentence", 1, pred, gold, text=text)
    assert ev.uncovered_gold_relations == 1
    agg = aggregate_cases([ev])
    assert agg["uncovered_gold_relations"] == 1


def test_only_matching_type_prefix_is_stripped_directly():
    # F8：这条直测能真正区分“只剥匹配类型”与“全表乱剥”。
    assert normalize_entity_name_aliases("案件编号中心", "ORG") == "案件编号中心"
    assert normalize_entity_name_aliases("案件编号中心", "EVENT") == "中心"
