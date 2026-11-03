# 工单 03 第 10 轮评审修复证据（r10）

- 分支：`yzc/tmp-yzd-feat-relation-202610-03-mention-span`（与评审同一 checkout）
- 评审定点：`262319b`；被复核的 HEAD：`56ba147`
- 方式：先红后绿。先提交 `tests/test_r10_review_fixes.py`（12 failed / 1 passed），
  再逐条实现；`docs/evidence/03-r10-red-tests.txt` 是修复前的失败清单。
- 测试环境：`ARK_API_KEY=dummy /tmp/opencode/review-venv/bin/python -m pytest
  -p no:cacheprovider`（无 DB / 无真实 LLM）。
- 全量结果：**368 passed / 65 skipped**（`03-r10-full-suite.txt`；比 r9 的
  355 多 13 条，正是本文件新增用例）。
- 定点探针：`03-r10-probe-output.txt`（确定性、无模型/无网络，逐条复现 S1/S2
  与 N1/N2/N4/N5/N9/N10）。

## 结论速览

| 发现 | 级别 | 处理 | 证据 |
|---|---|---|---|
| S1 吸收字面作关系端点被误报未命中 | should | ④ 复用 `seen_literals` | `test_relation_endpoint_absorbed_literal_is_grounded`、`test_absorbed_endpoint_does_not_trigger_model_rework` |
| S2 重叠块同一 span 双写 + 无歧义 | should | 归并后按全局 span 对账 | `test_overlap_same_span_owned_by_two_entities_is_unattributed` |
| N1 候选句边界与 evidence span 不一致 | nit | 候选增加裁剪后的 `evidence_start/end` | `test_selected_candidate_bounds_match_evidence_span_after_trim` |
| N2 合并后多条 `selected=True` | nit | 合并时只保留首块那条 selected | `test_merged_candidates_have_exactly_one_selected` |
| N3 `unmatched_aliases` 去重键不带类型 | nit | entry 加 `type`，键改三元组 | `test_unmatched_aliases_keep_type_so_same_name_diff_type_do_not_collapse` |
| N4 候选无上限直接外发 | nit | 序列化裁剪（默认 20，selected 必留），内存全量 | `test_relation_evidence_candidates_are_capped_in_serialized_signal` |
| N5 零 span 记 1.0 + 每块二次回填 | nit | 零 span 记 `None`；同一结果只扫一次 | `test_middleware_zero_span_is_unmeasured_and_reuses_extractor_cache`、`test_cached_backfill_runs_one_scan_per_result` |
| N6 ARCHITECTURE 口径过时 | nit | 改为 `is not None` 口径 | `docs/ARCHITECTURE.md` |
| N7 证据文档自相矛盾 | nit | summary 8/9 行、token 文件 r2 段改为历史 | 本文 + `03-local-backfill-summary.md`、`03-output-token-reduction.txt` |
| N8 `extraction/__init__` 重复导入 | nit | 删除第二组 | `test_extraction_package_init_has_no_duplicate_imports` |
| N9 身份键 strip 口径不一致 | nit | 抽 `entity_identity_key`，slim 归并去空白 | `test_naive_merge_identity_key_can_ignore_surrounding_whitespace` |
| N10 `naive_merge` 改写 partials | nit | 合并时存 `model_copy` | `test_naive_merge_does_not_mutate_input_relations` |
| N11 AC8/AC9 只在旧快照 + mean-of-3 成立 | nit | 文档并列「达成条件」与「未满足最坏值」，HEAD 未重放 | 本文「验收 8/9」节 |

## S1（should）吸收字面作关系端点不再误报「端点在正文中未命中」

- 位置：`src/moray/orchestration/specialists.py` `rule_review`
- 修法：把 ⑤ 已算好的 `seen`（正文真实出现过的字面，含被最长匹配吸收的命中）
  提前到 ② 之后，`_endpoint_grounded()` 在实体没有 mention 时再看该字面是否在
  `seen` 里。只影响 slim（有 backfill 信号）分支；关闭态逐字节不变。
- 探针（`03-r10-probe-output.txt`）：
  - 正文「阿里巴巴发布新品。」，实体 `阿里巴巴/ORG`、`阿里/ORG`，
    `阿里` 的 mentions 为空，`seen_literals=['阿里','阿里巴巴']`；
  - `rule_review` → `pass`，issues 为空；
  - `run_extraction_team(max_iters=3)` → **1 次调用，accepted=True**
    （修复前：3 次调用，accepted=False，见 `03-r10-red-tests.txt`）。
- 用例：`tests/test_r10_review_fixes.py::test_relation_endpoint_absorbed_literal_is_grounded`
  与 `::test_absorbed_endpoint_does_not_trigger_model_rework`。

## S2（should）重叠块同一全局 span 的多归属被撤销并留下歧义信号

- 位置：`src/moray/extraction/local_backfill.py::reconcile_span_ambiguity` +
  `src/moray/extraction/pipeline.py::run`
- 修法：`naive_merge` 之后按 `(surface, global_start, global_end)` 分组；owner
  多于 1 时从所有实体的 mentions 删掉该 span，并合并成一条全局
  `AmbiguousMention`（candidates = owner 集合）。**只对同一全局 span 对账**：
  不同位置上的同名出现仍按块内上下文归属。
- 探针：两块同为「苹果发布了新品。」（重叠），块 0 只抽 `苹果/ORG`、块 1 只抽
  `苹果/MISC` → 合并后两个实体的 mentions 均变为 `[]`，`ambiguous_mentions`
  恰 1 条：`surface=苹果, start=0, end=2, candidates=[ORG,MISC]`。
- 用例：`test_overlap_same_span_owned_by_two_entities_is_unattributed`；
  反向护栏 `test_overlap_same_entity_keeps_single_span_mention`（同一实体在
  重叠块各命中一次不是歧义，不误删）。
- 计数口径不变：`entities_grounded` 等仍是 chunk-weighted（r4 N4），这里只修
  「写进哪个实体」的归属冲突。

## N1（nit）selected 候选与 evidence span 可逐值对齐

- 修法：每个候选新增裁剪后的 `evidence_start/evidence_end`；原始
  `sent_start/sent_end` 仍保留，便于排查为什么裁剪。
- 探针：英文两句 → evidence span `(24,48)`；selected 候选
  `sent_start=23, sent_end=48, evidence_start=24, evidence_end=48`。
- `docs/evidence/03-local-backfill-contract.json` 的请求/响应样例已用当前
  实现重放，补上 `evidence_start/evidence_end`（diff 仅新增这两个字段）。

## N2（nit）跨块合并后全局只有一条 selected

- 修法：`merge_backfill_reports` 按块顺序拼接候选后，若出现多条 selected，只
  保留第一条（= 首个真正提供了证据的块，与 `naive_merge` 首见证据一致），其余
  降为普通候选。
- 用例：同一关系来自两块 → 候选 2 条、selected 恰 1 条。

## N3（nit）unmatched_aliases 去重键带类型

- 修法：entry 增加 `"type"`（`{"type","entity","literal"}`），merge 去重键与
  整篇别名扫描的候选集合都改用三元组。
- 用例：`苹果/ORG` 与 `苹果/MISC` 各给一个正文不存在的 `Acme` → 单块 2 条，
  跨块汇总后仍 2 条（修复前会被合并成 1 条）。
- 兼容性：这是 `backfill` 旁路信号的新增字段，不是内核 schema；老消费方按
  `entity/literal` 读仍可用。受影响的 3 条旧用例已同步断言 `type`。

## N4（nit）关系证据候选序列化上限

- 修法：`BackfillReport.to_dict()` 默认每条关系最多保留
  `MAX_SERIALIZED_RELATION_EVIDENCE_CANDIDATES = 20` 条（`selected` 必留），
  传 `None` 可取全量；内存中的 `BackfillReport` 始终保留全部候选。API 响应与
  作业结果都走这份 `to_dict()`，因此不再外发数十 MB 的笛卡尔积。
- 探针：400 字量级 run-on「华为深圳」×30 → 内存 900 条候选，序列化后 20 条，
  selected 恰 1 条。
- 口径说明：工单要求「保留全部候选」在**内存报告**层满足；序列化默认裁剪是
  评审建议的最小修法（K 可配）。K 仍可按 owner 要求调整常量。

## N5（nit）中间件零 span 记未测 + 回填只扫一次

- 修法 1：`ExtractionMetricsMiddleware` 在没有可核对 span 时
  `rec.span_ratio = None`（与 `rule_review` 的 r3 N9 口径一致），不再写空转 1.0。
- 修法 2：新增 `cached_backfill_provenance(owner, result, text)`，按
  `(原文, 结果 JSON)` 缓存到本次 reply 的 agent 上；中间件先算，抽取器随后
  复用，块正文只扫一次。结果不同（返工后的新输出）自然未命中重算。
- 用例：`test_middleware_zero_span_is_unmeasured_and_reuses_extractor_cache`
  同时断言 `span_ratio is None`、`block_confidence=0.5`、跨边界只有 1 次扫描；
  `test_cached_backfill_runs_one_scan_per_result` 用 monkeypatch 计数。
- 局限：中间件与抽取器只有在「抽取结果逐字段相同」时才命中缓存；不一致时退回
  两次扫描（正确性不受影响）。目前 `build_observability_stack` 未在 src 主链
  接线，这是旁路风险，不构成线上回归。

## N6（nit）ARCHITECTURE 口径与代码一致

- 改动：`docs/ARCHITECTURE.md` 由 `document.source_text or document.text` 改为
  `source_text if source_text is not None else text` 口径（空字符串不回退）。
- 同节补充 r10 的跨块 span 对账、候选字段/上限、身份键与单次扫描说明。

## N7（nit）证据文档自相矛盾

- `docs/evidence/03-local-backfill-summary.md`：验收 8 改为「达成（mean-of-3，
  快照 9f95ed4）+ worst-of-N 未满足 + HEAD 未重放」；验收 9 改为「达成（同一
  快照，2365.98→582.13，截断 6→0）」；验收 5/6 补 r10 依据。
- `docs/evidence/03-output-token-reduction.txt`：r2 的「未闭环」段与「后续动作」
  段标记为**历史记录**，并说明 `chunk_metrics` 已合入、r3 已闭环。

## N8（nit）重复导入

- 改动：删除 `src/moray/extraction/__init__.py` 第二组
  `chunking/document/extractor` 导入；保留首组。
- 用例：`test_extraction_package_init_has_no_duplicate_imports` 断言每个导入
  语句只出现一次。

## N9（nit）实体身份键统一

- 改动：新增 `local_backfill.entity_identity_key(entity, *, strip_name=True)`；
  回填 `_coalesce_entities` 与 `naive_merge(strip_entity_name=slim)` 共用。
  关闭态 `naive_merge` 默认 `strip_name=False`，保持 262319b 精确键。
- 用例：`" 腾讯"` 与 `"腾讯"` → slim 归并 1 个实体，legacy 归并 2 个。

## N10（nit）naive_merge 不改写入参

- 改动：`rel_index[key]` 存 `r.model_copy(deep=True)`，后续补证据/抬置信度只改
  副本。
- 用例：合并后 `merged.relations[0].confidence=0.9`，入参 `first.confidence`
  仍为 `None`、`evidence_span` 仍为 `None`。

## N11 / 验收 8、9：达成条件与未满足的最坏值并列

- 验收 8：**达成（mean-of-3）**，同时**worst-of-N 未满足**。
  - 3 轮实体 macro 增量 +1.00 / −1.30 / +0.52pt，均值 **+0.0733pt**（≤0.5pt）；
  - 单轮最低 **−1.30pt**、seed20261104 逐篇均值 **−2.88pt**，超过 ≤0.5pt；
  - 唯一数据快照 `docs/evidence/03-r3-stress-ab.json` 的 `git_sha=9f95ed4`；
  - **HEAD（r10）未重放**：本机无真实 LLM 凭证，本轮不伪造新数值。
- 验收 9：**达成（同一快照 9f95ed4）**：单块 output token 2365.98 → 582.13
  （−75.32%），截断块 6 → 0，与 JSON `delta` 逐项一致。
- Gate（不翻默认开关）：`MORAY_LOCAL_BACKFILL` 保持默认关；翻默认开关前必须用
  HEAD 重跑 A/B，并同时报告 mean-of-N 与 worst-of-N；worst-of-N 超过 −0.5pt
  不得翻默认开关。

## 测试命令

```bash
# 先红（src 退回修复前，仅保留新用例）
ARK_API_KEY=dummy python -m pytest -q tests/test_r10_review_fixes.py
# 12 failed, 1 passed  -> 03-r10-red-tests.txt

# 修复后（本文件 HEAD）
ARK_API_KEY=dummy python -m pytest -q -p no:cacheprovider
# 368 passed, 65 skipped -> 03-r10-full-suite.txt
```

DB 用例（`RUN_DB_TESTS=1`）本轮未跑：本机无 pgvector；本轮改动不涉
SQL/迁移，契约面无新增/更改 HTTP 端点（`backfill` 仍是既有可空字段，只新增
`type`/`evidence_start`/`evidence_end` 三个附加键）。
