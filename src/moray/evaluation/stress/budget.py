"""分块预算 A/B 的几何残余统计与上线判据（工单 04）

本模块只做**纯计算**，不调模型、不读环境变量：

* :func:`relation_chunk_profile` —— 在给定块预算下，统计黄金关系里有多少
  能在**某一块内**同时找到头与尾（可被该预算救回），有多少仍跨块（残余），
  并单独统计「头尾首次出现相隔超过一个块预算」的文档级远距类别。残余项
  逐条列出，供报告如实披露；本单明确不处理它们，也不能静默丢弃。
* :func:`evaluate_budget_gate` —— 把 1500/3500 两档的指标塞进同一套门槛：
  实体 macro-F1 的 mean/worst 跌幅都 ≤0.5pt、关系 F1 的 mean/worst 都不下降、
  候选档截断块为 0、overlap 配置不变、单文档调用数下降。任一不满足即
  ``passed=False``，并逐项给出失败原因——门槛写在代码里，不能靠报告手写
  一句“达标”放行。

口径说明（与工单 02/01 对齐）：

* 实体/关系指标来自 :func:`moray.evaluation.stress.aggregate_cases`；
* 截断块来自工单 01 的 ``DocumentMetrics.truncated_chunks``；
* 本模块的“units”仍是分块器使用的保守估算单位，**不是**真实 tokenizer
  数字。真实 tokenizer 口径由 A/B 工具从 provider 返回的 ``usage.input_tokens``
  记录（含正文+系统提示+结构化 schema），两者在报告里分开标注。
"""

from __future__ import annotations

from typing import Any, Iterable

from ...extraction.chunking import Chunker, estimate_tokens

# 实体 macro-F1 允许的最大跌幅（百分点）。写死在代码里，避免报告/工具
# 各写一份、评审时被悄悄放宽。
MAX_ENTITY_F1_DROP_PT = 0.5


def _occurrences(text: str, needle: str) -> list[int]:
    """``needle`` 在 ``text`` 中每一次字面出现的起始下标（空串返回空）。"""
    if not needle:
        return []
    out: list[int] = []
    start = 0
    while True:
        idx = text.find(needle, start)
        if idx < 0:
            return out
        out.append(idx)
        start = idx + len(needle)


def _same_chunk(chunks, head_pos: int, head_len: int, tail_pos: int,
                tail_len: int) -> bool:
    """是否存在一个块同时完整包含这对头/尾出现。"""
    for c in chunks:
        if (c.start <= head_pos and head_pos + head_len <= c.end
                and c.start <= tail_pos and tail_pos + tail_len <= c.end):
            return True
    return False


def relation_chunk_profile(
    cases: Iterable[Any],
    *,
    max_units: int,
    overlap_units: int,
) -> dict[str, Any]:
    """统计一档块预算下关系的可覆盖/跨块情况。

    ``cases`` 需要具备 ``name``/``text``/``relations`` 三个属性，关系元素
    具备 ``head``/``predicate``/``tail``（与工单 02 的 ``StressGoldCase``
    同形；测试里用轻量 dataclass 也可以）。
    """
    chunks_by_case = {
        case.name: Chunker(max_units, overlap_units).chunk_text(case.text)
        for case in cases
    }
    total = co_chunk = far_first = 0
    residual: list[dict[str, Any]] = []
    far_examples: list[dict[str, Any]] = []

    for case in cases:
        chunks = chunks_by_case[case.name]
        # 头/尾出现位置在整篇里是固定的，逐关系找即可（纯离线，规模有限）。
        for relation in case.relations:
            total += 1
            head_positions = _occurrences(case.text, relation.head)
            tail_positions = _occurrences(case.text, relation.tail)
            head_len, tail_len = len(relation.head), len(relation.tail)

            covered = any(
                _same_chunk(chunks, h, head_len, t, tail_len)
                for h in head_positions for t in tail_positions
            )
            if covered:
                co_chunk += 1
            else:
                row = {
                    "document": case.name,
                    "head": relation.head,
                    "predicate": relation.predicate,
                    "tail": relation.tail,
                    "head_occurrences": len(head_positions),
                    "tail_occurrences": len(tail_positions),
                }
                first_gap = None
                if head_positions and tail_positions:
                    a, b = sorted((head_positions[0], tail_positions[0]))
                    first_gap = estimate_tokens(case.text[a:b])
                row["first_occurrence_gap_units"] = first_gap
                residual.append(row)

            if head_positions and tail_positions:
                a, b = sorted((head_positions[0], tail_positions[0]))
                gap = estimate_tokens(case.text[a:b])
                if gap > max_units:
                    far_first += 1
                    far_examples.append({
                        "document": case.name,
                        "head": relation.head,
                        "predicate": relation.predicate,
                        "tail": relation.tail,
                        "first_occurrence_gap_units": gap,
                        "covered_by_repeated_mention": covered,
                    })

    def _ratio(n: int) -> float:
        return round(n / total, 6) if total else 0.0

    return {
        "budget": {"max_units": max_units, "overlap_units": overlap_units},
        "documents": len(chunks_by_case),
        "total_relations": total,
        "co_chunk_relations": co_chunk,
        "cross_chunk_relations": total - co_chunk,
        "cross_chunk_ratio": _ratio(total - co_chunk),
        "cross_chunk_ratio_pct": round(_ratio(total - co_chunk) * 100, 4),
        # 「文档级远距」：头尾首次出现间隔超过一个块预算。它们即使调大预算
        # 也未必能同块，是残余项的来源；卷尾重复点名可能把它们救回，因此
        # 与 cross_chunk_relations 是两个口径，必须分别列报。
        "far_first_occurrence_relations": far_first,
        "far_first_occurrence_ratio": _ratio(far_first),
        "far_first_occurrence_ratio_pct": round(_ratio(far_first) * 100, 4),
        "residual_relations": residual,
        "far_first_occurrence_examples": far_examples[:20],
    }


def evaluate_budget_gate(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    *,
    max_entity_drop_pt: float = MAX_ENTITY_F1_DROP_PT,
) -> dict[str, Any]:
    """按工单 04 的放量门槛判定候选块预算能否切换默认值。

    ``baseline``/``candidate`` 至少包含：
    ``budget``、``overlap_units``、``chunk_calls``、``truncated_chunks``、
    ``entity_macro_f1_mean``、``entity_macro_f1_worst``、
    ``relation_f1_mean``、``relation_f1_worst``。
    指标是 0–1 的比例，报告里的“百分点”由本函数换算。
    """
    def _pt(before: float, after: float) -> float:
        return round((after - before) * 100, 4)

    entity_mean_delta = _pt(
        baseline["entity_macro_f1_mean"], candidate["entity_macro_f1_mean"])
    entity_worst_delta = _pt(
        baseline["entity_macro_f1_worst"], candidate["entity_macro_f1_worst"])
    relation_mean_delta = _pt(
        baseline["relation_f1_mean"], candidate["relation_f1_mean"])
    relation_worst_delta = _pt(
        baseline["relation_f1_worst"], candidate["relation_f1_worst"])

    overlap_unchanged = (
        candidate.get("overlap_units") == baseline.get("overlap_units"))
    checks = {
        "entity_mean_within_0_5pt": entity_mean_delta >= -max_entity_drop_pt,
        "entity_worst_within_0_5pt": entity_worst_delta >= -max_entity_drop_pt,
        "relation_mean_not_lower": relation_mean_delta >= 0.0,
        "relation_worst_not_lower": relation_worst_delta >= 0.0,
        "truncated_chunks_zero": int(candidate.get("truncated_chunks", 0)) == 0,
        "overlap_unchanged": overlap_unchanged,
        "chunk_calls_decreased": (
            candidate.get("chunk_calls", 0) < baseline.get("chunk_calls", 0)),
    }
    failures = [name for name, ok in checks.items() if not ok]
    return {
        "passed": not failures,
        "checks": checks,
        "failures": failures,
        "entity_macro_f1_mean_delta_pt": entity_mean_delta,
        "entity_macro_f1_worst_delta_pt": entity_worst_delta,
        "relation_f1_mean_delta_pt": relation_mean_delta,
        "relation_f1_worst_delta_pt": relation_worst_delta,
        "overlap_unchanged": overlap_unchanged,
        "max_entity_drop_pt": max_entity_drop_pt,
        "budgets": {
            "baseline": baseline.get("budget"),
            "candidate": candidate.get("budget"),
        },
    }
