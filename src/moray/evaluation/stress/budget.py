"""分块预算 A/B 的几何残余统计与上线判据（工单 04）

本模块只做**纯计算**，不调模型、不读环境变量：

* :func:`relation_chunk_profile` —— 在给定块预算下，统计黄金关系里有多少
  能在**某一块内**同时找到头与尾（可被该预算救回），有多少仍跨块（残余），
  并单独统计「头尾首次出现相隔超过一个块预算」的文档级远距类别。残余项
  逐条列出，供报告如实披露；本单明确不处理它们，也不能静默丢弃。
* :func:`evaluate_budget_gate` —— 把 1500/3500 两档的指标塞进同一套门槛：
  实体（类型前缀标签归一化后）macro-F1 的 mean/worst 跌幅都 ≤0.5pt、
  **全量关系 F1 的 mean/worst 不下降**（工单验收 2）、**关系「金标覆盖
  子集」的 recall mean/worst 不下降、precision mean/worst 跌幅 ≤0.5pt**、
  候选档截断块为 0、overlap 配置不变且实际仍发生重叠、单文档调用数下降。
  任一不满足即 ``passed=False``，并逐项给出失败原因——门槛写在代码里，
  不能靠报告手写一句“达标”放行。

  全量口径（含 02 金标刻意未标注句面上的 FP）与覆盖子集口径并列列报；
  两者都参与放行（评审 r2 F2 修复：此前全量 F1 只列报，导致全量掉 7pt
  仍可返回 passed）。

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
from ._text import occurrences

# 实体 macro-F1 与关系覆盖子集 precision 允许的最大跌幅（百分点）。
# 写死在代码里，避免报告/工具各写一份、评审时被悄悄放宽。
MAX_ENTITY_F1_DROP_PT = 0.5
MAX_RELATION_PRECISION_DROP_PT = 0.5


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

    评审 r1 S4：``cases`` 声明为 ``Iterable`` 就必须只消费一次（生成器也能用），
    且块按**序号**归属，不能拿可能重名的 ``case.name`` 当 dict key 互相覆盖。
    """
    case_list = list(cases)
    chunks_by_index = [
        Chunker(max_units, overlap_units).chunk_text(case.text)
        for case in case_list
    ]
    total = co_chunk = far_first = 0
    residual: list[dict[str, Any]] = []
    far_examples: list[dict[str, Any]] = []

    for index, case in enumerate(case_list):
        chunks = chunks_by_index[index]
        # 头/尾出现位置在整篇里是固定的，逐关系找即可（纯离线，规模有限）。
        for relation in case.relations:
            total += 1
            head_positions = occurrences(case.text, relation.head)
            tail_positions = occurrences(case.text, relation.tail)
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
        "documents": len(case_list),
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
        # 评审 r2 F11：样例列表截断到 20 条时，总条数与“是否截断”必须显式给出。
        "far_first_occurrence_examples_total": far_first,
        "far_first_occurrence_examples_truncated": far_first > 20,
    }


_REQUIRED_GATE_KEYS = (
    "budget", "overlap_units", "overlap_pairs_min", "chunk_calls",
    "entity_macro_f1_mean", "entity_macro_f1_worst",
    "relation_covered_recall_mean", "relation_covered_recall_worst",
    "relation_covered_precision_mean", "relation_covered_precision_worst",
    # 工单 04 验收 2 明确要求“关系 F1 不下降”，全量口径必须参与放行。
    "relation_f1_mean", "relation_f1_worst",
)
# 评审 r2 F11：只有候选档读 ``truncated_chunks``，基线不再被强制要求。
_REQUIRED_CANDIDATE_KEYS = ("truncated_chunks",)


def evaluate_budget_gate(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    *,
    max_entity_drop_pt: float = MAX_ENTITY_F1_DROP_PT,
) -> dict[str, Any]:
    """按工单 04 的放量门槛判定候选块预算能否切换默认值。

    放行条件（全部满足）：
    * 实体（类型前缀标签归一化后）macro-F1 的 mean/worst 跌幅 ≤0.5pt；
    * **全量关系 F1 的 mean/worst 不下降**（工单验收 2 的原始判据，F2）；
    * 关系「金标覆盖子集」recall 的 mean/worst 不下降；
    * 关系「金标覆盖子集」precision 的 mean/worst 跌幅 ≤0.5pt；
    * 候选档截断块为 0；
    * overlap 配置不变（冻结基线，不接受调用方自证）且实际仍有重叠；
    * 单文档调用数下降。

    覆盖子集与全量口径**并列**：F2 修复后，全量 F1 也进 ``checks``，
    不再出现“全量 F1 掉 7pt 仍 passed”的情况。

    两轴口径不同（评审 r2 F7）：实体 macro-F1 与全量关系 F1 是“逐篇先算
    F1、再跨篇/跨轮平均”的 macro；覆盖子集是“TP/FP/FN 跨篇池化”的 micro。
    ``caliber`` 字段显式标注，报告主表不能把两列当成同一口径比较。

    ``candidate`` 如显式提供 ``entity_macro_f1_worst_delta_pt`` 等逐轮
    最坏差，优先使用它；否则退回“两档各自最差值相减”。门槛参数只能收紧，
    不能放宽（``max_entity_drop_pt > 0.5`` 直接报错）。
    """
    if max_entity_drop_pt > MAX_ENTITY_F1_DROP_PT:
        raise ValueError(
            f"max_entity_drop_pt={max_entity_drop_pt} 超过硬上限 "
            f"{MAX_ENTITY_F1_DROP_PT}；门槛不可放宽")
    missing = [key for key in _REQUIRED_GATE_KEYS
               if key not in baseline or key not in candidate]
    missing += [key for key in _REQUIRED_CANDIDATE_KEYS
                if key not in candidate]
    if missing:
        raise ValueError(f"gate 缺少字段: {sorted(set(missing))}")

    def _pt(before: float, after: float) -> float:
        return round((after - before) * 100, 4)

    def _delta(key: str, explicit_key: str | None = None) -> float:
        if explicit_key and candidate.get(explicit_key) is not None:
            return round(candidate[explicit_key], 4)
        return _pt(baseline[key], candidate[key])

    entity_mean_delta = _delta("entity_macro_f1_mean")
    entity_worst_delta = _delta(
        "entity_macro_f1_worst", "entity_macro_f1_worst_delta_pt")
    covered_recall_mean_delta = _delta("relation_covered_recall_mean")
    covered_recall_worst_delta = _delta(
        "relation_covered_recall_worst",
        "relation_covered_recall_worst_delta_pt")
    covered_precision_mean_delta = _delta("relation_covered_precision_mean")
    covered_precision_worst_delta = _delta(
        "relation_covered_precision_worst",
        "relation_covered_precision_worst_delta_pt")
    full_mean_delta = _delta("relation_f1_mean")
    full_worst_delta = _delta(
        "relation_f1_worst", "relation_f1_worst_delta_pt")

    overlap_unchanged = (
        candidate["overlap_units"] == baseline["overlap_units"])
    overlap_observed = int(candidate["overlap_pairs_min"]) > 0
    checks = {
        "entity_mean_within_0_5pt": entity_mean_delta >= -max_entity_drop_pt,
        "entity_worst_within_0_5pt": entity_worst_delta >= -max_entity_drop_pt,
        "relation_full_f1_mean_not_lower": full_mean_delta >= 0.0,
        "relation_full_f1_worst_not_lower": full_worst_delta >= 0.0,
        "relation_covered_recall_mean_not_lower": covered_recall_mean_delta >= 0.0,
        "relation_covered_recall_worst_not_lower": covered_recall_worst_delta >= 0.0,
        "relation_covered_precision_mean_within_0_5pt": (
            covered_precision_mean_delta >= -MAX_RELATION_PRECISION_DROP_PT),
        "relation_covered_precision_worst_within_0_5pt": (
            covered_precision_worst_delta >= -MAX_RELATION_PRECISION_DROP_PT),
        "truncated_chunks_zero": int(candidate["truncated_chunks"]) == 0,
        "overlap_unchanged": overlap_unchanged,
        "overlap_observed": overlap_observed,
        "chunk_calls_decreased": (
            candidate["chunk_calls"] < baseline["chunk_calls"]),
    }
    failures = [name for name, ok in checks.items() if not ok]
    return {
        "passed": not failures,
        "checks": checks,
        "failures": failures,
        "entity_macro_f1_mean_delta_pt": entity_mean_delta,
        "entity_macro_f1_worst_delta_pt": entity_worst_delta,
        "relation_covered_recall_mean_delta_pt": covered_recall_mean_delta,
        "relation_covered_recall_worst_delta_pt": covered_recall_worst_delta,
        "relation_covered_precision_mean_delta_pt": covered_precision_mean_delta,
        "relation_covered_precision_worst_delta_pt": covered_precision_worst_delta,
        # 全量口径：与覆盖子集并列列报，并参与 checks（F2）。
        "relation_full_mean_delta_pt": full_mean_delta,
        "relation_full_worst_delta_pt": full_worst_delta,
        # 评审 r2 F7：两轴口径不同，报告不得按同一列直接比较。
        "caliber": {
            "entity_macro_f1": (
                "macro: 逐篇 F1 先平均，再跨轮平均"),
            "relation_full_f1": (
                "macro: 逐篇 F1 先平均，再跨轮平均"),
            "relation_covered_prf": (
                "micro: TP/FP/FN 跨篇池化后再跨轮平均"),
        },
        "overlap_unchanged": overlap_unchanged,
        "overlap_observed": overlap_observed,
        "max_entity_drop_pt": max_entity_drop_pt,
        "max_relation_precision_drop_pt": MAX_RELATION_PRECISION_DROP_PT,
        "budgets": {
            "baseline": baseline.get("budget"),
            "candidate": candidate.get("budget"),
        },
    }
