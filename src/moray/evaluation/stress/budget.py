"""分块预算 A/B 的几何残余统计与上线判据（工单 04）

本模块只做**纯计算**，不调模型、不读环境变量：

* :func:`relation_chunk_profile` —— 在给定块预算下，统计黄金关系里有多少
  能在**某一块内**同时找到头与尾（可被该预算救回），有多少仍跨块（残余），
  并单独统计「头尾首次出现相隔超过一个块预算」的文档级远距类别。残余项
  逐条列出，供报告如实披露；本单明确不处理它们，也不能静默丢弃。
* :func:`evaluate_budget_gate` —— 把 1500/3500 两档的指标塞进同一套门槛：
  **实体原始（未归一化）macro-F1 与类型前缀标签归一化后的 macro-F1 两套
  口径的 mean/worst 跌幅都 ≤0.5pt**（评审 r4 B1：辅助口径不能替代验收
  口径）、**全量关系 F1 的 raw 与 normalized 两套 mean/worst 都不下降**
  （工单验收 2；评审 r5 S1：原始口径不能只列报）、**关系「金标覆盖子集」
  的 recall mean/worst 不下降、precision mean/worst 跌幅 ≤0.5pt**、候选档
  截断块为 0、overlap 配置不变且实际仍发生重叠、单文档调用数下降、两档
  预算必须不同。任一不满足即 ``passed=False``，并逐项给出失败原因——门槛
  写在代码里，不能靠报告手写一句“达标”放行。

  评审 r7 又补了两处空转口：真实 tokenizer 的 input/output 计数为 0（provider
  usage 缺失）时 ``context_within_window`` fail closed；real-doc 批次必须非空
  且最大文档 units 超过候选预算，空批次或只有短桩时
  ``real_doc_coverage_exercises_budget`` fail（验收 #4）。overlap 的最小值只在
  多块文档上统计，单块文档的 0 对不再误判，但整批没有多块文档时仍 fail closed。

  全量口径（含 02 金标刻意未标注句面上的 FP）与覆盖子集口径并列列报；
  两者都参与放行（评审 r2 F2 修复：此前全量 F1 只列报，导致全量掉 7pt
  仍可返回 passed）。评审 r5 S1 进一步把全量关系 F1 拆成 raw/normalized
  双口径，两套都进 checks。

口径说明（与工单 02/01 对齐）：

* 实体/关系指标来自 :func:`moray.evaluation.stress.aggregate_cases`；
* 截断块来自工单 01 的 ``DocumentMetrics.truncated_chunks``；
* 本模块的“units”仍是分块器使用的保守估算单位，**不是**真实 tokenizer
  数字。真实 tokenizer 口径由 A/B 工具从 provider 返回的 ``usage.input_tokens``
  记录（含正文+系统提示+结构化 schema），两者在报告里分开标注。
"""

from __future__ import annotations

import math
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
            # 同一 first-occurrence gap 只算一次（评审 r4 N2）。
            first_gap = None
            if head_positions and tail_positions:
                a, b = sorted((head_positions[0], tail_positions[0]))
                first_gap = estimate_tokens(case.text[a:b])
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
                row["first_occurrence_gap_units"] = first_gap
                residual.append(row)

            if first_gap is not None and first_gap > max_units:
                far_first += 1
                far_examples.append({
                    "document": case.name,
                    "head": relation.head,
                    "predicate": relation.predicate,
                    "tail": relation.tail,
                    "first_occurrence_gap_units": first_gap,
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
    # 评审 r7 N3：overlap 最小值只统计“可重叠”的多块文档；单块文档不能
    # 把 min_overlap_pairs 拉到 0 造成误判，但“没有任何多块文档”仍 fail closed。
    "overlap_documents_with_multiple_chunks",
    # 评审 r7 S2：验收 #4 的真实文档覆盖必须进 checks，不能只写进 gate 报告。
    "real_doc_documents", "real_doc_max_document_units",
    # 实体轴双口径：raw 是验收口径，normalized 是辅助口径；两套都必填、
    # 都进 checks。评审 r4 B1：不允许只填归一化辅助口径就放行。
    "entity_macro_f1_mean", "entity_macro_f1_worst",
    "raw_entity_macro_f1_mean", "raw_entity_macro_f1_worst",
    # 评审 r6 F3：strict 实体 F1 的 mean/worst 也落盘并进 checks；
    # 工单 02 的评测把 strict/relaxed 并列，不能只报 relaxed。
    "entity_strict_f1_mean", "entity_strict_f1_worst",
    "relation_covered_recall_mean", "relation_covered_recall_worst",
    "relation_covered_precision_mean", "relation_covered_precision_worst",
    # 工单 04 验收 2 明确要求“关系 F1 不下降”，全量口径必须参与放行。
    "relation_f1_mean", "relation_f1_worst",
    # 评审 r5 S1：全量关系 F1 也分 raw/normalized 双口径；原始（验收）
    # 口径不能只列报，否则归一化口径转正时可在原始关系 F1 更低时放行。
    "raw_relation_f1_mean", "raw_relation_f1_worst",
)
# 评审 r2 F11：只有候选档读 ``truncated_chunks``，基线不再被强制要求。
_REQUIRED_CANDIDATE_KEYS = ("truncated_chunks",)

# 调用方提供的 explicit worst delta 与 rounds 的对应关系（评审 r4 N3）。
_WORST_DELTA_ROUND_KEY = {
    "entity_macro_f1_worst_delta_pt": "entity_macro_f1",
    "raw_entity_macro_f1_worst_delta_pt": "raw_entity_macro_f1",
    "entity_strict_f1_worst_delta_pt": "entity_strict_f1",
    "relation_f1_worst_delta_pt": "relation_f1",
    "raw_relation_f1_worst_delta_pt": "raw_relation_f1",
    "relation_covered_recall_worst_delta_pt": "relation_covered_recall",
    "relation_covered_precision_worst_delta_pt": "relation_covered_precision",
}


def _round_aligned_worst_delta(
    baseline_rounds: list[dict[str, float]],
    candidate_rounds: list[dict[str, float]],
    key: str,
) -> float:
    """逐轮对齐后最坏的一轮差（百分点）；两档轮数必须一致。"""
    if len(baseline_rounds) != len(candidate_rounds):
        raise ValueError(
            "baseline_rounds 与 candidate_rounds 轮数不一致: "
            f"{len(baseline_rounds)} vs {len(candidate_rounds)}")
    if not baseline_rounds:
        raise ValueError("rounds 不能为空")
    deltas = [
        round((float(cand[key]) - float(base[key])) * 100, 4)
        for base, cand in zip(baseline_rounds, candidate_rounds)
    ]
    return min(deltas)


def evaluate_budget_gate(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    *,
    max_entity_drop_pt: float = MAX_ENTITY_F1_DROP_PT,
    baseline_rounds: list[dict[str, float]] | None = None,
    candidate_rounds: list[dict[str, float]] | None = None,
) -> dict[str, Any]:
    """按工单 04 的放量门槛判定候选块预算能否切换默认值。

    放行条件（全部满足）：
    * 实体 **原始未归一化** macro-F1 的 mean/worst 跌幅 ≤0.5pt（验收口径）；
    * 实体 **类型前缀标签归一化后** macro-F1 的 mean/worst 跌幅 ≤0.5pt
      （辅助口径；两套都进 ``checks``，评审 r4 B1）；
    * 实体 **strict** F1 的 mean/worst 跌幅 ≤0.5pt（评审 r6 F3）；
    * 候选档真实 tokenizer 的 ``max_input + max_output <= context_window``
      （验收 #5；能力缺失或 usage 计数 0/0 时 fail closed，评审 r6 F1 +
      r7 S1）；
    * **全量关系 F1 的 raw（验收）与 normalized（辅助）两套 mean/worst
      都不下降**（工单验收 2 的原始判据，F2 + 评审 r5 S1）；
    * 关系「金标覆盖子集」recall 的 mean/worst 不下降；
    * 关系「金标覆盖子集」precision 的 mean/worst 跌幅 ≤0.5pt；
    * 候选档截断块为 0；
    * real-doc 批次非空且最大文档 units 严格超过候选预算（验收 #4；评审
      r7 S2，空批次/短桩直接 fail）；
    * overlap 配置不变（冻结基线，不接受调用方自证）且**多块文档**中实际
      仍有重叠（``overlap_observed`` 只是 >0 冒烟检查；单块文档没有可重叠
      相邻对，不参与最小值，但整批没有多块文档时 fail closed；压力语料并非
      每对相邻块都重叠，逐档实际对数/可重叠对数由 A/B 工具的
      ``overlap_observed`` 落盘）；
    * 单文档调用数下降；
    * 两档预算必须不同（评审 r4 N3：1500-vs-1500 不能 passed）。

    覆盖子集与全量口径**并列**：F2 修复后，全量 F1 也进 ``checks``，
    不再出现“全量 F1 掉 7pt 仍 passed”的情况。

    两轴口径不同（评审 r2 F7）：实体 macro-F1 与全量关系 F1 是“逐篇先算
    F1、再跨篇/跨轮平均”的 macro；覆盖子集是“TP/FP/FN 跨篇池化”的 micro。
    ``caliber`` 字段显式标注，报告主表不能把两列当成同一口径比较。

    ``candidate`` 如显式提供 ``*_worst_delta_pt``，必须与
    ``baseline_rounds`` / ``candidate_rounds`` 的逐轮对齐差一致；二者都提供
    时由 gate 复算并校验，防止调用方用两档各自最差值相减糊弄（评审 r4 N3）。
    未提供 explicit 值时也用 rounds 复算；**没有 rounds 时 ``*_worst``
    一律报错**，不再退回两档各自最差值相减（评审 r6 F2）。门槛参数只能
    收紧，不能放宽（``max_entity_drop_pt > 0.5`` 直接报错）。
    """
    if max_entity_drop_pt > MAX_ENTITY_F1_DROP_PT:
        raise ValueError(
            f"max_entity_drop_pt={max_entity_drop_pt} 超过硬上限 "
            f"{MAX_ENTITY_F1_DROP_PT}；门槛不可放宽")
    if (baseline_rounds is None) != (candidate_rounds is None):
        raise ValueError(
            "baseline_rounds 与 candidate_rounds 必须同时提供或同时省略")
    missing = [key for key in _REQUIRED_GATE_KEYS
               if key not in baseline or key not in candidate]
    missing += [key for key in _REQUIRED_CANDIDATE_KEYS
                if key not in candidate]
    if missing:
        raise ValueError(f"gate 缺少字段: {sorted(set(missing))}")

    def _pt(before: float, after: float) -> float:
        return round((after - before) * 100, 4)

    def _delta(key: str, explicit_key: str | None = None) -> float:
        if explicit_key:
            # 评审 r6 F2：``*_worst`` 必须用逐轮对齐差。没有 rounds 时
            # 退回“两档各自最差值相减”会系统性偏松（实测可差 4pt），
            # 与 docstring 的契约相反，直接报错而不是静默放行。
            if baseline_rounds is None:
                raise ValueError(
                    f"{explicit_key} 需要 baseline_rounds 与 candidate_rounds "
                    "逐轮对齐；无 rounds 时不允许用两档各自最差值相减")
            computed = _round_aligned_worst_delta(
                baseline_rounds, candidate_rounds,
                _WORST_DELTA_ROUND_KEY[explicit_key])
            explicit = candidate.get(explicit_key)
            if explicit is not None and abs(
                    round(float(explicit), 4) - computed) > 0.0001:
                raise ValueError(
                    f"{explicit_key}={explicit} 与 rounds 逐轮对齐复算值 "
                    f"{computed} 不一致；gate 不接受调用方自证")
            return computed
        return _pt(baseline[key], candidate[key])

    entity_mean_delta = _delta("entity_macro_f1_mean")
    entity_worst_delta = _delta(
        "entity_macro_f1_worst", "entity_macro_f1_worst_delta_pt")
    raw_entity_mean_delta = _delta("raw_entity_macro_f1_mean")
    raw_entity_worst_delta = _delta(
        "raw_entity_macro_f1_worst", "raw_entity_macro_f1_worst_delta_pt")
    strict_entity_mean_delta = _delta("entity_strict_f1_mean")
    strict_entity_worst_delta = _delta(
        "entity_strict_f1_worst", "entity_strict_f1_worst_delta_pt")
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
    raw_full_mean_delta = _delta("raw_relation_f1_mean")
    raw_full_worst_delta = _delta(
        "raw_relation_f1_worst", "raw_relation_f1_worst_delta_pt")

    def _positive_number(value: Any) -> bool:
        """有限正数才算“拿到了 provider 计数”（bool 不算）。"""
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
            and value > 0
        )

    overlap_unchanged = (
        candidate["overlap_units"] == baseline["overlap_units"])
    # 评审 r7 N3：单块文档没有“可重叠的相邻对”，不能把它的 0 误判成
    # overlap 行为坏了；只统计有多块文档的最小实际重叠对数。若一篇多块
    # 文档都没有，则无法观测 overlap，fail closed。
    overlap_documents = candidate.get("overlap_documents_with_multiple_chunks")
    overlap_observed = (
        isinstance(overlap_documents, int)
        and not isinstance(overlap_documents, bool)
        and overlap_documents > 0
        and int(candidate["overlap_pairs_min"]) > 0
    )
    # 评审 r6 F1 + r7 S1：验收 #5 要求真实 tokenizer 口径的上下文余量；
    # 这里把 max_input+max_output<=context 作为放行 check。模型能力缺失
    # （None/非数）或 provider usage 缺失（0/0 tokens）时都必须 fail closed，
    # 不能因为“加到 0 也小于 context”就静默放行。
    context_window = candidate.get("context_window")
    max_context_input = candidate.get("max_context_input_tokens")
    max_context_output = candidate.get("max_context_output_tokens")
    context_within_window = (
        _positive_number(context_window)
        and _positive_number(max_context_input)
        and _positive_number(max_context_output)
        and (max_context_input + max_context_output) <= context_window
    )
    # 评审 r7 S2：验收 #4 的 real-doc 批次必须非空且真的包含超过候选预算的
    # 长文；只把 real_doc_coverage 写进 gate 报告会让空批次仍然 passed。
    real_doc_documents = candidate.get("real_doc_documents")
    real_doc_max_document_units = candidate.get("real_doc_max_document_units")
    real_doc_coverage_exercises_budget = (
        isinstance(real_doc_documents, int)
        and not isinstance(real_doc_documents, bool)
        and real_doc_documents > 0
        and _positive_number(real_doc_max_document_units)
        and float(real_doc_max_document_units) > float(candidate["budget"])
    )
    checks = {
        "entity_raw_mean_within_0_5pt": (
            raw_entity_mean_delta >= -max_entity_drop_pt),
        "entity_raw_worst_within_0_5pt": (
            raw_entity_worst_delta >= -max_entity_drop_pt),
        "entity_normalized_mean_within_0_5pt": (
            entity_mean_delta >= -max_entity_drop_pt),
        "entity_normalized_worst_within_0_5pt": (
            entity_worst_delta >= -max_entity_drop_pt),
        "entity_strict_mean_within_0_5pt": (
            strict_entity_mean_delta >= -max_entity_drop_pt),
        "entity_strict_worst_within_0_5pt": (
            strict_entity_worst_delta >= -max_entity_drop_pt),
        "context_within_window": context_within_window,
        "relation_full_f1_mean_not_lower": full_mean_delta >= 0.0,
        "relation_full_f1_worst_not_lower": full_worst_delta >= 0.0,
        "relation_raw_full_f1_mean_not_lower": raw_full_mean_delta >= 0.0,
        "relation_raw_full_f1_worst_not_lower": raw_full_worst_delta >= 0.0,
        "relation_covered_recall_mean_not_lower": covered_recall_mean_delta >= 0.0,
        "relation_covered_recall_worst_not_lower": covered_recall_worst_delta >= 0.0,
        "relation_covered_precision_mean_within_0_5pt": (
            covered_precision_mean_delta >= -MAX_RELATION_PRECISION_DROP_PT),
        "relation_covered_precision_worst_within_0_5pt": (
            covered_precision_worst_delta >= -MAX_RELATION_PRECISION_DROP_PT),
        "truncated_chunks_zero": int(candidate["truncated_chunks"]) == 0,
        # 评审 r7 S2：空 real-doc 批次或没有超过候选预算的长文都不能放行。
        "real_doc_coverage_exercises_budget": (
            real_doc_coverage_exercises_budget),
        "overlap_unchanged": overlap_unchanged,
        "overlap_observed": overlap_observed,
        "chunk_calls_decreased": (
            candidate["chunk_calls"] < baseline["chunk_calls"]),
        "budgets_differ": (
            baseline.get("budget") != candidate.get("budget")),
    }
    failures = [name for name, ok in checks.items() if not ok]
    return {
        "passed": not failures,
        "checks": checks,
        "failures": failures,
        "entity_macro_f1_mean_delta_pt": entity_mean_delta,
        "entity_macro_f1_worst_delta_pt": entity_worst_delta,
        # 验收口径（原始未归一化）：与 normalized 并列且同样进 checks（B1）。
        "raw_entity_macro_f1_mean_delta_pt": raw_entity_mean_delta,
        "raw_entity_macro_f1_worst_delta_pt": raw_entity_worst_delta,
        # 评审 r6 F3：strict 口径与 relaxed 并列落盘并进 checks。
        "entity_strict_f1_mean_delta_pt": strict_entity_mean_delta,
        "entity_strict_f1_worst_delta_pt": strict_entity_worst_delta,
        "context_window": context_window,
        "max_context_input_tokens": max_context_input,
        "max_context_output_tokens": max_context_output,
        # 评审 r7 S2：real-doc 覆盖判定所需的原始量也要落盘，便于复审复算。
        "real_doc_documents": real_doc_documents,
        "real_doc_max_document_units": real_doc_max_document_units,
        "real_doc_coverage_exercises_budget": (
            real_doc_coverage_exercises_budget),
        "relation_covered_recall_mean_delta_pt": covered_recall_mean_delta,
        "relation_covered_recall_worst_delta_pt": covered_recall_worst_delta,
        "relation_covered_precision_mean_delta_pt": covered_precision_mean_delta,
        "relation_covered_precision_worst_delta_pt": covered_precision_worst_delta,
        # 全量口径：与覆盖子集并列列报，并参与 checks（F2）。
        "relation_full_mean_delta_pt": full_mean_delta,
        "relation_full_worst_delta_pt": full_worst_delta,
        # 评审 r5 S1：全量关系 F1 也分 raw/normalized 双口径，均进 checks。
        "relation_raw_full_mean_delta_pt": raw_full_mean_delta,
        "relation_raw_full_worst_delta_pt": raw_full_worst_delta,
        # 评审 r4 B1：实体轴双口径显式标注，raw 是验收口径。
        "entity_caliber": {
            "acceptance": "raw（原始未归一化）macro-F1",
            "auxiliary": "normalized（类型前缀标签归一化后）macro-F1",
            "rule": ("raw 与 normalized 的 mean/worst 都进 checks，"
                     "任一跌幅超过 0.5pt 即 fail；辅助口径不能替代验收口径"),
        },
        # 评审 r5 S1：关系轴沿用同一规则，raw 不能只列报。
        "relation_caliber": {
            "acceptance": "raw（原始未归一化）全量关系 F1",
            "auxiliary": "normalized（类型前缀标签归一化后）全量关系 F1",
            "rule": ("raw 与 normalized 全量关系 F1 的 mean/worst 都进 "
                     "checks，任一低于基线即 fail；辅助口径不能替代验收口径"),
        },
        # 评审 r2 F7：两轴口径不同，报告不得按同一列直接比较。
        "caliber": {
            "entity_macro_f1": (
                "macro: 逐篇 F1 先平均，再跨轮平均"),
            "entity_strict_f1": (
                "strict: 比 mention.surface + span，不做类型前缀归一化；"
                "mean/worst 与 relaxed 同用 0.5pt 门槛（评审 r6 F3）"),
            "relation_full_f1": (
                "macro: 逐篇 F1 先平均，再跨轮平均"),
            "relation_covered_prf": (
                "micro: TP/FP/FN 跨篇池化后再跨轮平均"),
            "context_margin": (
                "provider 真实 tokenizer：max_chunk_input + max_chunk_output "
                "<= /models context_window；能力缺失或 usage 计数为 0 时 "
                "fail closed（评审 r7 S1）"),
            "real_doc_coverage": (
                "real-doc 批次必须非空且最大文档 units 超过候选预算；"
                "空批次或只有短桩时 fail（评审 r7 S2，验收 #4）"),
            "overlap_observed": (
                "只统计有多块文档的实际重叠对；单块文档不参与最小值，"
                "但没有任何多块文档时 fail closed（评审 r7 N3）"),
        },
        "overlap_unchanged": overlap_unchanged,
        "overlap_observed": overlap_observed,
        "overlap_documents_with_multiple_chunks": overlap_documents,
        "max_entity_drop_pt": max_entity_drop_pt,
        "max_relation_precision_drop_pt": MAX_RELATION_PRECISION_DROP_PT,
        "budgets": {
            "baseline": baseline.get("budget"),
            "candidate": candidate.get("budget"),
        },
        "rounds_used_for_explicit_deltas": baseline_rounds is not None,
    }
