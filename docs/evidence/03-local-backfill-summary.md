# 工单 03 证据汇总：结构化输出瘦身 + mention/span 确定性本地回填

开关：环境变量 `MORAY_LOCAL_BACKFILL`（`1/true/yes/on` 打开），**默认关闭**。
关闭时抽取路径与结果口径与改造前逐字段一致（见 `test_default_switch_off_is_identical_to_before`、
`test_switch_off_does_not_backfill_model_fields_preserved`）。

## 改动面（最小文件集）

| 文件 | 作用 |
|---|---|
| `src/moray/extraction/local_backfill.py`（新增） | Aho–Corasick 多模式自动机（正文一趟扫描）、最长匹配、歧义判定、mention/span 与关系证据回填、确定性 `BackfillReport` |
| `src/moray/schema/factory.py` | `build_extraction_model(domain, slim=)` / `build_system_prompt(domain, slim=)`：瘦身模型实体仅 name/type/aliases/attributes，关系仅 head/predicate/tail；默认 `slim=False` 与原来字节一致 |
| `src/moray/extraction/extractor.py` | `Extractor(local_backfill=None|bool)`；打开时模型只出逻辑实体/关系，成功校验后在块文本上本地回填；`ExtractionOutput.backfill` 挂载确定性信号 |
| `src/moray/extraction/document.py` | `ExtractionOutput.backfill: dict | None`（旁路信号，非稳定内核字段） |
| `src/moray/config.py` | `local_backfill_enabled()` 读 `MORAY_LOCAL_BACKFILL`，默认关 |
| `src/moray/extraction/__init__.py` | 导出 `backfill_provenance/BackfillReport/AmbiguousMention` |
| `tests/test_local_backfill.py`、`tests/test_slim_schema.py`、`tests/test_backfill_extractor.py`（新增） | 22 个离线测试（先红后绿） |
| `.env.example`、`docs/evidence/*` | 开关文档与证据 |

对上层稳定内核 `Entity/EntityMention/Span/Relation` 字段与语义**不变**：
见 `03-local-backfill-contract.json` 的实际请求/响应样例。

## 验收条目对照

1. **先红后绿（多实体/别名/多次出现/中英混排）**
   `test_multi_entity_alias_repeated_mixed_script_backfill`。
2. **N 次出现 → N mention，span 与原文切片 100% 一致（偏移零幻觉）**
   `test_n_occurrences_produce_n_mentions_with_exact_slices`；并在压力语料上
   `verify_spans` 每篇 ok、min=mean=1.0000（见 `03-stress-corpus-eval.txt`）。
3. **前缀/子串最长匹配优先**：`test_prefix_substring_longest_match_wins`。
4. **别名/标准名同实体；正文不存在的别名不产 mention 且计入信号**
   `test_canonical_and_alias_ground_to_same_entity_phantom_alias_signaled`。
5. **同句共现回填证据；跨句/跨块留空不臆造；最近一对 + 全候选**
   `test_relation_evidence_same_sentence_backfilled`、
   `test_relation_evidence_cross_sentence_is_empty_not_fabricated`、
   `test_relation_evidence_nearest_pair_wins_and_all_kept`。
6. **同名歧义未归属、不写错实体**：
   `test_same_name_different_type_is_ambiguous_and_unattributed`、
   `test_alias_ownership_conflict_is_ambiguous`。
   同 (type,name) 重复输出会先确定性合并（等同 `naive_merge` 身份），不误判歧义：
   `test_duplicate_logical_entities_are_coalesced_not_ambiguous`。
7. **开关关闭逐字段一致 + 默认关**：
   `test_default_switch_off_is_identical_to_before`（固定桩 schema/提示词全等）、
   `test_switch_off_does_not_backfill_model_fields_preserved`、
   `test_env_flag_enables_backfill_by_default`。
8. **压力语料（工单 02，3 seed / ~89 实体 / ~110 关系 / 两种块预算）macro F1**
   见 `03-stress-corpus-eval.txt`：
   - 实体 relaxed macro-F1 delta = **+0.0000**（≤0.5pt 门槛，零回退）
   - 实体 strict macro-F1 **+0.45pt / +0.52pt（略升）**
   - 关系 macro-F1 delta = **+0.0000（不下降，0.8287→0.8287）**
   - span 核对一致率 = 100%（改造前的规则 span 为对照；回填后无任何错偏）
   - 注：工单 02 模块在本 worktree 为未提交兄弟分支，此证据用其**只读接口**离线生成，
     未改动其文件；放量时在合并后的黄金集上按同一口径复测。
9. **输出 token（工单 01 度量口径）**：见 `03-output-token-reduction.txt`，
   单块输出 JSON 体积下降 60%/74%/83%（每实体出现 1/3/6 次），schema 定义 −50.8%，
   均 ≥30% 目标。slim 输出是 full 输出的严格字段子集，**不会新增截断**；
   真实 token/截断块数由工单 01 `chunk_metrics` 在放量 A/B 实测确认。
10. **纯本地确定性、每块正文只扫描一趟（线性）**：
    `test_scan_is_single_linear_pass_independent_of_pattern_count`
    （计数包装证明 8 与 58 个模式都只迭代正文一趟、字符推进次数恒为正文长度）。

测试：`113 passed, 47 skipped`（skip 为需 PostgreSQL/嵌入模型的集成测试），
见 `03-pytest-output.txt`。
