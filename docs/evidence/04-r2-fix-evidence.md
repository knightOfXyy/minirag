# 工单 04 评审 r2：13 条发现逐条处理结果

- 修复提交：`e78dd35`（代码/测试/判据）
- A/B 证据：`git_sha=e78dd35`、`git_dirty=false`，5 轮独立重复，
  本次运行额外传入 1 篇 5,360-unit 真实长文
- 测试：`tests/test_chunk_budget_r2_fixes.py`（新增）+ 既有
  `tests/test_chunk_budget.py` / `tests/test_chunk_budget_r1_fixes.py` 同步新判据
- 定向：**58 passed**；全量：**514 passed / 66 skipped**
- 先红/后绿/全量：`04-r2-red-tests.txt` / `04-r2-green-tests.txt` /
  `04-r2-full-suite.txt`
- 本报告只处理评审发现，没有顺手重构或扩大改动面；复审由协调者另派
  全新上下文的 reviewer 执行。

## 处理结果总表

| 编号 | 结论 | 代码/证据 |
|---|---|---|
| F1 blocker | **已修**：关系头/尾按实体类型前缀归一化（含全表回退）；重跑 A/B | `stress/metrics.py::apply_type_label_aliases`；`test_relation_endpoints_*`；JSON 1,080/2,858 条错配可审计 |
| F2 blocker | **已修**：全量关系 F1 mean/worst 加回 `checks`；覆盖子集继续并列 | `stress/budget.py`；`test_gate_fails_when_full_relation_f1_drops_*`；JSON `gate.checks.relation_full_f1_*`（本轮 false） |
| F3 should | **已修**：`--real-doc` 可追加长文；本轮压到 3500 档并如实标注 | `tools/chunk_budget_ab.py::_load_real_docs`；`test_load_real_docs_accepts_extra_long_document`；JSON `real_doc_coverage.exercises_candidate_budget=true` |
| F4 should | **已修**：归一化改 opt-in，默认 False；02/03 口径不变 | `evaluate_case(..., normalize_type_labels=False)`；`test_evaluate_case_default_keeps_legacy_raw_caliber` |
| F5 should | **报告口径已改**：归一化定位为辅助口径，不写“通过”；owner 决策待定 | `04-budget-ab.md` 结论 2 / 实体轴 / 验收 #2 / 文末决策 3 |
| F6 should | **已修**：逐篇块按序号归属，重名不覆盖 | `_chunk_cases` + `enumerate`；`test_chunk_cases_does_not_collide_on_duplicate_names` |
| F7 should | **已修**：gate 显式标注 macro/micro；报告加口径警告与 macro 复算 | JSON `gate.caliber`；`test_gate_reports_macro_and_micro_calibers`；报告主表后说明 |
| F8 should | **已修**：单测改成 pred≠gold，能真正区分“只剥匹配类型” | `test_type_label_prefix_only_strips_labels_of_matching_type`；新增直测 |
| F9 nit | **已修**：不再同义反复，断言 provider usage 来源与固定减法 | `test_token_estimation_is_not_mistaken_for_real_tokenizer` 调用 `_safety_margin` |
| F10 nit | **已修**：测试文件头同步新判据（全量 F1 + 覆盖子集） | `tests/test_chunk_budget.py` 文件头 |
| F11 nit | **已修**：删无用必填键、`_delta` 收成单 key、`occurrences` 提模块、远距样例给总数/截断标记 | `stress/_text.py`；`_REQUIRED_CANDIDATE_KEYS`；`far_first_occurrence_examples_*`；`test_relation_chunk_profile_reports_far_example_truncation` |
| F12 nit | **已修**：墙钟写“未改善”；“6 篇黄金集短样本”；真实文档覆盖面改写 | `04-budget-ab.md` 结论 4 / 截断护栏 / 验收 #3 |
| F13 nit | **已修**：跨句 gold 关系显式计数（本轮 0） | `relation_covered_stats` + `uncovered_gold_relations`；`test_cross_sentence_gold_relations_are_reported_as_uncovered` |

## F1：关系端点归一化与重跑后的关系指标

旧实现只在 `clone.entities` 上剥前缀，`relations` 原样保留；3500 档模型把
案号写成 `案件编号 A2024-942`，关系尾也带前缀，于是永远匹配不上 gold
（`A2024-942`），既进不了 TP 又进 FP。

修复：

- `apply_type_label_aliases` 同时遍历 `clone.relations` 的 `head/tail`；
- 端点能查到实体类型时按该类型的 `TYPE_LABEL_PREFIXES` 剥；
- 查不到类型时按前缀表全表回退（评审给出的最小修法）；
- strict 仍比 `mention.surface`，不受影响。

证据：

```text
3500 档 predicted_relations: 2,858 条
其中原始 head/tail 带「案件编号」: 1,080 条（37.79%）
修复后 normalized_head/normalized_tail 与原始不同: 1,080 条
1500 档: 0 / 2,159 条
示例: {"head":"宋思远","predicate":"涉案关联",
       "tail":"案件编号 A2024-942",
       "normalized_tail":"A2024-942"}
```

重跑后的关系轴（5 轮）：

```text
覆盖子集 recall:  1500 0.7731 → 3500 0.8561（+8.30pt mean / +2.63pt worst）
覆盖子集 precision: 1500 0.6353 → 3500 0.5125（−12.28pt / −28.66pt）
全量关系 F1:     1500 0.7042 → 3500 0.6410（−6.32pt / −16.33pt）
```

即：评审指出的“recall 假失败”已消除；precision 与全量 F1 的下降仍真实存在。

## F2：全量关系 F1 回到 gate

`evaluate_budget_gate` 新增两条 check：

```text
relation_full_f1_mean_not_lower
relation_full_f1_worst_not_lower
```

`_REQUIRED_GATE_KEYS` 同步要求 `relation_f1_mean/worst`；单测：

- `test_gate_fails_when_full_relation_f1_drops_even_if_covered_subset_passes`
  ——覆盖子集全过、全量 F1 −7pt 时 `passed=False`；
- `test_gate_requires_full_relation_f1_fields` ——缺字段直接报错；
- `test_gate_passes_only_when_full_relation_f1_also_holds` ——全量 F1 持平才过。

本轮 JSON `gate.failures`：

```text
relation_full_f1_mean_not_lower             # -6.32pt
relation_full_f1_worst_not_lower            # -16.33pt
relation_covered_precision_mean_within_0_5pt   # -12.28pt
relation_covered_precision_worst_within_0_5pt  # -28.66pt
```

覆盖子集 recall、实体归一化、截断、overlap、调用数均通过；默认仍 1500。

## F3：真实长文批次

新增 `--real-doc`（可重复）：

```bash
--real-doc docs/ARCHITECTURE.md
```

本轮真实文档 12 篇。长文 5,360 units：

```text
1500 档: 4 块，最大块 1,497 units，截断 0
3500 档: 2 块，最大块 3,483 units，截断 0
real_doc_coverage.exercises_candidate_budget = true
```

报告把“6 篇干净正文”改为“6 篇黄金集短样本”，并说明该长文是仓库技术文档、
不是案件卷宗；后续可用 `--real-doc` 传脱敏案卷。单测
`test_load_real_docs_accepts_extra_long_document` 与
`test_real_doc_coverage_marks_long_document_as_exercising_budget` 锁住。

## F4：归一化 opt-in

```python
evaluate_case(name, seed, pred, gold, text=None,
              normalize_type_labels: bool = False)
```

- 04 A/B 工具显式传 `normalize_type_labels=True`；
- `stress/runner.py`（02 网格）与 `tools/backfill_stress_ab.py`（03 回填）
  保持默认 False，未改调用；
- 单测 `test_evaluate_case_default_keeps_legacy_raw_caliber` 断言默认口径
  与底层 `evaluate()` 完全一致。

## F5 / F12：报告措辞

- 结论 2 改成“实体轴：辅助口径改善，但不写成验收通过”；
- 验收 #2 明确写原始口径 −2.92/−4.08 未过，归一化 +6.09/+3.36 只作辅助；
- 文末新增 owner 决策第 3 条：接受规则 / 生产侧归一化 / 扩金标三选一；
- 结论 4 拆成“调用/token 达成，墙钟未改善（+14.25%）”；
- “6 篇干净正文”→“6 篇黄金集短样本”；真实文档覆盖面按 12 篇长文重写。

## F6：重名文档

`_run_corpus` 不再构造 `{case.name: chunks}`，改为
`_chunk_cases(cases, ...)` 返回按序号的列表，`enumerate(cases)` 取块。
单测用两个同名、不同长度的文档断言块数分别 1 与 >1，互不覆盖。

## F7：口径标注

gate 返回：

```json
"caliber": {
  "entity_macro_f1": "macro: 逐篇 F1 先平均，再跨轮平均",
  "relation_full_f1": "macro: 逐篇 F1 先平均，再跨轮平均",
  "relation_covered_prf": "micro: TP/FP/FN 跨篇池化后再跨轮平均"
}
```

报告主表后说明两种口径不能直接比较，并给出覆盖子集的 macro 复算：
recall +8.30pt（worst +2.63）、precision −16.13pt（worst −28.67），
方向与 micro 一致。

## F8：不可能失败的用例

旧用例 pred/gold 都是 `案件编号中心/ORG`，实现即使乱剥 ORG 前缀也两边相等。
现在改成 pred=`案件编号中心` / gold=`中心`（均为 ORG），断言
`tp=0 / fp=1 / fn=1`；并新增直测：

```python
normalize_entity_name_aliases("案件编号中心", "ORG") == "案件编号中心"
normalize_entity_name_aliases("案件编号中心", "EVENT") == "中心"
```

## F9：tokenizer 自检

不再只断言 `estimate_tokens(text)==len(text)`；改为用固定输入调用
`_safety_margin`，断言：

- `source` 含 `provider usage`；
- `max_chunk_input_tokens` 取两批最大值；
- `input_margin_tokens = context - max_input`（固定 1000−150=850）；
- `input_plus_output_margin_tokens` 再减 output（820）。

## F10：测试文件头

`tests/test_chunk_budget.py` 文件头已改为当前判据：实体 ≤0.5pt、全量关系 F1
不降、覆盖子集 recall 不降 / precision ≤0.5pt、截断 0、overlap 不变、调用降。

## F11：死代码/重复

- `occurrences` 提取到 `src/moray/evaluation/stress/_text.py`，budget/metrics 共用；
- `_delta(key, explicit_key=None)` 收成单 key，所有调用不再重复传同一 key；
- 基线不再强制要求 `truncated_chunks`，只有候选档要求
  （`_REQUIRED_CANDIDATE_KEYS`）；
- 远距首现样例新增 `far_first_occurrence_examples_total` 与
  `..._truncated`；单测 21 条关系时断言样例 20 条、total 21、truncated true。

## F13：跨句 gold 关系

新增 `relation_covered_stats` 返回
`RelationCoverage(prf, covered_sentences, gold_relations_total,
uncovered_gold_relations)`；`CaseEvaluation` / `aggregate_cases` / A/B 行
都带 `uncovered_gold_relations`。本轮语料全部同句，JSON
`uncovered_gold_relations_total=0`；单测构造“头在第一句、尾在第四句”的 gold
关系，断言 `uncovered=1`，防止将来静默丢出 recall 分母。
