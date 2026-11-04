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
    （``raw_entity_macro_f1_*``）两套口径；两者都必填、都进 checks。
    """
    base = {
        "budget": 1500,
        "overlap_units": 200,
        "overlap_pairs_min": 8,
        "chunk_calls": 14,
        "truncated_chunks": 3,
        "entity_macro_f1_mean": 0.9000,
        "entity_macro_f1_worst": 0.8800,
        "raw_entity_macro_f1_mean": 0.9000,
        "raw_entity_macro_f1_worst": 0.8800,
        "relation_covered_recall_mean": 0.8000,
        "relation_covered_recall_worst": 0.7500,
        "relation_covered_precision_mean": 0.9000,
        "relation_covered_precision_worst": 0.8500,
        "relation_f1_mean": 0.5000,
        "relation_f1_worst": 0.4500,
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
        relation_covered_recall_mean=0.8000,
        relation_covered_recall_worst=0.7500,
        relation_covered_precision_mean=0.8950,   # -0.50pt 边界
        relation_covered_precision_worst=0.8450,
        relation_f1_mean=0.5000,
        relation_f1_worst=0.4500,
    )
    candidate.update(overrides)
    return candidate


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
    ({"truncated_chunks": 1}, "truncated_chunks_zero"),
    ({"overlap_units": 0}, "overlap_unchanged"),
    ({"overlap_pairs_min": 0}, "overlap_observed"),
    ({"chunk_calls": 14}, "chunk_calls_decreased"),
]
