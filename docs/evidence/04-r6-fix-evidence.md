# 工单 04 评审 r6 修复证据（F1–F4 + N1–N6）

- 分支：`yzc/tmp-yzd-feat-relation-202610-04`
- 基线：`9753ae3`（评审 r6 的 HEAD）；代码修复 `acac46f`；本轮**未重跑
  LLM**，gate/thresholds/safety/strict/raw-by-type 由 `--from-json` 从既有
  逐轮/逐篇数据重算
- 默认值：仍是 `1500` / overlap `200`，**没有切换 commit**（验收 #2 仍未达成）

## 逐条处理

| 编号 | 处理 | 代码 / 测试 | 证据 |
|---|---|---|---|
| F1 should | 上下文余量进 gate；能力缺失 fail closed | `budget.py::evaluate_budget_gate` 新增 `context_within_window`；`chunk_budget_ab.py::_gate_input` 传 `context_window`/`max_context_input_tokens`/`max_context_output_tokens` | JSON `gate.checks.context_within_window=true`；`test_gate_checks_context_headroom`、`test_gate_fails_closed_*` |
| F2 should | 无 rounds 时 `*_worst` 直接报错，不再两档各自最差值相减 | `budget.py::_delta` 对 `*_worst` 强制 rounds | `test_gate_rejects_worst_delta_without_rounds`、`..._explicit_worst_delta_without_rounds`；旧夹具全部改为 `gate_rounds()` |
| F3 should | strict 实体 F1 的 mean/worst 落盘并进 gate | `_run_corpus`/`_enrich_result_derived` 落 `entity_strict_f1_{mean,worst}`；gate 两条 check | JSON `entity_strict_f1_mean_delta_pt=-0.54`、`..._worst_delta_pt=-8.5`；`gate.failures` 含两条 strict |
| F4 should | raw 验收口径的按类型 P/R | `metrics.py::entity_type_counts`；逐篇 `raw_entity_by_type` + 跨轮聚合 | JSON `budgets.3500.raw_entity_by_type.EVENT={tp:0,fp:120,fn:120,...}`；报告新增 raw 表 |
| N1 nit | 复现命令的 venv 路径 | `04-r5-fix-evidence.md` 改为 `../moray/.venv/bin/python` | `test_r5_fix_evidence_uses_existing_venv_path` |
| N2 nit | 3,597 units 与 4,448 tokens 分属两批语料/两个块 | `04-budget-ab.md` 拆句 | `test_report_separates_estimated_units_from_real_tokens` |
| N3 nit | `min_possible_pairs` 改名说明是“比例最小那篇”的分母 | `_overlap_stats` → `min_ratio_document_possible_pairs` | `test_overlap_stats_names_denominator_as_min_ratio_document` |
| N4 nit | 删除只写不读的 overlap 死字段 | `_enrich_result_derived` 删除并清理旧 JSON | JSON 已无 `overlap_pairs_possible_min` / `overlap_pairs_ratio_min`；`test_enrichment_does_not_write_dead_overlap_fields` |
| N5 nit | 0.5 不再写死 | `gate_thresholds()` 复用 `MAX_ENTITY_F1_DROP_PT` / `MAX_RELATION_PRECISION_DROP_PT` | `test_gate_thresholds_reuse_budget_constants` |
| N6 nit | 不再从 2MB 证据读死数字 | r5 测试改内联 payload；保留 r4/r6 `--from-json` 冒烟 | `gate_fixtures.evidence_payload()` |

## 关键 JSON 数值（`docs/evidence/04-budget-ab.json`）

```json
{
  "gate": {
    "passed": false,
    "checks": 20,
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
    ],
    "entity_strict_f1_mean_delta_pt": -0.54,
    "entity_strict_f1_worst_delta_pt": -8.5,
    "context_window": 1048576,
    "max_context_input_tokens": 4448,
    "max_context_output_tokens": 5402
  },
  "delta_summary": {
    "entity_strict_f1": {
      "values": [-8.5, 0.6, 2.54, 1.85, 0.84],
      "mean_pt": -0.534,
      "min_pt": -8.5
    }
  },
  "raw_entity_by_type": {
    "1500": {"EVENT": {"tp": 0, "fp": 0, "fn": 120, "precision": 0.0, "recall": 0.0}},
    "3500": {"EVENT": {"tp": 0, "fp": 120, "fn": 120, "precision": 0.0, "recall": 0.0}}
  }
}
```

`raw_entity_by_type` 的 1500/3500 全表在
`budgets.<budget>.raw_entity_by_type`，报告里与 normalized 表并列。

## 复跑

```bash
cd /home/dell/orca/workspaces/minirag/tmp-yzd-feat-relation-202610-04
ARK_API_KEY=dummy PYTHONPATH=src ../moray/.venv/bin/python -m pytest \
  tests/test_chunking.py tests/test_chunk_budget.py \
  tests/test_chunk_budget_r1_fixes.py tests/test_chunk_budget_r2_fixes.py \
  tests/test_chunk_budget_r4_fixes.py tests/test_chunk_budget_r5_fixes.py \
  tests/test_chunk_budget_r6_fixes.py -q
# 113 passed（见 04-r6-green-tests.txt）
ARK_API_KEY=dummy PYTHONPATH=src ../moray/.venv/bin/python -m pytest -q
# 563 passed / 66 skipped（见 04-r6-full-suite.txt）
PYTHONPATH=src ../moray/.venv/bin/python tools/chunk_budget_ab.py \
  --from-json docs/evidence/04-budget-ab.json \
  --json-out docs/evidence/04-budget-ab.json
```

红测试在 `9753ae3` 的独立 worktree 上跑同一份 `test_chunk_budget_r6_fixes.py`：
**13 failed / 1 passed**（见 `04-r6-red-tests.txt`），修复后 113 passed。

## 未改动的项

- 未改 `src/moray/extraction/chunking.py`；默认 `1500` / `overlap 200` 不变。
- 未扩 02 金标、未改验收口径；验收 #2 仍为“未达成（不切换）”。
- 未重跑 LLM；原始逐轮/逐篇预测面未改。
