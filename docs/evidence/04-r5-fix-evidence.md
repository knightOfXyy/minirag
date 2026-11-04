# 工单 04 评审 r5：6 条发现逐条处理结果

- 代码提交：`4698445`（本轮 r5 修复：`src/moray/evaluation/stress/{budget,metrics}.py`、
  `tools/chunk_budget_ab.py`、`tests/*`）
- A/B 证据：`docs/evidence/04-budget-ab.json` 的 gate/thresholds/overlap/coverage
  由 `--from-json` 从既有逐轮数据复算（未重跑 LLM），
  `gate_recompute` 记录源提交与重算环境
- 定向：**93 passed**（`test_chunking.py` + 5 个 chunk-budget 文件）；
  全量：**543 passed / 66 skipped**（`ARK_API_KEY=dummy`）
- 先红/后绿/全量：`04-r5-red-tests.txt`（9 failed）/ `04-r5-green-tests.txt` /
  `04-r5-full-suite.txt`
- 本报告只处理评审发现，没有顺手重构；验收 #2 仍按 owner 裁决如实保留
  “未通过（不切换）”，默认 1500/200、线上行为不变。

## 处理结果总表

| 编号 | 结论 | 代码/证据 |
|---|---|---|
| S1 should | **已修**：关系轴补 raw+normalized 双口径，raw 必填并进 checks；报告补原始行 | `budget.py::_REQUIRED_GATE_KEYS`/`checks`；`chunk_budget_ab.py::_gate_rounds`/`_derive_comparison`；JSON `gate.failures` 8 条、`relation_caliber` |
| S2 should | **已修**：报告改为“合成用例逐对；压力语料 1500 8/25、3500 7/8”；JSON 新增 `overlap_observed` | `chunk_budget_ab.py::_overlap_stats`/`_doc_row`；报告 overlap 节与验收 #1 |
| N1 nit | **已修**：docstring 改为“只对文档中实际出现过的声明类型回退” | `metrics.py::apply_type_label_aliases` |
| N2 nit | **已修**：`real_doc_coverage` 每档用自己的预算计算 | `chunk_budget_ab.py` live + `--from-json`；JSON `candidate_budget` 1500/3500 |
| N3 nit | **已修**：报告不再声称 JSON 含逐块文本明细 | 报告“原始数据”段与 r5 小节 |
| N4 nit | **已修**：两份手写 17 键 gate 输入收敛为 `_gate_input` | `chunk_budget_ab.py::_gate_input`；`test_gate_input_helper_fills_all_required_keys_from_one_place` |

## S1：关系轴 gate 只吃归一化口径（should）

旧实现只把归一化后的 `relation_f1_*` 放进 `checks`；原始未归一化关系 F1
虽在 `rounds[].raw_macro.relation.f1` 里，却从不参与放行。评审复算的原始
关系 F1 是 **0.7042 → 0.5954（mean −10.88pt）**，逐轮对齐 worst
**−19.28pt**；旧报告只写归一化的 −6.32/−16.33pt。

修复：

- `_REQUIRED_GATE_KEYS` 增加 `raw_relation_f1_mean/worst`，缺失直接
  `ValueError`（fail closed）；
- `checks` 增加
  `relation_raw_full_f1_mean_not_lower`、
  `relation_raw_full_f1_worst_not_lower`，与归一化两条并列；
- 输出 `relation_raw_full_{mean,worst}_delta_pt` 与 `relation_caliber`
  （acceptance=raw，auxiliary=normalized）；
- `_gate_rounds` 提供 `raw_relation_f1`，gate 用 rounds 复算并校验 explicit
  worst delta，不接受调用方自证；
- A/B 工具从 `rounds[].raw_macro.relation.f1` 落盘
  `raw_relation_f1_mean/worst` 与 `delta_summary.relation_raw_full`；
- `--from-json` 用 `_enrich_result_derived` 给旧 JSON 补齐同样的字段，
  不需要重跑 LLM。

本轮 JSON `gate.failures`（8 条）：

```text
entity_raw_mean_within_0_5pt                  # -2.92pt
entity_raw_worst_within_0_5pt                 # -5.44pt
relation_full_f1_mean_not_lower               # -6.32pt（归一化）
relation_full_f1_worst_not_lower              # -16.33pt（归一化）
relation_raw_full_f1_mean_not_lower           # -10.88pt（原始，本轮新增）
relation_raw_full_f1_worst_not_lower          # -19.28pt（原始，本轮新增）
relation_covered_precision_mean_within_0_5pt  # -12.28pt
relation_covered_precision_worst_within_0_5pt # -28.66pt
```

反例测试：
`test_gate_fails_when_raw_relation_drops_even_if_normalized_passes`
（归一化 +2pt、原始 −2pt 时 raw 两条 fail、`passed=false`）与
`test_gate_recomputes_raw_relation_worst_delta_from_rounds`（rounds 复算
−2.0pt）。报告主表、CI 表、逐轮数据均补原始关系行。

## S2：overlap 验收表述与压力语料不符（should）

旧报告/验收表写“相邻块逐对重叠 0<x≤200”，但按仓库 `Chunker` 复算：
1500 档每篇 26 块、实际重叠 **8/25** 对；3500 档每篇 9 块、实际重叠
**7/8** 对。长句（>200 units）会让 `overlap_seed` 为空，该对不会重叠。
配置与算法确实没改（`chunking.py` 不在 diff；合成用例逐对断言）。

修复：

- 报告与验收 #1 改为“**合成用例逐对断言；压力语料 1500 8/25、3500 7/8，
  长句致 seed 为空**”；
- `_doc_row` 每篇落盘 `overlap_pairs_possible=n_chunks-1`；
- JSON 新增顶层 `overlap_observed`：
  `baseline={min_overlap_pairs:8, min_possible_pairs:25, min_pair_ratio:0.32}`、
  `candidate={min_overlap_pairs:7, min_possible_pairs:8, min_pair_ratio:0.875}`，
  并明确 `gate.overlap_observed` 只是 `>0` 冒烟检查、不是逐对证明；
- gate 行为不变（仍只检查 `overlap_pairs_min > 0`），只是证据表述修正。

反例测试：`test_from_json_reports_overlap_pairs_over_possible`。

## N1：metrics docstring 过期

`apply_type_label_aliases` 的 docstring 仍写“端点不在实体列表时按
`TYPE_LABEL_PREFIXES` 全表回退”，与 r4 N6 的实现（只对文档内实际出现的
声明类型回退）矛盾。已改为“只对**文档中实际出现过的声明类型**回退，
不会按前缀表全表盲剥”；行为测试仍由
`test_relation_endpoint_fallback_does_not_strip_when_type_is_absent` 锁定。

## N2：real_doc_coverage 两档共用候选预算

旧 `main()` 对 1500/3500 都传 `candidate_budget=budgets[-1]`，导致 1500 档
的 note 也写“超过候选预算 3500”。现在 live 与 `--from-json` 都按
`candidate_budget=b` 计算；本轮 JSON：

```text
real_doc_coverage.1500.candidate_budget = 1500
real_doc_coverage.3500.candidate_budget = 3500
```

## N3：JSON “逐块明细” 名不副实

`cases[]` 只有逐篇聚合与 `max_chunk_*` 极值，没有逐块文本记录。报告
“原始数据”段与 r5 小节已改成“逐轮/逐篇聚合与单块极值；JSON 不含逐块
文本明细”。没有为了这句措辞额外落盘 52k 行之外的逐块数据。

## N4：两份 17 键 gate 输入重复

`_derive_comparison` 里 baseline/candidate 各手写一份 17 键 dict，新增
raw 关系口径要两处同步。已抽成
`_gate_input(result, *, budget, real, overlap_units)`，live 与
`--from-json` 共用；基线仍传冻结的 `DEFAULT_OVERLAP_UNITS=200`，候选传
实际配置值，语义不变。

## 未改动的项

- **验收 #2 仍为未通过（不切换）**：owner 裁决不扩 02 金标、不改口径；
  默认 `chunk_settings().max_units=1500`、`overlap_units=200` 不变，
  没有切换 commit。
- 未改 `src/moray/extraction/chunking.py`，未改 02/03 的评测入口。

## 复现

```bash
cd /home/dell/orca/workspaces/minirag/tmp-yzd-feat-relation-202610-04
ARK_API_KEY=dummy PYTHONPATH=src ../moray/.venv/bin/python -m pytest \
  tests/test_chunking.py tests/test_chunk_budget.py \
  tests/test_chunk_budget_r1_fixes.py tests/test_chunk_budget_r2_fixes.py \
  tests/test_chunk_budget_r4_fixes.py tests/test_chunk_budget_r5_fixes.py -q
# 93 passed
ARK_API_KEY=dummy PYTHONPATH=src ../moray/.venv/bin/python -m pytest -q
# 543 passed / 66 skipped
PYTHONPATH=src ../moray/.venv/bin/python tools/chunk_budget_ab.py \
  --from-json docs/evidence/04-budget-ab.json \
  --json-out docs/evidence/04-budget-ab.json
```
