# 工单 03 证据汇总：结构化输出瘦身 + mention/span 确定性本地回填

> 第 4 轮评审修复（缓存键含回填模式、整篇别名口径、验收 8 可判域重跑、
> N1/N4/N5/N6/N7）见 `03-r4-fix-evidence.md`。验收 8/9 的当前权威证据是
> **`03-r15-stress-ab.json`（r15 代码 HEAD `a5e1ca2`，3 轮）**；
> `03-r13-stress-ab.json`（`0df406c`）与 `03-r3-stress-ab.json` 只作历史
> 对照。本文件下方 r2 记录保留为历史。
>
> **第 13 轮评审修复（r13：S1 缓存键含 attributes/description/confidence、
> S2 验收 8 HEAD 重放与旧文档纠正、N1/N3/N4/N5/N6、N2 口径收窄）见
> `03-r13-fix-evidence.md`。**
>
> **第 14 轮评审修复（r14：F1 关系配对边配对边限流、F2 别名存值规范化、
> F3 缓存载荷版本递增、F4 逐篇最差披露、F5 负数上限只告警一次）见
> `03-r14-fix-evidence.md`。**
>
> **第 15 轮评审修复（r15：F1 跨块歧义虚拟 owner、F2 验收 8/9 在 r15 HEAD
> 重放、F3 无证据关系对账、F4 跨块候选全局排序、F5/F6/F7 证据口径、
> F8 API/作业回填单一入口）见 `03-r15-fix-evidence.md`。**
>
> **第 10 轮评审修复（r10：S1 吸收字面作关系端点、S2 重叠块同 span 歧义对账、
> N1–N11）见 `03-r10-fix-evidence.md`；第 11 轮（r11：F1 分块候选全量传递、
> F2 验收 8 口径、N1–N11）见 `03-r11-fix-evidence.md`。**
>
> **验收 8 状态（r15 更新）**：r15 代码 HEAD `a5e1ca2` 用 02 压力语料重放
> full/slim 3 轮（`03-r15-stress-ab.json`，working tree clean，git_dirty=
> false）**达成**：实体 macro relaxed F1 0.8934→0.8997，mean-of-N
> **+0.63pt**、worst-of-N **−0.15pt**（≤0.5pt 门槛内）；关系 F1
> 0.3223→0.6996（不下降）；span 一致率 min/mean 0.0093/0.0164→1.0/1.0；
> 单块 output token 2610.83→595.61（−76.83%）；截断块 7→0。
> `03-r13-stress-ab.json`（`0df406c`）的数字只作为历史快照保留。
> **逐篇残留（r15 披露）**：逐篇最差 **−4.55pt**（`stress_seed20261104`
> 第 1 轮；该篇三轮 −4.55 / 0.00 / 0.00、逐篇均值 **−1.52pt**；另两篇逐篇
> 均值 +2.28pt / +1.13pt）。上面的「≤0.5pt 门槛内」是单轮 **macro** 的
> mean-of-3 + worst-of-N 口径，不覆盖逐篇单轮波动；逐篇最差比 r14 的
> −1.59pt 更大，翻默认前必须按逐篇波动单独评估。默认开关仍保持关闭。

开关：环境变量 `MORAY_LOCAL_BACKFILL`（`1/true/yes/on` 打开），**默认关闭**。
关闭时：full schema / 提示词 / 归并语义保持 262319b（schema+结果逐字段 golden）。
打开时：模型只出逻辑实体/关系，mention/span 与关系证据由本地 Aho-Corasick
一次线性扫描回填；稳定内核 `Entity/EntityMention/Span/Relation` 字段不变。

## 历史：第 2 轮评审修复逐条处理（r2 当时状态；r3–r10 已推进，勿当当前结论）

| # | 评审发现 | 处理 | 证据 |
|---|---|---|---|
| 1 | **blocker** slim 下 confidence 恒 0.0 被当真、规则④永不触发 | `confidence` 改为可空：slim 缺失 = `None`，入库写 NULL（不冲掉已知值），查询/API 回 `null`，`/relations` 排序 `NULLS LAST`；规则④对「未知 + 无证据」也拦截；审校/看板消费 `block_confidence` | `test_slim_to_core_keeps_confidence_unknown`、`test_ingestor_passes_unknown_confidence_through_as_none`、`test_unknown_confidence_is_null_not_zero`、`test_relation_unknown_confidence_sorts_last`、`test_rule_review_flags_evidence_less_relation_when_confidence_unknown`、`03-local-backfill-contract.json` |
| 2 | should `_is_free` 语义反了，部分重叠短名漏进长名 | 改为整段全空闲才接受（`_interval_all_free`），部分重叠用例覆盖 | `test_partial_overlap_short_name_does_not_leak_into_long_name`、`test_partial_overlap_does_not_block_independent_occurrence` |
| 3 | should 英文句号不切句、开头空白句列表为空 | `split_sentences(ascii_period=True)` 供关系证据使用（不切 3.14）；开头空白不再返回空；证据 span 裁掉句首尾空白。默认分块路径只改变「开头是空白/结束符」这一原先返回 0 块的边界情形 | `test_relation_evidence_splits_ascii_period_sentences`、`test_relation_evidence_handles_document_starting_with_whitespace` |
| 4 | should 关闭开关仍改变归并输出 | `naive_merge(merge_relation_duplicates=...)`：pipeline 仅在开关打开时合并后见证据/置信度/description；关闭走 262319b 旧语义 | `test_switch_off_merge_matches_pre_change_full_field_golden`（golden 由 262319b 源码生成）、`test_chunked_pipeline_gates_relation_merge_by_switch` |
| 5 | should 验收 8 未在 02 压力语料验证、文件名误导 | 误导文件改名 `03-stress-corpus-eval.txt` → `03-baseline-ab.txt`；明确标注压力集 LLM 复跑**r2 时未闭环**；r3 的快照后来经 r11/r13 复核为 worst −1.30pt（当时未达成），当前 HEAD 重放已达成（见上方 r13 段） | `03-baseline-ab.txt`、`tools/backfill_ab_eval.py` docstring |
| 6 | should 验收 9 用 per-document 与 attempts 冒充 | 证据文件**r2 时**明确「验收 9 未闭环」，列出 01 chunk_metrics/截断口径与后续命令；r3 起有快照，当前 HEAD 重放见上方 r13 段 | `03-output-token-reduction.txt`、`03-baseline-ab.txt` |
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
| 1 | 先红后绿 | **可核范围是 r2+ 修复；原始 `185fb41` 的先红不可追溯（r15 F5 明确）**：当前仓库没有 `185fb41` 时点的红测试记录，`03-r2-red-tests.txt` 的 21 条红用例也不含本工单最初的三条（多实体/别名/同文多次/中英混排、N occurrences、prefix-substring）。因此不再声称「原始功能先红」；r2 及以后每轮修复均保留红→绿输出（含 r15 的 5 failed/1 passed） | `03-r2-red-tests.txt`（r2 修复红）、`03-r15-red-tests.txt`（r15 红）；本行即证据链缺口披露 |
| 2 | N 次出现 → N mention、span 原文切片 | 达成 | `test_n_occurrences_produce_n_mentions_with_exact_slices`；slim span 1.0 |
| 3 | 前缀/子串最长匹配不错误覆盖 | 达成（含部分重叠） | `test_prefix_substring_longest_match_wins`、`test_partial_overlap_short_name_does_not_leak_into_long_name` |
| 4 | 别名/标准名同实体；幻觉别名计入信号 | 达成 | `test_canonical_and_alias_ground_to_same_entity_phantom_alias_signaled`、`test_unmatched_aliases_absorbed_by_longer_name_is_not_hallucination` |
| 5 | 同句共现回填证据；跨句/跨块不臆造；保留全部候选 | **有条件达成（r13 N2 口径收窄，r14 F1 内存口径同步，r15 F4 全局排序）**：证据定位达成（中文/英文/开头空白；r10 N1/N2 修候选边界与全局 selected；r11 F1 修分块路径截断；r14 F1 起配对阶段就用大顶堆只保留**最近 ≤2000 条/关系**，不再先物化 H×T 全量；r15 F4 跨块后 selected 固定首条、其余按全局距离排序，API/作业落盘再按 20 裁剪）。因此单条关系的候选上限是**进程内 ≤2000、出网 ≤20**；极端单句（如 50×50 共现）不再保留全部候选——这是**对工单「保留全部候选」的明确偏离，待工单 owner 确认**（r15 F7），不再声称无条件达成 | `test_relation_evidence_splits_ascii_period_sentences`、`test_relation_evidence_handles_document_starting_with_whitespace`、`test_relation_evidence_not_fabricated_across_chunks`、`test_selected_candidate_bounds_match_evidence_span_after_trim`、`test_chunked_pipeline_preserves_all_relation_evidence_candidates`、`test_extract_endpoint_caps_backfill_candidates`、`test_real_extractor_caps_internal_candidates_above_soft_limit`、`test_relation_evidence_pairing_without_punctuation_is_bounded`（r14）、`test_merged_candidates_are_globally_distance_sorted`（r15） |
| 6 | 同名歧义未归属 | 达成（单块 + r10 S2 重叠块同 span 对账 + r15 F1 块内歧义跨块虚拟 owner） | `test_same_name_different_type_is_ambiguous_and_unattributed`、`test_alias_ownership_conflict_is_ambiguous`、`test_overlap_same_span_owned_by_two_entities_is_unattributed`、`test_overlap_block_ambiguity_blocks_later_single_owner_mention`（r15：块 0 双实体、块 1/2 单实体，两实体 mention 均为空且候选含 ORG+MISC） |
| 7 | 开关关闭逐字段一致 + 默认关 | **达成（口径已讲清；r15 F6 修正测试命名）**：schema/prompt 对 262319b 逐字节；归并结果对 262319b 全字段 golden；默认关。名为「关态不触发回填」的用例只断言字段默认值，已改名并在 docstring 指向真正的 golden 用例 | `test_full_schema_and_prompt_match_pre_change_golden`、`test_switch_off_merge_matches_pre_change_full_field_golden`、`test_switch_off_extraction_output_matches_262319b_golden`、`test_switch_off_does_not_backfill_and_keeps_field_defaults`（r15 F6） |
| 8 | 02 扩展语料实体 F1 不跌、关系不降 | **达成（r15 代码 HEAD `a5e1ca2` 重放）**：02 压力语料 3 轮 full/slim，实体 macro relaxed F1 0.8934→0.8997，mean-of-N **+0.63pt**、worst-of-N **−0.15pt**（单轮 −0.15 / +1.32 / +0.72pt，均在 ≤0.5pt 门槛内）；关系 F1 0.3223→0.6996（不下降）；span 一致率 min/mean 0.0093/0.0164→1.0/1.0。逐篇最差 **−4.55pt**（`stress_seed20261104` r1，该篇三轮 −4.55/0.00/0.00、均值 −1.52pt，见上方 r15 披露）。默认开关保持关闭 | `03-r15-stress-ab.json`（git_sha=a5e1ca2、git_dirty=false）、`03-r15-fix-evidence.md` |
| 9 | 单块 output token ≥30% 降、截断不增 | **达成（r15 代码 HEAD `a5e1ca2`）**：2610.83 → 595.61 tokens/块（−76.83%），截断块 7 → 0；历史 `0df406c` 的 2783.56→610.20（−77.73%）、6→0 同样达成 | `03-r15-stress-ab.json` 的 `delta`、`03-output-token-reduction.txt` |
| 10 | 纯本地确定性、每块只扫一遍（线性） | 达成（口径已讲清）：块扫描每块一趟；单块文档不再做第二次整篇别名补扫（r11 N8），多块时整篇补扫一次仍是线性、且与块扫描用途不同 | `test_scan_is_single_linear_pass_independent_of_pattern_count`、`test_single_chunk_merge_skips_document_alias_rescan`、20k 性能回归 |

## 测试

r15 评审修复后的最新回归（本分支代码 HEAD，含 03 × 01 × 05 合并后的全部用例）：

```bash
# 离线（无 DB；ARK_API_KEY=dummy 仅为让 get_settings() 通过，不发请求）
ARK_API_KEY=dummy python3 -m pytest -q -p no:cacheprovider
# 414 passed, 65 skipped, 0 failed  -> docs/evidence/03-r15-full-suite.txt
```

DB 用例（`RUN_DB_TESTS=1`）本轮未跑：本机无 pgvector；r15 改动不涉 SQL/迁移。
（历史数字：r2 时 148 passed / 50 skipped；r11 时 379 passed / 65 skipped；
r13 401 passed；r14 408 passed。）

原始输出：`03-r15-full-suite.txt`（r15 本轮更新；r14 为 `03-r14-full-suite.txt`）。

> 收敛说明：临时实现（先红提交 `03a7c79` 之后稳定分支自补的 r2）已删除，
> 03 相关代码与测试以分支真实实现（`1d4fbd7`/`8a0d750`）为准，只保留
> 01 的分块度量与 05 的缓存/租约接线。
