# 工单 04 评审 r9 修复证据

- 代码提交：`c98ee6b`（基于 r8 的 `7ebfcd1`）
- 评审基线：`d28b3c7`（r8 证据元数据提交）
- 未重跑 LLM；`04-budget-ab.json` 的 gate/thresholds 由
  `tools/chunk_budget_ab.py --from-json` 从既有逐轮/逐篇数据重生成。
- 先红：`04-r9-red-tests.txt`（新增 8 条用例在 `d28b3c7` 上全部失败）
- 后绿：`04-r9-green-tests.txt`（9 个预算相关测试文件 148 passed）
- 全量：`04-r9-full-suite.txt`（离线 604 passed / 66 skipped）

## S1：基线档失败块也进 gate

**问题**：gate 只检查候选档 `failed_chunks`。实测基线档
`failed_chunks=5` 时 gate 仍 `passed=true`；基线 F1 在缺块/半成品上计算，
“候选减基线”的相对判据不再可比。

**处理**：

- `_REQUIRED_GATE_KEYS` 增加 `failed_chunks`，两档都必填；
- `evaluate_budget_gate` 新增 `baseline_failed_chunks_zero` check，与候选档
  `failed_chunks_zero` 并列；
- gate 返回值新增 `baseline_failed_chunks`，便于复审复算。

**证据**：

```
tests/test_chunk_budget_r9_fixes.py::test_gate_fails_when_baseline_has_failed_chunks PASSED
tests/test_chunk_budget_r9_fixes.py::test_gate_requires_failed_chunks_on_baseline_too PASSED
```

实际证据 JSON：两档 `failed_chunks_max=0`、`real_docs.*.failed_chunks_total=0`，
`gate.checks.baseline_failed_chunks_zero=true`，`gate.failures` 仍为 10 条。

## S2：`--from-json` 失败证据缺失直接报错

**问题**：重建时 `row.get("failed_chunks", 0)` 把缺失静默当 0；删光失败证据
仍可 `passed=true`，与注释“缺失直接报错”相反。

**处理**：新增 `_failed_chunks_from_rows(rows, context=...)`，逐篇行缺少
`failed_chunks` 时抛 `ValueError`（带批次与行号）；压力语料
`_enrich_result_derived` 与真实文档 `_recompute_from_payload` 两条重建路径
共用。

**证据**：

```
tests/test_chunk_budget_r9_fixes.py::test_from_json_rejects_missing_failed_chunks_in_stress_rows PASSED
tests/test_chunk_budget_r9_fixes.py::test_from_json_rejects_missing_failed_chunks_in_real_rows PASSED
```

（两个用例均断言 `--from-json` 退出码非 0 且 stderr 含 `failed_chunks`。）

## N1：overlap 聚合只保留一份实现

**处理**：`_enrich_result_derived` 不再自己计算
`overlap_documents_with_multiple_chunks` / `overlap_pairs_min`，改为
`result.update(_overlap_aggregate(rounds))`；live 与 from-json 共用同一函数。

**证据**：

```
tests/test_chunk_budget_r9_fixes.py::test_enrichment_delegates_overlap_aggregation PASSED
```

## N2：空真实文档批次不再 KeyError

**处理**：`_run_real_docs([])` 显式返回 `failed_chunks_total=0`（并补齐
`truncation_signals_total` / `chunk_calls_total`）；`_gate_input` 不再抛
`KeyError`，空批次仍由 `real_doc_coverage_exercises_budget` fail closed。

**证据**：

```
tests/test_chunk_budget_r9_fixes.py::test_empty_real_docs_return_failed_chunks_total_zero PASSED
tests/test_chunk_budget_r9_fixes.py::test_gate_input_handles_empty_real_docs_without_keyerror PASSED
```

## 回归与范围

- 定向：9 个预算相关测试文件 **148 passed**；
- 全量：**604 passed / 66 skipped**；
- 未改默认预算（仍 1500）、未改 overlap（200）、未重跑 LLM、未提交
  `dev/yzd-feat-relation-202610` 或 `main`。
