# 工单 04 评审 r7 修复证据（S1 / S2 + N1–N3）

- 分支：`yzc/tmp-yzd-feat-relation-202610-04`
- 基线：`f4bdf33`（评审 r7 的 HEAD）；代码修复 `0f7efa7`；本轮**未重跑
  LLM**，gate/thresholds/safety/real-doc/overlap 由 `--from-json` 从既有
  逐轮/逐篇数据重算
- 默认值：仍是 `1500` / overlap `200`，**没有切换 commit**（验收 #2 仍未达成）

## 逐条处理

| 编号 | 处理 | 代码 / 测试 | 证据 |
|---|---|---|---|
| S1 should | `context_within_window` 在 token 计数 0/0 时 fail closed | `budget.py` 要求 context/input/output 都是有限正数；`_safety_margin.within_context` 同步 | `test_gate_fails_closed_when_token_counts_are_zero`、`..._output_token_count_is_missing`；`gate.failures` 无新增 |
| S2 should | `real_doc_coverage` 进 `gate.checks`，空批次/短桩不能放过验收 #4 | `_gate_input` 传 `real_doc_documents` / `real_doc_max_document_units`；gate 新增 `real_doc_coverage_exercises_budget` | `test_gate_fails_when_real_doc_batch_is_empty`、`..._does_not_exercise_candidate_budget`、`test_from_json_empty_real_doc_batch_fails_gate` |
| N1 nit | 删除 `index % len(cases)` 的静默重建；rows 与 cases 不能按轮次对齐时直接报错 | `_raw_entity_by_type_from_rows` | `test_raw_entity_by_type_rebuild_rejects_misaligned_rows`、`..._maps_aligned_rows_in_order` |
| N2 nit | 统一解释 strict mean 的 −0.54（gate）与 −0.534（逐轮对齐均值，CI 显示 −0.53） | `04-budget-ab.md` 主表注 + CI 表下说明 | `test_report_explains_strict_delta_rounding_caliber` |
| N3 nit | `overlap_pairs_min` 只统计多块文档；单块文档不再把 0 误判成失败；整批无多块文档仍 fail closed | `_run_corpus` / `_enrich_result_derived` 新字段 `overlap_documents_with_multiple_chunks`；gate 的 `overlap_observed` 读取该字段 | `test_gate_overlap_ignores_single_chunk_documents`、`..._fails_closed_when_no_multi_chunk_document`、`test_enrichment_excludes_single_chunk_docs_from_overlap_min` |

## 关键 JSON 数值（`docs/evidence/04-budget-ab.json`，`--from-json` 重生成）

```json
{
  "gate": {
    "passed": false,
    "checks": {
      "context_within_window": true,
      "real_doc_coverage_exercises_budget": true,
      "overlap_observed": true
    },
    "real_doc_documents": 12,
    "real_doc_max_document_units": 5360,
    "failures": [
      "entity_raw_mean_within_0_5pt",
      "entity_raw_worst_within_0_5pt",
      "entity_strict_mean_within_0_5pt",
      "entity_strict_worst_within_0_5pt",
      "relation_full_f1_mean_not_lower",
      "relation_full_f1_worst_not_lower",
      "relation_raw_full_f1_mean_not_lower",
      "relation_raw_full_f1_worst_not_lower",
      "relation_covered_precision_mean_within_0_5pt",
      "relation_covered_precision_worst_within_0_5pt"
    ]
  },
  "budgets": {
    "1500": {"overlap_documents_with_multiple_chunks": 3, "overlap_pairs_min": 8},
    "3500": {"overlap_documents_with_multiple_chunks": 3, "overlap_pairs_min": 7}
  },
  "real_doc_coverage": {
    "3500": {
      "documents": 12,
      "max_document_units": 5360,
      "max_chunk_units": 3483,
      "candidate_budget": 3500,
      "exercises_candidate_budget": true
    }
  },
  "delta_summary": {
    "entity_strict_f1": {"mean_pt": -0.534, "min_pt": -8.5}
  },
  "gate.entity_strict_f1_mean_delta_pt": -0.54
}
```

新增 check 后 `gate.checks` 从 20 条变为 21 条；10 条 failure 与 r6 完全一致
（验收 #2 仍未达成），没有因为新 check 误伤其它轴。

## 复跑

```bash
cd /home/dell/orca/workspaces/minirag/tmp-yzd-feat-relation-202610-04
ARK_API_KEY=dummy PYTHONPATH=src ../moray/.venv/bin/python -m pytest \
  tests/test_chunking.py tests/test_chunk_budget.py \
  tests/test_chunk_budget_r1_fixes.py tests/test_chunk_budget_r2_fixes.py \
  tests/test_chunk_budget_r4_fixes.py tests/test_chunk_budget_r5_fixes.py \
  tests/test_chunk_budget_r6_fixes.py tests/test_chunk_budget_r7_fixes.py -q
# 131 passed（见 04-r7-green-tests.txt）

ARK_API_KEY=dummy PYTHONPATH=src ../moray/.venv/bin/python -m pytest -q
# 581 passed / 66 skipped（见 04-r7-full-suite.txt）

PYTHONPATH=src ../moray/.venv/bin/python tools/chunk_budget_ab.py \
  --from-json docs/evidence/04-budget-ab.json \
  --json-out docs/evidence/04-budget-ab.json
```

红测试在 `f4bdf33` 上跑同一份 `test_chunk_budget_r7_fixes.py`：
**11 failed / 3 passed**（见 `04-r7-red-tests.txt`）；修复后 14 passed。

## 未改动的项

- 未改 `src/moray/extraction/chunking.py`；默认 `1500` / `overlap 200` 不变。
- 未扩 02 金标、未改验收口径；验收 #2 仍为“未达成（不切换）”。
- 未重跑 LLM；原始逐轮/逐篇预测面未改。
