"""工单 04 放行 gate 的共享测试夹具（评审 r4 N4）。

此前 ``test_chunk_budget.py``、``test_chunk_budget_r1_fixes.py``、
``test_chunk_budget_r2_fixes.py`` 各复制了一份 ``_metrics`` /
``_passing_candidate`` 和同一组 12 条阈值破坏用例；三份阈值守卫会漂移。
这里只保留**一份**，三个测试文件统一 import，新增/修改判据只改这里。
"""

from __future__ import annotations


def gate_metrics(**overrides):
    """基线档（1500）的完整 gate 输入。

    实体轴同时包含归一化（``entity_macro_f1_*``）与原始
    （``raw_entity_macro_f1_*``）两套口径；strict 与上下文余量也必填
    （评审 r6 F1/F3）。
    """
    base = {
        "budget": 1500,
        "overlap_units": 200,
        "overlap_pairs_min": 8,
        "chunk_calls": 14,
        "truncated_chunks": 3,
        "context_window": 1048576,
        "max_context_input_tokens": 2614,
        "max_context_output_tokens": 5482,
        "entity_macro_f1_mean": 0.9000,
        "entity_macro_f1_worst": 0.8800,
        "raw_entity_macro_f1_mean": 0.9000,
        "raw_entity_macro_f1_worst": 0.8800,
        "entity_strict_f1_mean": 0.9624,
        "entity_strict_f1_worst": 0.9600,
        "relation_covered_recall_mean": 0.8000,
        "relation_covered_recall_worst": 0.7500,
        "relation_covered_precision_mean": 0.9000,
        "relation_covered_precision_worst": 0.8500,
        "relation_f1_mean": 0.5000,
        "relation_f1_worst": 0.4500,
        "raw_relation_f1_mean": 0.5000,
        "raw_relation_f1_worst": 0.4500,
    }
    base.update(overrides)
    return base


def passing_candidate(**overrides):
    """候选档（3500）的“全部刚好达标”输入，用于逐条破坏一条阈值。"""
    candidate = gate_metrics(
        budget=3500,
        overlap_pairs_min=7,
        chunk_calls=6,
        truncated_chunks=0,
        entity_macro_f1_mean=0.8960,        # -0.40pt
        entity_macro_f1_worst=0.8760,       # -0.40pt
        raw_entity_macro_f1_mean=0.8960,    # -0.40pt
        raw_entity_macro_f1_worst=0.8760,   # -0.40pt
        entity_strict_f1_mean=0.9580,       # -0.44pt（刚好达标）
        entity_strict_f1_worst=0.9555,      # -0.45pt
        max_context_input_tokens=4448,
        max_context_output_tokens=5402,
        relation_covered_recall_mean=0.8000,
        relation_covered_recall_worst=0.7500,
        relation_covered_precision_mean=0.8950,   # -0.50pt 边界
        relation_covered_precision_worst=0.8450,
        relation_f1_mean=0.5000,
        relation_f1_worst=0.4500,
        raw_relation_f1_mean=0.5000,   # 原始验收口径：持平
        raw_relation_f1_worst=0.4500,
    )
    candidate.update(overrides)
    return candidate


# gate 复算 worst delta 的 rounds 需要的键（评审 r6 F2：无 rounds 必须报错，
# 不能退回两档各自最差值相减；r6 F3：strict 也进 checks）。
_ROUND_KEYS = {
    "entity_macro_f1": ("entity_macro_f1_mean", "entity_macro_f1_worst"),
    "raw_entity_macro_f1": (
        "raw_entity_macro_f1_mean", "raw_entity_macro_f1_worst"),
    "entity_strict_f1": (
        "entity_strict_f1_mean", "entity_strict_f1_worst"),
    "relation_f1": ("relation_f1_mean", "relation_f1_worst"),
    "raw_relation_f1": (
        "raw_relation_f1_mean", "raw_relation_f1_worst"),
    "relation_covered_recall": (
        "relation_covered_recall_mean", "relation_covered_recall_worst"),
    "relation_covered_precision": (
        "relation_covered_precision_mean",
        "relation_covered_precision_worst"),
}


def gate_rounds(metrics, *, n=5):
    """由 mean/worst 反推一组逐轮值，使 mean 与 min 精确等于输入。

    4 轮取 ``(n*mean-worst)/(n-1)``、1 轮取 worst；仅用于夹具，不代表真实
    采样。这样 round-aligned worst delta 就等于两档 worst 之差。
    """
    rounds = []
    for i in range(n):
        row = {}
        for key, (mean_key, worst_key) in _ROUND_KEYS.items():
            mean, worst = metrics[mean_key], metrics[worst_key]
            row[key] = (worst if i == n - 1
                        else round((n * mean - worst) / (n - 1), 6))
        rounds.append(row)
    return rounds


def evaluate_gate(baseline, candidate, **kwargs):
    """带 rounds 的 gate 调用；不再走无 rounds 的宽松路径。"""
    from moray.evaluation.stress.budget import evaluate_budget_gate

    if "baseline_rounds" not in kwargs:
        kwargs["baseline_rounds"] = gate_rounds(baseline)
        kwargs["candidate_rounds"] = gate_rounds(candidate)
    return evaluate_budget_gate(baseline, candidate, **kwargs)


def evidence_payload(*, candidate_overrides=None, overlap=None) -> dict:
    """最小可重算证据 JSON（不依赖 2MB 生成产物，评审 r6 N6）。

    ``--from-json`` 只读这里的逐轮/逐篇字段；``corpus.stress_path`` 故意
    指向不存在的路径，验证 rows 已持久化时不需要语料目录。
    """
    baseline = gate_metrics()
    candidate = passing_candidate(**(candidate_overrides or {}))
    overlap = overlap or {}
    budgets = {}
    for budget, metrics in ((1500, baseline), (3500, candidate)):
        rounds = []
        for index, values in enumerate(gate_rounds(metrics)):
            actual, possible = overlap.get(
                budget, (metrics["overlap_pairs_min"], 9))
            rounds.append({
                **values,
                "round": index + 1,
                "macro": {
                    "entity_relaxed": {"f1": values["entity_macro_f1"]},
                    "entity_strict": {"f1": values["entity_strict_f1"]},
                    "relation": {"f1": values["relation_f1"]},
                },
                "raw_macro": {
                    "entity_relaxed": {
                        "f1": values["raw_entity_macro_f1"]},
                    "relation": {"f1": values["raw_relation_f1"]},
                },
                "relation_covered_recall": values[
                    "relation_covered_recall"],
                "relation_covered_precision": values[
                    "relation_covered_precision"],
                "cases": [{
                    "n_chunks": possible + 1,
                    "overlap_pairs": actual,
                    "predicted_entities": [
                        {"type": "EVENT", "name": "案件编号 A2024-942"},
                        {"type": "PER", "name": "张三"},
                    ],
                    "raw_entity_by_type": {
                        "EVENT": {"tp": 0, "fp": 1, "fn": 1,
                                  "precision": 0.0, "recall": 0.0,
                                  "f1": 0.0},
                        "PER": {"tp": 1, "fp": 0, "fn": 0,
                                "precision": 1.0, "recall": 1.0, "f1": 1.0},
                    },
                }],
            })
        budgets[str(budget)] = {
            "budget": budget,
            "overlap_units": 200,
            "overlap_pairs_min": metrics["overlap_pairs_min"],
            "chunk_calls_total_mean": metrics["chunk_calls"],
            "truncated_chunks_max": metrics["truncated_chunks"],
            "max_chunk_input_tokens": metrics[
                "max_context_input_tokens"],
            "max_chunk_output_tokens": metrics[
                "max_context_output_tokens"],
            "entity_macro_f1_mean": metrics["entity_macro_f1_mean"],
            "entity_macro_f1_worst": metrics["entity_macro_f1_worst"],
            "raw_entity_macro_f1_mean": metrics[
                "raw_entity_macro_f1_mean"],
            "raw_entity_macro_f1_worst": metrics[
                "raw_entity_macro_f1_worst"],
            "entity_strict_f1_mean": metrics["entity_strict_f1_mean"],
            "entity_strict_f1_worst": metrics["entity_strict_f1_worst"],
            "relation_f1_mean": metrics["relation_f1_mean"],
            "relation_f1_worst": metrics["relation_f1_worst"],
            "raw_relation_f1_mean": metrics["raw_relation_f1_mean"],
            "raw_relation_f1_worst": metrics["raw_relation_f1_worst"],
            "relation_covered_recall_mean": metrics[
                "relation_covered_recall_mean"],
            "relation_covered_recall_worst": metrics[
                "relation_covered_recall_worst"],
            "relation_covered_precision_mean": metrics[
                "relation_covered_precision_mean"],
            "relation_covered_precision_worst": metrics[
                "relation_covered_precision_worst"],
            "raw_entity_by_type": {
                "EVENT": {"tp": 0, "fp": 1, "fn": 1,
                          "precision": 0.0, "recall": 0.0, "f1": 0.0},
                "PER": {"tp": 1, "fp": 0, "fn": 0,
                        "precision": 1.0, "recall": 1.0, "f1": 1.0},
            },
            "rounds": rounds,
        }
    return {
        "chunking": {"budgets": [1500, 3500]},
        "corpus": {"stress_path": "data/does-not-exist-r6"},
        "model_capabilities": {
            "context_window": 1048576,
            "max_output_tokens": 393216,
        },
        "budgets": budgets,
        "real_docs": {
            str(b): {
                "truncated_chunks_total": 0,
                "max_chunk_input_tokens": 100,
                "max_chunk_output_tokens": 20,
                "rows": [],
            }
            for b in (1500, 3500)
        },
    }


# (overrides, 预期失败的 check 名)；三个测试文件共用，防止守卫漂移。
GATE_FAILURE_CASES = [
    ({"entity_macro_f1_mean": 0.8949},
     "entity_normalized_mean_within_0_5pt"),
    ({"entity_macro_f1_worst": 0.8749},
     "entity_normalized_worst_within_0_5pt"),
    ({"raw_entity_macro_f1_mean": 0.8949},
     "entity_raw_mean_within_0_5pt"),
    ({"raw_entity_macro_f1_worst": 0.8749},
     "entity_raw_worst_within_0_5pt"),
    ({"relation_covered_recall_mean": 0.7999},
     "relation_covered_recall_mean_not_lower"),
    ({"relation_covered_recall_worst": 0.7499},
     "relation_covered_recall_worst_not_lower"),
    ({"relation_covered_precision_mean": 0.8949},
     "relation_covered_precision_mean_within_0_5pt"),
    ({"relation_covered_precision_worst": 0.8449},
     "relation_covered_precision_worst_within_0_5pt"),
    ({"relation_f1_mean": 0.4999}, "relation_full_f1_mean_not_lower"),
    ({"relation_f1_worst": 0.4499}, "relation_full_f1_worst_not_lower"),
    ({"raw_relation_f1_mean": 0.4999},
     "relation_raw_full_f1_mean_not_lower"),
    ({"raw_relation_f1_worst": 0.4499},
     "relation_raw_full_f1_worst_not_lower"),
    ({"entity_strict_f1_mean": 0.9573},
     "entity_strict_mean_within_0_5pt"),
    ({"entity_strict_f1_worst": 0.9549},
     "entity_strict_worst_within_0_5pt"),
    ({"max_context_input_tokens": 1048576},
     "context_within_window"),
    ({"truncated_chunks": 1}, "truncated_chunks_zero"),
    ({"overlap_units": 0}, "overlap_unchanged"),
    ({"overlap_pairs_min": 0}, "overlap_observed"),
    ({"chunk_calls": 14}, "chunk_calls_decreased"),
]
