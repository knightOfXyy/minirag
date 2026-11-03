# 工单 03 第 12 轮评审修复证据（r12）

- 分支：`yzc/tmp-yzd-feat-relation-202610-03-mention-span`（与评审同一 checkout）
- 评审报告：`/tmp/opencode/review-r12-03.md`；本轮起点 `84f6f6a`
- 方式：先红后绿。先写 `tests/test_r12_review_fixes.py`（11 failed / 1
  coverage-pass），再逐条实现；r10/r11 两条旧用例按修正后的语义同步更新。
- 测试环境：`ARK_API_KEY=dummy /home/dell/orca/workspaces/minirag/moray/.venv/bin/python
  -m pytest -p no:cacheprovider`（无 DB / 无真实 LLM；worktree 的 `.venv`
  为空，复用主仓 `moray/.venv`，`pytest` 的 `pythonpath=src` 指向本 worktree）。
- 全量结果：**391 passed / 65 skipped**（`03-r12-full-suite.txt`；r11 为
  379 passed，本轮新增 12 条用例）。定向 36 passed
  （`03-r12-green-targeted.txt`）。
- 定点探针：`tools/backfill_r12_probe.py` → `03-r12-probe-output.txt`
  （确定性、无模型/无网络，逐条复现 12 条发现）。

## 结论速览

| 发现 | 级别 | 处理 | 证据 |
|---|---|---|---|
| S1 只 strip 去重键、落库值仍带空白 | should | SLIM 归并存值也规范化；入库不再建 MISC 占位 | `test_slim_merge_normalizes_stored_values_and_ingest_resolves_entity`、探针 S1 |
| S2 歧义去重 O(A²) | should | `(surface,start)` 集合 O(1) 判重 | `test_ambiguity_dedup_is_linear_on_16k_all_ambiguous_hits`、探针 S2 |
| N1 重叠块候选按块重复 | nit | 按全局位置去重 + 聚合 `chunk_ids` | `test_overlap_relation_candidates_are_deduped_with_source_chunks`、探针 N1 |
| N2 进程内候选无上限 | nit | 内部软上限 2000/关系（对外仍 20） | `test_real_extractor_caps_internal_candidates_above_soft_limit`、`test_pipeline_caps_merged_candidates_at_internal_soft_limit` |
| N3 F1 无真实 Extractor 用例 | nit | 真 `Extractor.extract` 钉住 30×30=900 | `test_real_extractor_keeps_all_30x30_candidates` |
| N4 owner 空断言 | nit | 删除空转断言，改为可失败的缓存副作用断言 | `test_cached_backfill_uses_explicit_cache_without_mutating_owner` |
| N5 ARCHITECTURE setattr 口径 | nit | 文档改为「抽取器持有、显式传入，函数内部不写 owner」 | `docs/ARCHITECTURE.md` 回填只扫一次段 |
| N6 单块覆盖启发式只看块数 | nit | 同时校验 `start==0 && end==len(document_text)` | `test_partial_single_chunk_still_rescans_document_for_aliases` |
| N7 `reconciled_unattributed` 不进看板 | nit | RunRecorder 字段 + `_backfill_fields` + 落库列/聚合 | `test_recorder_exposes_reconciled_unattributed`、探针 N7 |
| N8 序列化浅拷贝 | nit | 所有嵌套容器/条目的深副本 | `test_serialize_backfill_deep_copies_every_nested_container`、探针 N8 |
| N9 四个布尔跟同一 slim | nit | `MergeMode.LEGACY/SLIM` 单一开关，关键字保留为 shim | `test_merge_mode_slim_is_a_single_switch_for_all_four_behaviors`、探针 N9 |
| N10 `limit<=0` 语义反转 | nit | `None=全量 / 0=空 / 负数 ValueError` | `test_relation_candidate_cap_rejects_negative_limit`、探针 N10 |

## S1（should）SLIM 存值规范化，入库解析真实实体

- 位置：`src/moray/extraction/pipeline.py` `naive_merge`。
- 修法：`strip_entity_name` 打开时实体名存 `e.name.strip()`；
  `strip_relation_endpoints` 打开时 `model_copy(update={"head": head.strip(),
  "tail": tail.strip()})`。去重键与存值同口径，且不改写调用方 partial。
- 复现（`03-r12-probe-output.txt`）：混用 `" 腾讯"` / `"腾讯"`、`"腾讯 "` /
  `"腾讯"` 后：

  ```text
  S1 merged_entities= ['腾讯', '混元'] relations= [('腾讯', '混元')]
     ingest_entities= ['混元', '腾讯'] placeholder_misc= []
  ```

  修前实体的落库名是 `" 腾讯"`、关系 head 是 `"腾讯 "`，`Ingestor._resolve`
  精确匹配失败并新建 MISC 占位。

## S2（should）歧义去重线性化

- 位置：`src/moray/extraction/local_backfill.py` `backfill_provenance`。
- 修法：`any(am.start == start and am.surface == literal for am in ...)` 改为
  `seen_ambiguous: set[tuple[str, int]]`。区间被占用后起点唯一，键足够。
- 复现（探针 S2）：16000 次全歧义命中 **0.0992s**（修前红测 2.02s），
  `entries=16000`。20k 通用线性守护用例继续通过。

## N1（nit）重叠块关系证据候选去重

- 位置：`local_backfill.py` `merge_backfill_reports`。
- 修法：按 `(head_start, tail_start, evidence_start, evidence_end)`（平移后
  全局坐标）去重；保留首条并聚合 `chunk_ids`；同一候选在别块被 selected 时
  先标真，全部合并后按块顺序只留一条 `selected=True`。
- 复现（探针 N1）：两块完全重叠
  `merged_candidates=1 chunk_ids=['d#c0', 'd#c1'] selected=1`。

## N2（nit）进程内候选软上限

- 位置：`local_backfill.py` 新增
  `MAX_INTERNAL_RELATION_EVIDENCE_CANDIDATES=2000`；`extractor.py` 与
  `pipeline.py` 的块/整篇序列化都用它，对外边界仍是 20。
- 复现（探针 N2）：真 Extractor 30×30 保留 900；50×50（2500）被软上限裁到
  **2000**。这样 05 结果缓存与 `ExtractionOutput` 不再携带无上限笛卡尔积。
- 文档：`docs/ARCHITECTURE.md` 关系证据候选段写明「块间全量、进程内软上限
  2000、出网 20」。

## N3（nit）真实 Extractor 用例

- 位置：`tests/test_r12_review_fixes.py` 的 `_RealExtractorStub` 走真实
  `Extractor.extract`（不是自己调 `to_dict(None)` 的 fake）。
- 断言：`30×30` 文本返回 `out.backfill` 候选 **900** 条。把
  `extractor.py` 的 `to_dict(None/2000)` 改回默认 `to_dict()`（20）本用例必红。

## N4（nit）删除空转断言

- 位置：`tests/test_r11_review_fixes.py`
  `test_cached_backfill_uses_explicit_cache_without_mutating_owner`。
- 修法：删除「owner 从未传入却断言无属性」的断言，改为可失败检查：
  `len(cache)==1` 且缓存中的产物对象就是首次返回的 `filled_a`。
  显式 cache 仍是唯一副作用，`model_dump_json` 调用数 0。

## N5（nit）ARCHITECTURE 与 setattr 一致

- 文档改为：cache 由抽取器在本次 reply 的 agent 上持有
  （`Extractor.extract` 显式 `setattr`），并显式传给中间件/回填函数；
  **缓存函数内部不再往 owner 写私有属性**。代码事实见
  `extractor.py` 的 `setattr(agent, "_audit_backfill_cache", ...)` 与
  `middlewares.py` 的 `getattr`。

## N6（nit）单块必须覆盖全文

- 位置：`pipeline.py` `run`。
- 修法：`single_chunk_covers_document = len(chunks)==1 and
  chunks[0].start==0 and chunks[0].end==len(document_text)`。
- 复现（探针 N6）：单块只覆盖前缀时，块外真实存在的别名不再被判幻觉，
  `unmatched_aliases=[]`。

## N7（nit）撤销归属数进入看板

- 位置：`observability/dashboard.py`、`observability/schema.sql`。
- 修法：`RunRecorder.reconciled_unattributed`（默认 0=无撤销）；
  `_backfill_fields` 暴露；`pipeline_runs` 新增列（含 `ADD COLUMN IF NOT
  EXISTS` 平滑升级）；`record/summary/recent` 同步读写聚合。
- 复现（探针 N7）：`dashboard_fields` 含 `'reconciled_unattributed': 2`；
  未传 backfill 时字段仍为 0，不伪造。

## N8（nit）序列化边界深拷贝

- 位置：`local_backfill.py` `serialize_backfill_for_output`。
- 修法：`matched_literals` / `unmatched_aliases` / `seen_literals` /
  `ambiguous_mentions`（含 `candidates`/`chunk_ids`）/
  `relations_without_evidence` / 候选 dict 全部返回副本。
- 复现（探针 N8）：修改输出中的嵌套容器后原字典不变，`deep_copy=True`。

## N9（nit）MergeMode 单一开关

- 位置：`pipeline.py` 新增 `MergeMode.LEGACY|SLIM`，`naive_merge(mode=...)`。
  四个布尔参数保留为兼容 shim：显式传值覆盖模式默认。
- 复现（探针 N9）：`mode=SLIM` → `slim_entities=['腾讯']`、1 条关系（且
  描述补全）；默认 LEGACY → 2 个实体、2 条关系。生产 pipeline 只传
  `mode=MergeMode.SLIM if slim else MergeMode.LEGACY`。

## N10（nit）候选上限语义

- 位置：`local_backfill.py` `_cap_relation_candidates`。
- 修法：`None`=全量、`0`=空、正数=前 N 条；负数 `ValueError`（不再静默
  从 900 掉到 0）。`to_dict` 与 `serialize_backfill_for_output` 共用。
- 复现（探针 N10）：`limit_0=[]`；
  `limit_-5=ValueError: max_relation_evidence_candidates 不能为负...`。
- 文档：`docs/ARCHITECTURE.md` 记录迁移说明。

## 回归与范围

- 全量：391 passed / 65 skipped（`03-r12-full-suite.txt`）。
- 定向：r10/r11/r12 共 36 passed（`03-r12-green-targeted.txt`）。
- 更新旧用例的两处是**语义修正**，不是放宽断言：
  1. `test_naive_merge_identity_key_can_ignore_surrounding_whitespace`
     期望存值 `"腾讯"`（原 `" 腾讯"` 是 S1 的 bug 本身）；
  2. `test_merged_candidates_have_exactly_one_selected` 期望重叠块候选
     1 条 + `chunk_ids` 两块（N1 的新口径）。
- 未改验收 8/9 的数值与 gate；未翻默认开关；未跑 DB/真实 LLM（与本轮
  改动无关，评审同样未跑）。
