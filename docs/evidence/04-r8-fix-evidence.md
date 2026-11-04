# 工单 04 评审 r8 修复证据（S1 + N1–N3）

- 分支：`yzc/tmp-yzd-feat-relation-202610-04`
- 基线：`0f7efa7`（评审 r8 的 HEAD）；代码修复 `7ebfcd1`；本轮**未重跑
  LLM**，gate/thresholds/safety/real-doc/overlap 由 `--from-json` 从既有
  逐轮/逐篇数据重算
- 默认值：仍是 `1500` / overlap `200`，**没有切换 commit**（验收 #2 仍未达成）

## 逐条处理

| 编号 | 处理 | 代码 / 测试 | 证据 |
|---|---|---|---|
| S1 should | 失败块进 gate：压力语料 `failed_chunks_max` + 真实文档 `failed_chunks_total` 合成候选档 `failed_chunks`，必须为 0；失败块不产生 finish_reason，不能靠 `truncated=0` / real-doc 覆盖间接放行 | `_gate_input` 新增 `failed_chunks`；`budget.py` 新增 `_REQUIRED_CANDIDATE_KEYS=("truncated_chunks","failed_chunks")` 与 `failed_chunks_zero`；`--from-json` 从旧证据逐篇行重建缺失字段；`gate_thresholds` / docstring / caliber 同步 | `test_gate_fails_when_candidate_has_failed_chunks`、`test_gate_passes_failed_chunks_check_when_zero`、`test_from_json_stress_corpus_failed_chunks_fails_gate`、`test_from_json_real_doc_failed_chunks_fails_gate`、`test_from_json_rebuilds_failed_chunks_from_old_evidence_rows`、`test_shared_failure_cases_cover_failed_chunks_check`；JSON `gate.checks.failed_chunks_zero=true`、`gate.failed_chunks=0`（stress 0 + real 0）、`gate.failures` 仍 10 条 |
| N1 nit | 报告头部不再引用不存在的 `9400e6d`，改为 r7 真实证据区间 `a7dbdfe`–`ff0f33c`；复现说明的“评审 r4”改为“评审 r4–r7”，并显式写明本轮代码 `7ebfcd1` | `docs/evidence/04-budget-ab.md:1-12`、`:70`、`## 评审 r8 修复` | `test_report_header_references_existing_evidence_commits`；`grep -n 9400e6d` 只在 r8 修复说明里作为“已删除的错误引用”出现，头部无 |
| N2 nit | `_overlap_stats` 的 `documents_with_multiple_chunks` 改名 `case_runs_with_multiple_chunks`（5 轮 × 3 篇 = 15 个 case-run），与 `budgets.<档>.overlap_documents_with_multiple_chunks`（每轮篇数 = 3）区分 | `tools/chunk_budget_ab.py::_overlap_stats`；报告 overlap 一节与 r8 修复节显式说明两个基数 | `test_overlap_stats_names_case_run_count_distinctly`、`test_overlap_stats_empty_result_uses_new_field_name`、`test_from_json_regenerates_failed_chunks_and_overlap_case_runs`；JSON `overlap_observed.{baseline,candidate}.case_runs_with_multiple_chunks=15`，`budgets.*.overlap_documents_with_multiple_chunks=3` 保留 |
| N3 nit | 可重叠对数收敛到 `_possible_pairs`（缺 `overlap_pairs_possible` 时按 `n_chunks-1` 回退，显式 0 保持权威）；`_overlap_stats`、`_enrich_result_derived`、live `_overlap_aggregate` 三处共用 | `tools/chunk_budget_ab.py` 新增 `_possible_pairs` / `_overlap_aggregate`；`_run_corpus` 改用 `_overlap_aggregate` | `test_possible_pairs_falls_back_to_n_chunks_minus_one`、`test_live_overlap_aggregate_uses_same_fallback_as_from_json`、`test_enrichment_and_live_aggregate_agree_without_possible_field` |

## 复跑结果

```text
# 先红（基线 0f7efa7 + 本轮新测试）
13 failed in 3.17s                         # docs/evidence/04-r8-red-tests.txt

# 后绿（定向 11 个文件）
164 passed, 2 skipped in 9.04s             # docs/evidence/04-r8-green-tests.txt

# 全量（离线，ARK_API_KEY=dummy）
596 passed, 66 skipped in 26.84s           # docs/evidence/04-r8-full-suite.txt
```

## `--from-json` 重生成核对

`--from-json` 重生成后与上一版 JSON 逐字段比对，差异只有本轮预期项：

- 新增 `gate.checks.failed_chunks_zero`、`gate.failed_chunks`、
  `gate.stress_failed_chunks`、`gate.real_docs_failed_chunks`、
  `gate.caliber.failed_chunks`、`thresholds.failed_chunks`；
- `overlap_observed.{baseline,candidate}.documents_with_multiple_chunks`
  改名为 `case_runs_with_multiple_chunks`（数值仍 15）；
- `gate_recompute` 的 `regenerated_at` / `regenerator` 元数据。

`gate.passed` 仍为 `false`、`gate.failures` 仍为原有 **10 条**（验收 #2
未达成：raw 实体 −2.92/−5.44pt、strict −0.54/−8.50pt、raw 关系
−10.88/−19.28pt、覆盖 precision −12.28/−28.66pt），本轮没有改口径、没有
扩金标、没有切换默认值。

## 未做

- 未重跑真实 LLM；未改 02 金标；未改验收 #2 的 owner 裁决；
- 未提交 dev/main，只提交本临时分支。
