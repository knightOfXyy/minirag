# 工单 03 证据汇总：结构化输出瘦身 + mention/span 确定性本地回填

开关：环境变量 `MORAY_LOCAL_BACKFILL`（`1/true/yes/on` 打开），**默认关闭**。
关闭时抽取路径与结果口径与改造前逐字段一致（见 golden 快照测试与开关关闭用例）。

## 本轮（评审第 1 次修复）改了什么

| 评审发现 | 处理 | 证据 |
|---|---|---|
| **blocker** 把 grounding 写回内核 `Entity/Relation.confidence` | 回填不再写 confidence，保留输入值（瘦身模型缺省 0.0）；确定性信号只走 `BackfillReport`；质量中间件把「缺失 confidence」当未知而非低置信；full schema 下 specialists 规则④恢复有效，slim 的等价信号是 `relations_without_evidence` | `test_backfill_keeps_kernel_confidence_untouched`、`test_slim_output_without_confidence_is_not_counted_as_low_confidence`、`03-local-backfill-contract.json` 的 `kernel_confidence_note` |
| should：回填信号在 pipeline/provider/jobs/API 被丢 | 分块信号按块汇总进 `ExtractionOutput.backfill`；`ReviewedExtraction.backfill` 透传；provider 增加 `backfill_sink`；作业结果与 `/extract` 响应都返回 backfill | `test_pipeline_run_aggregates_backfill_signal_with_global_spans`、`test_provider_passes_backfill_sink`、`test_job_handler_result_and_output_carry_backfill`、`test_extract_api_response_carries_backfill` |
| should：验收 8 用规则抽取器自证、脚本未入库 | 新增可复现脚本 `tools/backfill_ab_eval.py`（`--mode llm` 真实模型 / `--mode rule` 仅离线冒烟并显式标注）；用真实 LLM 在仓库内黄金语料重跑 A/B | `03-ab-llm.json`、`03-ab-rule.json`、`03-stress-corpus-eval.txt` |
| should：验收 7「逐字段一致」空转、full schema 字段顺序变了 | 从改造前基线 `262319b` 导出 schema+提示词 golden，逐字节比对；恢复 full 字段顺序 `name/type/aliases/mentions/confidence/attributes` | `tests/golden/full_schema_262319b.json`、`test_full_schema_and_prompt_match_pre_change_golden` |
| should：重叠裁决 O(H²)，20k 字 4.5s | 用按字符 `bytearray` 做区间占用，`find`/切片赋值在 C 层完成；新增 20k 字性能回归 | `test_overlap_resolution_is_linearish_on_20k_text`（本机实测 6.8ms，断言 <1.5s） |
| should：分块接线测试绕过 `pipeline.run` | 旧用例改为真实调用 `pipeline.run`，同时验证信号汇总与全局 span | `test_chunked_pipeline_backfills_then_maps_to_global`、`test_pipeline_run_aggregates_backfill_signal_with_global_spans` |
| nit：歧义命中不占区间，短名漏进歧义串 | 歧义命中同样占住区间 | `test_ambiguous_hit_occupies_interval_so_substring_does_not_leak`、`test_non_prefix_substring_inside_longer_name_is_not_matched` |
| nit：最近一对证据 span 不可观测 | 候选里增加 `selected` 标记，可直接对上 `evidence_span` | `test_relation_evidence_marks_the_selected_nearest_pair` |
| nit：`unmatched_aliases` 把标准名当幻觉 | 只统计模型给的别名（`literal != name`） | `test_unmatched_aliases_never_counts_canonical_name_as_hallucination` |
| nit：候选 key 拼接碰撞 | key 改为 JSON 编码三元组 | `test_relation_evidence_candidate_key_does_not_collide` |
| nit：合并逻辑重复、关系证据首见丢证据 | 抽取共用 `merge_entity_metadata`；`naive_merge` 对重复三元组合并证据并取最大置信度 | `test_naive_merge_does_not_drop_later_relation_evidence` |
| nit：token 对比用手写 payload | 测试改为由真实 schema 模型 `model_validate(...).model_dump()` 生成 payload；证据改用真实 LLM `usage.output_tokens` | `test_slim_output_payload_is_at_least_30pct_smaller`、`03-ab-llm.json` |

## 稳定内核契约

- 回填只补 `mentions[].span` 与 `relations[].evidence_span`。
- **不改** `Entity.confidence` / `Relation.confidence`；瘦身模式模型不输出该字段时保持默认 `0.0`。
- 确定性质量信号（块级置信度、命中、未命中别名、歧义、关系证据）在 `ExtractionOutput.backfill` / API `backfill` / 作业结果 `backfill` 中旁路暴露。

实际请求/响应样例见 `03-local-backfill-contract.json`。

## 验收条目对照

1. **先红后绿（多实体/别名/多次出现/中英混排）**
   `test_multi_entity_alias_repeated_mixed_script_backfill`。
2. **N 次出现 → N mention，span 与原文切片 100% 一致**
   `test_n_occurrences_produce_n_mentions_with_exact_slices`；真实 LLM A/B 中 slim span 一致率 min=mean=1.0000（full 为 0.2000/0.6778）。
3. **前缀/子串最长匹配优先**
   `test_prefix_substring_longest_match_wins`、`test_non_prefix_substring_inside_longer_name_is_not_matched`。
4. **别名/标准名同实体；正文不存在的别名不产 mention 且计入信号**
   `test_canonical_and_alias_ground_to_same_entity_phantom_alias_signaled`、`test_unmatched_aliases_never_counts_canonical_name_as_hallucination`。
5. **同句共现回填证据；跨句/跨块留空不臆造；最近一对 + 全候选**
   `test_relation_evidence_same_sentence_backfilled`、
   `test_relation_evidence_cross_sentence_is_empty_not_fabricated`、
   `test_relation_evidence_not_fabricated_across_chunks`、
   `test_relation_evidence_nearest_pair_wins_and_all_kept`。
6. **同名歧义未归属、不写错实体**
   `test_same_name_different_type_is_ambiguous_and_unattributed`、
   `test_alias_ownership_conflict_is_ambiguous`、`test_duplicate_logical_entities_are_coalesced_not_ambiguous`。
7. **开关关闭逐字段一致 + 默认关**
   `test_full_schema_and_prompt_match_pre_change_golden`（对 262319b golden 逐字节）、
   `test_default_switch_off_is_identical_to_before`、
   `test_switch_off_does_not_backfill_model_fields_preserved`、
   `test_env_flag_enables_backfill_by_default`。
8. **实体 macro-F1 跌幅 ≤0.5pt、关系 F1 不下降；span 一致率不低于改造前**
   真实 LLM（`data/gold` 6 篇，DeepSeek）：实体 relaxed `Δ=+0.0000`、关系 `Δ=+0.0000`、实体 strict `+0.1667`；span 一致率 full `0.6778` → slim `1.0000`。见 `03-ab-llm.json`。
   （工单 02 的扩展压力语料在兄弟分支；本仓脚本可对任意 tagged 黄金目录复跑，合并后按同一命令复测压力集。）
9. **输出 token 显著下降（≥30%），截断不增加**
   同一真实 LLM A/B：output tokens `2011 → 953`（**−52.6%**），每篇均值 `335.17 → 158.83`；模型尝试次数 `6 → 6`（无新增重试/截断迹象）。见 `03-ab-llm.json`。
10. **纯本地确定性、每块正文只扫描一趟（线性）**
    `test_scan_is_single_linear_pass_independent_of_pattern_count`；
    另加 20k 字重叠裁决性能回归（旧 O(H²) 已消除）。

测试：`129 passed, 47 skipped`（skip 为需 PostgreSQL/嵌入模型的集成测试），
见 `03-pytest-output.txt`。
