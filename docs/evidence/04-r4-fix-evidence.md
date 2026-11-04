# 工单 04 评审 r4：11 条发现逐条处理结果

- 代码提交：`5036760`（本轮 r4 修复：`src/moray/evaluation/stress/{budget,metrics}.py`、
  `tools/chunk_budget_ab.py`、`tests/*`、`docs/DEPLOYMENT.md`）
- A/B 证据：`docs/evidence/04-budget-ab.json` 的 gate/thresholds 由
  `--from-json` 从既有逐轮数据复算（未重跑 LLM），`gate_recompute` 记录
  源提交与重算提交（`regenerator.git_sha=5036760`、`git_dirty=false`）
- 定向：**79 passed**（`test_chunking.py` + 4 个 chunk-budget 文件）；
  全量：**529 passed / 66 skipped**（`ARK_API_KEY=dummy`）
- 先红/后绿/全量：`04-r4-red-tests.txt` / `04-r4-green-tests.txt` /
  `04-r4-full-suite.txt`
- 本报告只处理评审发现，没有顺手重构；B2 按协调者裁决保留未达成，
  不扩 02 金标、不改验收口径。

## 处理结果总表

| 编号 | 结论 | 代码/证据 |
|---|---|---|
| B1 blocker | **已修**：gate 实体轴 raw + normalized 双口径 4 条 check，raw 字段必填 | `budget.py::evaluate_budget_gate`；`test_gate_includes_raw_and_normalized_entity_checks` 等；JSON `gate.failures` 含 2 条 raw |
| B2 blocker | **按 owner 裁决保留未达成**：不扩金标、不改口径、默认不切换 | 报告「验收逐条 #2」「需要 owner 决策的下一步」；JSON `gate.passed=false` |
| S1 should | **已修**：thresholds 写明全量 F1 参与放行；`--from-json` 重生成 | `gate_thresholds()`；`test_evidence_thresholds_describe_full_relation_f1_as_gating` |
| S2 should | **已修**：切换步骤写明 config + Chunker 两处默认 + 锁定测试 | `docs/DEPLOYMENT.md` 2c；`test_deployment_switch_steps_name_both_default_sites` |
| N1 nit | **已修**：删除无调用方 `relation_covered_prf` 与未用 `_max` | `metrics.py`、`stress/__init__.py`、`tools/chunk_budget_ab.py` |
| N2 nit | **已修**：gap 每关系算一次；A/B 复用 `evaluate_case` 返回的归一化副本 | `budget.py:relation_chunk_profile`；`chunk_budget_ab.py:_run_corpus` |
| N3 nit | **已修**：校验两档预算不同；gate 用 rounds 复算/校验 explicit worst delta | `budget.py`；`test_gate_rejects_same_budget_on_both_sides` 等 |
| N4 nit | **已修**：gate 夹具收敛到 `tests/gate_fixtures.py` 一份 | 三个既有测试文件改 import；新增 r4 测试同用 |
| N5 nit | **已修**：过期“全量只列报”注释随夹具重构移除 | `test_chunk_budget_r1_fixes.py` |
| N6 nit | **已修**：端点回退只对文档中实际出现的声明类型生效；补反向用例 | `metrics.py::_normalize_relation_endpoint`；`test_relation_endpoint_fallback_does_not_strip_when_type_is_absent` |
| N7 nit | **已披露，属数据面**：真实文档 11/12 为短桩；`--real-doc` 可传长文 | 报告真实文档批次；本轮未改代码 |

## B1：gate 实体轴必须双口径（blocker）

旧实现只把归一化后的 `entity_macro_f1_*` 放进 `checks`；`raw_entity_macro_f1_*`
虽在 JSON 里却从不参与放行。3500 档原始实体 −2.92pt（mean）/ −5.44pt
（逐轮对齐 worst）时，实体两条 check 仍全绿。

修复：

- `_REQUIRED_GATE_KEYS` 增加 `raw_entity_macro_f1_mean/worst`，缺失直接
  `ValueError`（fail closed）；
- `checks` 同时包含：
  `entity_raw_mean_within_0_5pt`、`entity_raw_worst_within_0_5pt`、
  `entity_normalized_mean_within_0_5pt`、
  `entity_normalized_worst_within_0_5pt`，任一超 0.5pt 即 fail；
- 输出 `raw_entity_macro_f1_*_delta_pt` 与 `entity_caliber`
  （acceptance=raw，auxiliary=normalized，两套都进 checks）。

本轮 JSON `gate.failures`（6 条）：

```text
entity_raw_mean_within_0_5pt                  # -2.92pt
entity_raw_worst_within_0_5pt                 # -5.44pt
relation_full_f1_mean_not_lower               # -6.32pt
relation_full_f1_worst_not_lower              # -16.33pt
relation_covered_precision_mean_within_0_5pt  # -12.28pt
relation_covered_precision_worst_within_0_5pt # -28.66pt
```

同时确认 3500 档 **1,080/2,858（37.79%）** 预测关系端点带 `案件编号 ` 前缀
（1500 档 0/2,159），属输出表面形态退化，不是评测噪声。

## B2：验收 #2 未达成（owner 裁决）

协调者裁决：本单**不扩 02 金标、不改验收口径**，结论就是不切换默认
（保持 1500）。报告已把验收 #2 如实标为“未通过（不切换）”，并把
“扩金标 / 正式改覆盖子集口径”写成另开工单的后续路径；本单不移动门槛。

## S1：证据 JSON 的 gate 描述

`gate_thresholds()` 现在写：

- `entity_macro_f1_note`：raw 与 normalized 都进 checks，任一 >0.5pt fail；
- `relation_full_scope`：全量口径 mean/worst **也参与放行（gate.checks）**，
  与覆盖子集并列列报。

新增 `--from-json`：从既有 JSON 的逐轮数据重算 gate/thresholds，不调 LLM；
JSON `gate_recompute` 记录源提交、源生成时间、重算提交与时间。单测
`test_from_json_regenerates_gate_without_llm` 跑真实文件验证。

## S2：默认值切换步骤

`docs/DEPLOYMENT.md` 2c 与报告同步改为三步：①`config.chunk_settings()` 的
`MORAY_CHUNK_MAX_UNITS` 默认；②`Chunker.__init__` 的 `max_units` 默认
（或让 Chunker 读 `chunk_settings()`）；③更新
`tests/test_chunk_budget.py::test_default_budget_and_overlap_are_unchanged`
期望值。overlap 仍 200；回滚 `git revert` 或删 env。

## N1：死代码

删除 `metrics.py::relation_covered_prf`（全仓无调用方，`ab1a321` 时不存在）
及其在 `stress/__init__.py`、`__all__` 的导出；删除
`tools/chunk_budget_ab.py::_max`（定义后从未调用）。

## N2：重复计算

- `relation_chunk_profile`：first-occurrence gap 在分支外算一次，residual 行与
  far-first 判定复用同一个值；
- `_run_corpus`：`evaluate_case` 已把归一化副本放进 `ev.pred`，工具层不再
  额外 `apply_type_label_aliases`，也不再逐实体调用
  `normalize_entity_name_aliases`；预测实体/关系用 `zip(raw, ev.pred)` 落盘。

## N3：gate 输入自证

- 新增 `budgets_differ` check：`baseline["budget"] == candidate["budget"]` 时
  `passed=False`；
- `evaluate_budget_gate(..., baseline_rounds=..., candidate_rounds=...)`：
  提供 rounds 时由 gate 逐轮对齐复算 `*_worst_delta_pt`；若调用方同时给了
  explicit 值且与复算不一致，直接 `ValueError`。A/B 工具已传 rounds。

## N4：夹具三份复制

新增 `tests/gate_fixtures.py`：唯一的 `gate_metrics()` / `passing_candidate()`
与 `GATE_FAILURE_CASES`（14 条，含新增 raw/normalized 实体 check）。三个既有
测试文件与 r4 新测试统一 import，不再各写一份阈值守卫。

## N5：过期注释

`test_chunk_budget_r1_fixes.py` 中“全量口径：只列报，不参与放行”的旧注释
随夹具重构删除；文件头改为当前判据（实体双口径 + 覆盖子集 + 全量 F1）。

## N6：关系端点全表回退

`_normalize_relation_endpoint` 的类型查不到时，只在
`TYPE_LABEL_PREFIXES` ∩ **文档中实际出现的实体类型** 上回退。文档没有 EVENT
实体时，`编号 X` 不会被剥成 `X`。新增反向用例
`test_relation_endpoint_fallback_does_not_strip_when_type_is_absent`
（PER 文档里 `编号 X` 保持原样，tp=0/fp=1/fn=1）；原 fallback 用例补上
EVENT 实体后继续验证“有声明类型时仍会剥”。A/B 语料中 1,080 条带前缀端点
全部出现在含 EVENT 实体的篇目，故本轮指标不受影响（已逐篇核对 JSON）。

## N7：真实文档覆盖

12 篇中 11 篇 ≤341 units 的事实已在报告披露；`--real-doc` 已能追加长文，
本轮使用 5,360-unit `docs/ARCHITECTURE.md` 压到 3500 档（2 块 / 最大
3,483 units / 截断 0）。更长的脱敏案卷属数据面，不阻塞本单。

## 复现

```bash
cd /home/dell/orca/workspaces/minirag/tmp-yzd-feat-relation-202610-04
ARK_API_KEY=dummy PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_chunking.py tests/test_chunk_budget.py \
  tests/test_chunk_budget_r1_fixes.py tests/test_chunk_budget_r2_fixes.py \
  tests/test_chunk_budget_r4_fixes.py -q     # 79 passed
ARK_API_KEY=dummy PYTHONPATH=src .venv/bin/python -m pytest -q  # 529/66
PYTHONPATH=src .venv/bin/python tools/chunk_budget_ab.py \
  --from-json docs/evidence/04-budget-ab.json \
  --json-out docs/evidence/04-budget-ab.json
```
