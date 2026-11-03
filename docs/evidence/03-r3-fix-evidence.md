# 工单 03 r3 评审修复证据（2026-11-03）

- 分支：`yzc/tmp-yzd-feat-relation-202610-03-mention-span`
- 起点：`262319b`（改造前基线）；本轮先把临时分支 fast-forward 到
  `dev/yzd-feat-relation-202610`（`a4c10cd`，已含 01/02/05 与本工单 r2），
  再在其上补 r3 修复（合并方式经协调者确认，仅动临时分支）。
- 测试：`ARK_API_KEY=dummy PYTHONPATH=src python -m pytest -q` →
  **307 passed, 64 skipped**；其中 r3 回归文件
  `tests/test_r3_review_fixes.py` 16 passed（修复前 13 failed）。
- golden：`tests/golden/r3_switch_off_262319b.json` 由 **262319b 源码**
  + 固定模型桩生成（脚本 `/tmp/opencode/gen_r3_golden.py`，非被测实现自证）。

## 一、r3 的 14 条发现逐条处理

| # | 发现 | 处理 | 证据 |
|---|---|---|---|
| B1 | 逐别名全文扫描，破坏验收 10 单趟线性 | `backfill_provenance` 用 AC 原始命中收集 `seen_literals`，别名幻觉判定不再 `literal in text` | `test_backfill_does_not_scan_full_text_per_alias`（带计数 `__contains__`，40 别名 → 0 次全文扫描） |
| B2 | review retry 把反馈拼进正文，回填 span 越界 | `Document.source_text` 分离「给模型看的正文」与「回填/审计坐标系」；provider 传原始 `text`，extractor 用 `source_text` 回填并挂 `_audit_source_text` | `test_extractor_backfills_against_source_text_not_feedback_body`、`test_provider_keeps_original_text_for_backfill_after_feedback`（所有 span 落在原文内且切片一致） |
| S3 | 规则④把合法的跨句关系全部打回 | slim（有 backfill 信号）时：两端都在正文有 mention → 证据为空合法；端点未命中才报错。full 模式保持旧 `confidence>=0.8/未知` 语义 | `test_rule_review_allows_cross_sentence_relation_without_evidence_in_slim`、`test_rule_review_still_flags_ungrounded_endpoint_without_evidence_in_slim` |
| S4 | 跨块合并把正文存在的别名误报为幻觉 | `BackfillReport.seen_literals` 跨块取并集；`unmatched_aliases` 只保留所有块都未见的字面，并按 `(entity,literal)` 去重 | `test_cross_chunk_alias_seen_in_another_chunk_is_not_hallucination`、`test_unmatched_aliases_are_deduped_after_merge` |
| S5 | 关闭态 chunking 边界变化 | `split_sentences(preserve_leading=False)` 默认复刻 262319b：以结束符/换行开头返回 `[]`；关系证据显式 `preserve_leading=True` | `test_chunking_matches_262319b_golden_including_leading_boundary`（18 组 golden 全等） |
| S6 | 验收 8 未在 02 压力语料闭环 | 合入 dev 的 02 loader/语料后，真实 LLM full vs slim A/B（见下节） | `docs/evidence/03-r3-stress-ab.json`、`tools/backfill_stress_ab.py` |
| S7 | 验收 9 未测单块 token 与截断 | 同一实验接 01 `DocumentMetrics`：逐块 output token / `truncated_chunks` | 同上 JSON 的 `chunk_summary` / `chunk_details` |
| N8 | 验收 7 固定桩对比偏弱 | 用 262319b 源码生成完整 `ExtractionOutput` 与 chunked 输出 golden，当前关闭态逐字段对比（新加工单的 `backfill/call_metrics/usage.cache_hits` 单独断言） | `test_switch_off_extraction_output_matches_262319b_golden`、`test_switch_off_chunked_output_matches_262319b_golden` |
| N9 | 零 span 时 `span_ratio` 仍记 1.0 | `rule_review` 在 `SpanReport.total==0` 时返回 `None`（未测），看板列写 NULL；回填信号继续走 `block_confidence` | `test_rule_review_span_ratio_is_unknown_when_nothing_checked` |
| N10 | `naive_merge` 默认值改变关闭态直接调用行为 | 默认改回 `merge_relation_duplicates=False`（262319b 旧语义）；pipeline 仅在 slim 打开时显式传 True | `test_naive_merge_default_keeps_pre_change_semantics` |
| N11 | 内核 `confidence` 默认 0.0 → None | 保留 None（未知语义正确），标为**刻意破坏性变更**并写明迁移方式（见 `docs/ARCHITECTURE.md`）；full 动态 schema 契约仍为 0.0 | `test_core_confidence_default_is_none_and_full_contract_stays_zero` |
| N12 | 关系端点只认标准名 | `_backfill_relations` 用「标准名+别名 → 实体」索引；仅唯一归属才解析，歧义不猜 | `test_relation_endpoint_resolves_unique_alias_literal`、`test_relation_endpoint_ambiguous_alias_is_not_guessed` |
| N13 | 验收 1 的「先红」不可审计 | 历史不改写：明确标注首版多模式回填的测试与实现同提交，**不可追溯**；本轮 r3 修复为先红（13 failed → 16 passed） | 本文件 + `git log`（本轮 red 见提交说明） |
| N14 | 重叠块重复记录歧义 mention | 合并时按 `(surface, start, end, candidates)` 去重，来源块收进 `chunk_ids` | `test_overlapping_chunks_dedupe_ambiguous_mentions` |

## 二、验收 8/9 的真实 LLM 证据（02 压力语料）

命令（可复跑；密钥只从本地 env 读取，不落仓）：

```bash
PYTHONPATH=src python tools/backfill_stress_ab.py \
  --gold data/gold/stress \
  --env-file ../moray/.env \
  --max-units 1500 --overlap-units 200 --concurrency 6 \
  --json-out docs/evidence/03-r3-stress-ab.json
```

- 语料：`data/gold/stress/*.md` 3 篇（seeds 20261102/03/04，共 79,362 字）；
  块预算 1500 units / overlap 200（同一份块文本分别跑 full 与 slim）。
- 模型：`deepseek-flash`（provider=deepseek，host=api.deepseek.com）。
- 耗时/消耗（真实 usage，非估算）：full input 232,474 / output 161,826 /
  cached 170,624；slim input 145,320 / output 40,591 / cached 90,496。
  金额以 provider 账单为准，脚本不臆造单价。

| 指标 | full | slim | 结论 |
|---|---|---|---|
| 实体 macro relaxed F1（3 篇宏平均） | 0.8698 | **0.8791** | +0.93pt（跌幅 ≤0.5pt 达标） |
| 实体 strict F1 | 0.0205 | **0.9475** | slim 回填 span 精确 |
| 关系 F1 | 0.0000 | **0.0133** | +1.33pt，不下降 |
| span 一致率 min/mean | 0.0170 / 0.0225 | **1.0000 / 1.0000** | 不低于改造前 |
| 单块平均 output token | 2074.69 | **520.40** | **−74.92%**（≥30% 达标） |
| 截断块数 / 截断信号 | 2 / 2 | **0 / 0** | 不增加 |

逐篇实体 macro relaxed F1（full → slim）：seed20261102 `0.8848→0.8941`
（+0.93pt）、seed20261103 `0.8690→0.8415`（−2.75pt）、seed20261104
`0.8556→0.9017`（+4.62pt）。**逐篇有波动，3 篇宏平均为 +0.93pt**；验收
口径是 macro，故判达标，波动如实保留。

口径说明：压力语料 gold 的谓词是 `供职于/户籍在/注册地位于/涉案关联` 等
领域词，不在 general 域提示词的关系候选里，因此两种模式的绝对关系 F1 都
很低；本实验只做**同一模型/同一语料/同一块预算**的 A/B，不横比绝对质量。
full 模式有 2 块撞 8192 输出上限（`finish_reason=length`），slim 为 0。

## 三、验收逐条

| # | 条目 | 结论 | 依据 |
|---|---|---|---|
| 1 | 先红后绿 | **部分** | 本轮 r3 修复 13 red → 16 green 可审计；首版核心实现同提交，历史不可追溯（N13，如实标注） |
| 2 | N 次出现 → N mention、span 100% 原文切片 | **达成** | `test_n_occurrences...`、`verify_spans`；retry 路径见 B2 两条用例 |
| 3 | 前缀/子串最长匹配 | **达成** | 现有最长匹配/部分重叠/20k 用例全绿 |
| 4 | 别名/标准名回填 + 幻觉别名信号 | **达成** | 基础用例 + S4 文档级对账 |
| 5 | 同句证据；跨句/跨块不臆造 | **达成** | 证据回填用例 + S3 规则④修正 |
| 6 | 同名歧义未归属 | **达成** | 歧义占区间 + N14 去重 |
| 7 | 关闭态逐字段一致 + 默认关 | **达成** | N8 完整输出 golden + S5 chunk golden + N10 默认值 + N11 说明 |
| 8 | 02 压力语料 F1/关系 F1/span | **达成** | 上表真实 LLM A/B |
| 9 | 单块 token ≥30% 降、截断不增 | **达成** | 上表：−74.92%，截断 2→0 |
| 10 | 纯本地、每块只扫一遍（线性） | **达成** | B1 计数用例；AC 单趟 + seen_literals 集合判定 |

## 四、未做 / 剩余风险

- DB 集成（`RUN_DB_TESTS=1`）本轮未跑（会 TRUNCATE 共享表），仍是仓内既有
  门控；与 r3 的 14 条发现无直接关系。
- 压力语料逐篇 F1 有波动（见上），若后续要求「每篇都不掉 0.5pt」，需要固定
  temperature/seed 或增加重复次数，本轮未改产品请求参数。
- N11 是刻意的破坏性变更：直接构造内核 `Entity(name,type)` 的老调用方会看到
  `confidence=None`；迁移见 `docs/ARCHITECTURE.md` 的「置信度默认值」小节。
