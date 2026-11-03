# 工单 03 证据汇总：结构化输出瘦身 + mention/span 确定性本地回填

> 第 4 轮评审修复（缓存键含回填模式、整篇别名口径、验收 8 可判域重跑、
> N1/N4/N5/N6/N7）见 `03-r4-fix-evidence.md`；验收 8/9 以
> `03-r3-stress-ab.json` 的最新多轮重跑为准。本文件下方 r2 记录保留为历史。

开关：环境变量 `MORAY_LOCAL_BACKFILL`（`1/true/yes/on` 打开），**默认关闭**。
关闭时：full schema / 提示词 / 归并语义保持 262319b（schema+结果逐字段 golden）。
打开时：模型只出逻辑实体/关系，mention/span 与关系证据由本地 Aho-Corasick
一次线性扫描回填；稳定内核 `Entity/EntityMention/Span/Relation` 字段不变。

## 本轮（评审 r2 修复）逐条处理

| # | 评审发现 | 处理 | 证据 |
|---|---|---|---|
| 1 | **blocker** slim 下 confidence 恒 0.0 被当真、规则④永不触发 | `confidence` 改为可空：slim 缺失 = `None`，入库写 NULL（不冲掉已知值），查询/API 回 `null`，`/relations` 排序 `NULLS LAST`；规则④对「未知 + 无证据」也拦截；审校/看板消费 `block_confidence` | `test_slim_to_core_keeps_confidence_unknown`、`test_ingestor_passes_unknown_confidence_through_as_none`、`test_unknown_confidence_is_null_not_zero`、`test_relation_unknown_confidence_sorts_last`、`test_rule_review_flags_evidence_less_relation_when_confidence_unknown`、`03-local-backfill-contract.json` |
| 2 | should `_is_free` 语义反了，部分重叠短名漏进长名 | 改为整段全空闲才接受（`_interval_all_free`），部分重叠用例覆盖 | `test_partial_overlap_short_name_does_not_leak_into_long_name`、`test_partial_overlap_does_not_block_independent_occurrence` |
| 3 | should 英文句号不切句、开头空白句列表为空 | `split_sentences(ascii_period=True)` 供关系证据使用（不切 3.14）；开头空白不再返回空；证据 span 裁掉句首尾空白。默认分块路径只改变「开头是空白/结束符」这一原先返回 0 块的边界情形 | `test_relation_evidence_splits_ascii_period_sentences`、`test_relation_evidence_handles_document_starting_with_whitespace` |
| 4 | should 关闭开关仍改变归并输出 | `naive_merge(merge_relation_duplicates=...)`：pipeline 仅在开关打开时合并后见证据/置信度/description；关闭走 262319b 旧语义 | `test_switch_off_merge_matches_pre_change_full_field_golden`（golden 由 262319b 源码生成）、`test_chunked_pipeline_gates_relation_merge_by_switch` |
| 5 | should 验收 8 未在 02 压力语料验证、文件名误导 | 误导文件改名 `03-stress-corpus-eval.txt` → `03-baseline-ab.txt`；明确标注压力集 LLM 复跑**未闭环**（本分支无 02 loader、本机无 key），写清合入后的复跑命令 | `03-baseline-ab.txt`、`tools/backfill_ab_eval.py` docstring |
| 6 | should 验收 9 用 per-document 与 attempts 冒充 | 证据文件明确「验收 9 未闭环」，列出 01 chunk_metrics/截断口径与后续命令；不再声称达标 | `03-output-token-reduction.txt`、`03-baseline-ab.txt` |
| 7 | should 质量中间件 slim 恒 1.0、无下游消费回填 | 中间件对无 mention 的 slim 输出跑本地回填，用命中率替代空转 span 1.0；`ExtractionMetrics`/`RunRecorder`/`pipeline_runs` 记录 block_confidence/命中数/证据率/歧义数 | `test_slim_output_with_ungrounded_entity_lowers_quality_score`、`test_from_pipeline_carries_backfill_grounding_signal`、`test_summary_includes_backfill_signals` |
| 8 | nit 被长名吸收的别名误报幻觉 | 别名改用「正文中字面存在性」判定 | `test_unmatched_aliases_absorbed_by_longer_name_is_not_hallucination` |
| 9 | nit 信号键只用 name，同名异型覆盖 | 统计键/歧义候选改为 JSON `[type, name]`（`entity_signal_key`） | `test_signal_keys_keep_type_so_same_name_different_type_does_not_collide` |
| 10 | nit 跨块汇总仍是块内偏移、未命中关系堆叠 | 汇总按 `chunk_start` 平移并带 `chunk_id`；`relations_without_evidence` 按三元组去重并聚合 `chunk_ids` | `test_merged_report_shifts_block_offsets_and_tags_chunk` |
| 11 | nit 先红后绿不可核 | 本轮先提交失败用例（21 failed）再提交实现；red 输出入库 | `03-r2-red-tests.txt`（提交 `03a7c79` 先红） |
| 12 | nit 文档未记录开关/模块职责 | `docs/ARCHITECTURE.md` 新增瘦身回填章节与 `MORAY_LOCAL_BACKFILL`；`local_backfill.py` 头注释指路各职责 | `docs/ARCHITECTURE.md` |

## 稳定内核 / 确定性信号契约

- 回填只补 `mentions[].span` 与 `relations[].evidence_span`。
- **不改** `confidence` 的语义（模型自报）；但 slim 模型不自报时必须是
  `None`/NULL（未知），不能用 0.0 冒充。full schema 仍要求模型给 0~1。
- 确定性质量信号在 `ExtractionOutput.backfill` / API `backfill` / 作业结果
  `backfill` 旁路暴露：块级置信度、命中数、未命中别名、歧义、关系证据候选。
- 跨块汇总后的偏移是文档全局坐标，并带 `chunk_id`。

实际请求/响应样例见 `03-local-backfill-contract.json`。

## 验收条目逐条（工单 03）

| # | 条目 | 结论 | 依据 |
|---|---|---|---|
| 1 | 先红后绿 | **本轮可核**（历史 185fb41 仍不可追溯） | `03-r2-red-tests.txt`：21 failed → 实现后全绿 |
| 2 | N 次出现 → N mention、span 原文切片 | 达成 | `test_n_occurrences_produce_n_mentions_with_exact_slices`；slim span 1.0 |
| 3 | 前缀/子串最长匹配不错误覆盖 | 达成（含部分重叠） | `test_prefix_substring_longest_match_wins`、`test_partial_overlap_short_name_does_not_leak_into_long_name` |
| 4 | 别名/标准名同实体；幻觉别名计入信号 | 达成 | `test_canonical_and_alias_ground_to_same_entity_phantom_alias_signaled`、`test_unmatched_aliases_absorbed_by_longer_name_is_not_hallucination` |
| 5 | 同句共现回填证据；跨句/跨块不臆造 | 达成（中文/英文/开头空白） | `test_relation_evidence_splits_ascii_period_sentences`、`test_relation_evidence_handles_document_starting_with_whitespace`、`test_relation_evidence_not_fabricated_across_chunks` |
| 6 | 同名歧义未归属 | 达成 | `test_same_name_different_type_is_ambiguous_and_unattributed`、`test_alias_ownership_conflict_is_ambiguous` |
| 7 | 开关关闭逐字段一致 + 默认关 | **达成（口径已讲清）**：schema/prompt 对 262319b 逐字节；归并结果对 262319b 全字段 golden；默认关 | `test_full_schema_and_prompt_match_pre_change_golden`、`test_switch_off_merge_matches_pre_change_full_field_golden` |
| 8 | 02 扩展语料实体 F1 不跌、关系不降 | **未闭环**：本分支无 02 stress loader、本机无 LLM key；基线集真实 LLM A/B 保留在 `03-ab-llm.json`（非压力集） | `03-baseline-ab.txt` |
| 9 | 单块 output token ≥30% 降、截断不增 | **未闭环**：缺工单 01 chunk_metrics + LLM key；现有数字是 per-document 旁证，已明确标注 | `03-output-token-reduction.txt` |
| 10 | 纯本地确定性、每块只扫一遍（线性） | 达成 | `test_scan_is_single_linear_pass_independent_of_pattern_count`、20k 性能回归 |

## 测试

分支独立回归（仅 03，`1d4fbd7`/`8a0d750`）：

```bash
# 离线（无 DB；ARK_API_KEY=dummy 仅为让 get_settings() 通过，不发请求）
ARK_API_KEY=dummy PYTHONPATH=src pytest -q          # 148 passed, 50 skipped, 0 failed
# 含 pgvector/看板/入库/作业/合并/API 集成（本机 moray-pgvector 在 5433）
RUN_DB_TESTS=1 ARK_API_KEY=dummy PYTHONPATH=src pytest -q   # 198 passed, 0 failed
```

收敛到稳定分支后（03 × 01 × 05 × 02 全部用例同跑，本文件所述实现为准）：

```bash
ARK_API_KEY=dummy python3 -m pytest -q                      # 281 passed, 64 skipped
RUN_DB_TESTS=1 ARK_API_KEY=dummy python3 -m pytest -q       # 345 passed, 0 failed
```

原始输出：`03-pytest-output.txt`（本轮更新）。

> 收敛说明：临时实现（先红提交 `03a7c79` 之后稳定分支自补的 r2）已删除，
> 03 相关代码与测试以分支真实实现（`1d4fbd7`/`8a0d750`）为准，只保留
> 01 的分块度量与 05 的缓存/租约接线。
