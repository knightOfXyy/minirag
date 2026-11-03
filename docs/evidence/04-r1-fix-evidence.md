# 工单 04 评审 r1：11 条发现逐条处理结果

- 修复提交：`3bbc0c2`（代码/测试/判据）；证据/报告提交见分支最新提交
- A/B 证据采集提交：`3bbc0c2`（`git_dirty=false`，5 轮独立重复）
- 测试：`tests/test_chunk_budget_r1_fixes.py`（21 条新增）+ 既有
  `tests/test_chunk_budget.py` 加固；全量 **497 passed / 66 skipped**
- 先红/后绿/全量：`04-r1-red-tests.txt` / `04-r1-green-tests.txt` /
  `04-r1-full-suite.txt`

## 处理结果总表

| 编号 | 结论 | 代码/证据 |
|---|---|---|
| B1 | **按协调者 B 方案落地双口径；gate 仍未过 → 默认保持 1500** | `stress/budget.py` 新门槛；`04-budget-ab.md` 双口径；JSON `gate` |
| B2 | **已修**：类型前缀标签归一化；原始口径并列保留 | `stress/metrics.py`；`test_type_label_prefix_*`；报告「实体轴」 |
| S1 | **已修**：逐篇预测实体/关系面落盘 | `tools/chunk_budget_ab.py` 行字段 `predicted_entities/predicted_relations`；JSON 可直接审计 |
| S2 | **已修**：真实文档未压到 3500 的事实显式标注 | `_real_doc_coverage`；JSON `real_doc_coverage`；报告「截断护栏」 |
| S3 | **已修**：overlap 用冻结基线 200 + 实际重叠对数 | `DEFAULT_OVERLAP_UNITS` 冻结、`overlap_pairs_min`；`test_gate...overlap_*` |
| S5 | **已修**：5 轮独立重复 + 95% CI + unpaired 标签 | `--repeat 5`；`_delta_summary_pt`；JSON `delta_summary` |
| S4 | **已修**：`Iterable` 只消费一次，重名不覆盖 | `relation_chunk_profile` 先 `list()` 再按序号取块；`test_relation_chunk_profile_accepts_generator_and_duplicate_names` |
| N1 | **已修**：overlap 断言改为逐对、带长度上界 | `tests/test_chunk_budget.py` 两个用例 |
| N2 | **已修**：两种批次共用 `_doc_row` | `tools/chunk_budget_ab.py`；`test_doc_row_is_shared_by_stress_and_real_doc_paths` |
| N3 | **已修**：证据 JSON 由本次运行整份生成，无手改派生字段 | JSON `git_sha=3bbc0c2`、`git_dirty=false`；不再有 `derived_note` |
| N4 | **已修**：实体门槛只能收紧，放宽直接报错 | `evaluate_budget_gate` 校验；`test_gate_entity_threshold_cannot_be_loosened_by_caller` |

## B1：为什么 gate 仍不过，以及为什么没有切默认

协调者 r1 选择 B：关系判据改为「金标覆盖子集」——只统计 gold 标注覆盖到的
句子上的 TP/FP/FN，同时保留全量口径；放行要求是覆盖子集 recall 不降、
precision 不显著降（实现为跌幅 ≤0.5pt），加上实体归一化后 ≤0.5pt、截断 0、
overlap 不变。本轮 5 轮结果：

```
gate.passed = false
failures = [
  relation_covered_recall_mean_not_lower,        # -0.94pt
  relation_covered_recall_worst_not_lower,       # -2.34pt
  relation_covered_precision_mean_within_0_5pt,  # -13.91pt
  relation_covered_precision_worst_within_0_5pt, # -30.50pt
]
entity_macro_f1_* = +6.03pt / +5.24pt（通过）
truncated_chunks_zero / overlap_unchanged / overlap_observed / chunk_calls_decreased = true
```

原因（证据）：02 金标的“覆盖句”本身只标了部分三元组。3500 档全量 FP 1525
条中 **1515 条仍落在 gold 覆盖句内**；1500 档是 907/926。典型是卷四每案
一句列 5 名涉案人、gold 只记第 1 人：3500 把同句其余 4 人也读出，按覆盖子集
口径仍是 FP。也就是说，覆盖子集口径不能把“同句多标/少标”的缺口消掉。
按协调者规则，**默认保持 1500，未添加切换 commit**；报告把这条写成
“未通过（不切换）”，并建议单独开工单扩 02 金标或细化判据粒度。

## B2：归一化规则与原始口径

- 规则：`TYPE_LABEL_PREFIXES = {"EVENT": ("案件编号", "案号", "编号")}`；
  relaxed 匹配前剥前缀，strict 仍比 `mention.surface`（不能让别名抹平原文差异）。
- 单测：`test_type_label_prefix_is_normalized_for_relaxed_entity_match`、
  `test_type_label_prefix_does_not_change_strict_surface_match`、
  `test_type_label_prefix_only_strips_labels_of_matching_type`。
- 数字：归一化后 3500 实体 **+6.03pt / +5.24pt**；原始未归一化
  **−3.01pt / −3.78pt**，两口径都在报告主表。EVENT 从 P=0/R=0（120 FP +
  120 FN）变为 P=1/R=1。

## S1：预测面落盘样例

JSON 每条逐篇行现在都有（示例路径）：

```
budgets["1500"].rounds[0].cases[0].predicted_entities[] = {type, name, normalized_name}
budgets["1500"].rounds[0].cases[0].predicted_relations[] = {head, predicate, tail}
budgets["1500"].rounds[0].cases[0].relation_covered_prf = {tp,fp,fn,precision,recall,f1}
```

报告里的 FP 分布（`涉案关联` +565 等）就是从这些预测面 + gold 重算的，
不再只有聚合 PRF。

## S2：真实文档限制

11 篇真实文档最大块 **341 units**，两档都是 1 块，**没有压到 3500**。
JSON 的 `real_doc_coverage.<budget>` 给出
`exercises_candidate_budget=false` 和一条中文说明；报告「截断护栏」明确写
“本批次只验证 ≤1500-unit 文档”，不把它冒充成 3500 在真实长文上的截断证据。

## S3：overlap 冻结基线

`gate_baseline["overlap_units"] = DEFAULT_OVERLAP_UNITS (200)`（常量，不再取
`args.overlap_units`）；`gate_candidate["overlap_units"]` 来自实际运行；
另有 `overlap_pairs_min`（1500=8、3500=7，均 >0）。单测
`test_gate_fails_when_any_new_threshold_breaks[overlap_units=0]` 与
`[overlap_pairs_min=0]` 分别锁死“配置被换”和“没有实际重叠”。

## S5：独立重复与 CI

- `--repeat 5`（默认也改为 5）；两档先后独立跑、无 seed，标签明确
  `unpaired independent repeats`，不再叫“配对”。
- JSON `delta_summary` 每族给 `values / n / mean_pt / sd_pt / ci95_low_pt /
  ci95_high_pt / min_pt`；`min_pt` 才是 worst-of-N（round-aligned）。
- 关系覆盖子集 recall CI [−2.15, +0.28] 跨 0，但判据是“不下降”，点估计
  −0.94 已不满足；precision CI [−25.48, −2.33] 明显为负。

## S4：Iterable / 重名

旧实现先 `{case.name: chunks}` 再 `for case in cases`，生成器会被耗尽且
重名互相覆盖。现在 `case_list = list(cases)`，块按 `enumerate` 序号取；
`documents` 用 `len(case_list)`。单测用生成器 + 两个同名 case 断言
`documents=2 / total_relations=2 / co_chunk=1 / cross_chunk=1`。

## N1：overlap 断言

`tests/test_chunk_budget.py` 不再用 `any(...)` 或“首字符在上一块里”：
对**每一对**相邻块断言 `b.start > a.start`、`0 < a.end - b.start <= 200`，
并校验重叠切片与原文一致；两个用例（3500 档、1500 vs 3500）都改了。

## N2：行构造器去重

新增 `_doc_row(doc, chunks, out, wall_s)`，压力语料与真实文档两条路径都调用
它；单测断言公共字段集合存在。真实文档行现在也带 `cached_input_tokens` /
`reworked_chunks` / `max_chunk_chars`，字段不再两处漂移。

## N3：证据不可手改

本轮 JSON 由 `tools/chunk_budget_ab.py` 在 `3bbc0c2`、`git_dirty=false` 下
整份生成；`derived_note`、手加的 `paired_round_deltas_pt` 已移除，
worst/CI/全量差都在运行时计算。若后续只改报告，不改 JSON。

## N4：门槛不可放宽

`evaluate_budget_gate(..., max_entity_drop_pt=99)` 现在抛 `ValueError`
（> 0.5 直接拒绝）；单测
`test_gate_entity_threshold_cannot_be_loosened_by_caller` 锁住。
