# 工单 03 第 11 轮评审修复证据（r11）

- 分支：`yzc/tmp-yzd-feat-relation-202610-03-mention-span`（与评审同一 checkout）
- 评审报告：`/tmp/opencode/review-r11-03.md`；被评审 HEAD：`93ac8d7`
- 方式：先红后绿。先提交 `tests/test_r11_review_fixes.py`（11 failed），
  再逐条实现。
- 测试环境：`ARK_API_KEY=dummy /home/dell/orca/workspaces/minirag/moray/.venv/bin/python
  -m pytest -p no:cacheprovider`（无 DB / 无真实 LLM；本 worktree 的 `.venv`
  为空，复用主仓 `moray/.venv` 的依赖，`pytest` 的 `pythonpath=src` 指向本
  worktree 源码）。
- 全量结果：**379 passed / 65 skipped**（`03-r11-full-suite.txt`；r10 为
  368 passed，本轮新增 11 条用例）。
- 定点探针：`tools/backfill_r11_probe.py` → `03-r11-probe-output.txt`
  （确定性、无模型/无网络，逐条复现 F1 与 N1/N2/N3/N4/N5/N8/N10/N11）。

## 结论速览

| 发现 | 级别 | 处理 | 证据 |
|---|---|---|---|
| F1 分块路径把候选截到 20 | should | 块间/内存传全量；只在 API/作业落盘裁剪 | `test_chunked_pipeline_preserves_all_relation_evidence_candidates`、`test_serialize_backfill_for_output_caps_candidates_and_keeps_selected`、`test_extract_endpoint_caps_backfill_candidates` |
| F2 验收 8 HEAD 未重放且 worst seed 超门槛 | should | 如实改为「未达成」+ 保留默认关 + 写清 HEAD 重放命令；无凭证不伪造数值 | 本文「F2」节、`03-local-backfill-summary.md` 验收表 |
| N1 S2 歧义条无溯源/可与 merge 条目重复 | nit | 复用 merge 去重索引；pipeline 反查来源块写 `chunk_ids` | `test_reconciled_ambiguity_is_deduped_and_carries_source_chunks` |
| N2 撤销归属后计数不重算 | nit | 新增 `reconciled_unattributed`，保留 chunk-weighted 口径并说明 | `test_reconcile_counts_revoked_mentions_without_reweighting_blocks` |
| N3 `to_dict()` 候选共享引用 | nit | 所有容器/条目序列化时返回副本 | `test_serialized_candidates_are_copies_and_do_not_mutate_report` |
| N4 缓存破坏纯度 + 每次全量序列化 | nit | 显式 `cache: dict`；轻量内容指纹做键；不再 `setattr`/`model_dump_json` | `test_cached_backfill_uses_explicit_cache_without_mutating_owner`、`03-r11-probe-output.txt` |
| N5 `_cap` 死分支 / `limit<=0` 语义 | nit | 只认 `None` 为全量；`<=0` 返回空；删除死分支 | `test_relation_candidate_cap_semantics` |
| N6 中间件注释与代码相反 | nit | 注释改为「零 span 记 None（未测），命中率写 block_confidence」 | `src/moray/observability/middlewares.py` |
| N7 ARCHITECTURE 漏 S2 开关门禁 | nit | 明确「仅在开启回填且存在块信号时」 | `docs/ARCHITECTURE.md` |
| N8 名义「每块一次」仍有整篇补扫 | nit | 单块文档跳过补扫；多块保留并写明口径 | `test_single_chunk_merge_skips_document_alias_rescan` |
| N9 证据文档/契约样例不齐 | nit | 更正 diff 措辞、候选真实值；contract 补非空 `unmatched_aliases`（带 `type`）与 `reconciled_unattributed` | `03-r10-fix-evidence.md`、`03-local-backfill-contract.json` |
| N10 测试名不副实 / overlap 重复 mention | nit | 同 owner 同 `(start,end)` 收敛为一条；测试补 mention 数断言 | `test_overlap_same_entity_duplicate_span_is_collapsed_to_one_mention`、r10 同名用例 |
| N11 关系键未同步去空白 | nit | slim 关系去重键对 head/tail `strip()`，关闭态保持精确键 | `test_slim_naive_merge_relation_key_ignores_whitespace_but_legacy_keeps_it` |

## F1（should）块间传递不再截断候选，只在落盘边界裁剪

- 位置：`extractor.py`（`ExtractionOutput.backfill`）、`pipeline.py`（汇总）、
  `api/app.py` / `jobs/handlers.py`（落盘边界）、`local_backfill.py`
  （`serialize_backfill_for_output`）。
- 修法：
  1. `Extractor.extract` 返回 `backfill.to_dict(max_relation_evidence_candidates=None)`；
  2. `pipeline.run` 的 `BackfillReport.from_dict` 看到全量候选，合并后仍用
     `None` 序列化，**分块边界不再是一次裁剪**；
  3. 只有 API 响应/作业结果在 `serialize_backfill_for_output(...)` 里按
     `MAX_SERIALIZED_RELATION_EVIDENCE_CANDIDATES=20` 裁剪（selected 在首条）；
  4. `to_dict()` 默认上限保留，供显式序列化方使用。
- 探针（`03-r11-probe-output.txt`）：
  `in-memory: 900 | to_dict(None): 900 | pipeline out: 900`，
  `API/job 边界: 20 selected= 1`。
- 用例：分块集成用例断言合并后 900 条；API 用例注入 900 条的
  `ReviewedExtraction`，断言 `/extract` 响应只出 20 条且 selected 恰 1 条。
- 附带：删除「selected 不在前 limit 就替换末条」的不可达分支（见 N5）。
  `selected` 始终由生产方放在 index 0，前 limit 条天然包含它。

## F2（should）验收 8 仍未达成：无凭证不能重放，不伪造数值

- 事实：本机没有任何真实 LLM 凭证（仓库无 `.env`，环境变量无 `ARK_API_KEY`
  等）；`tools/backfill_stress_ab.py` 在无密钥时按设计直接失败，不会用规则
  模式冒充。实测命令与原始报错见 `03-r11-f2-no-credentials.txt`：
  `moray.config.ConfigError: 未找到 ARK_API_KEY（当前 LLM_PROVIDER=volcengine）`。
- 因此本轮**没有**新的 A/B 数值。唯一快照仍是 `9f95ed4` 的
  `03-r3-stress-ab.json`：mean-of-3 = +0.0733pt，但 worst-of-N −1.30pt、
  seed20261104 逐篇均值 −2.88pt 超过 ≤0.5pt 门槛。
- 处理：把验收 8 的状态从「达成（mean-of-3）」改为 **「未达成」**
  （`03-local-backfill-summary.md`），默认开关 `MORAY_LOCAL_BACKFILL` 保持关闭；
  翻默认开关前的前置门禁不变。
- HEAD 重放命令（有凭证时）：

  ```bash
  PYTHONPATH=src python tools/backfill_stress_ab.py \
    --gold data/gold/stress --env-file <凭证文件> \
    --repeat 3 --temperature 0 \
    --json-out docs/evidence/03-r11-stress-ab-head.json
  ```

  报告必须同时给 mean-of-N 与 worst-of-N；任一 seed 仍超 −0.5pt 就继续
  保持默认关，不得把验收 8 写成达成。
- 说明：这是度量缺口，不是代码缺陷；没有修改工单门槛（改门槛需要产品/
  协调者决定）。

## N1（nit）跨块 span 歧义：复用 merge 索引 + 来源块

- 修法：抽出 `_upsert_ambiguous_mention`，`merge_backfill_reports` 与
  `reconcile_span_ambiguity` 共用同一 `(surface,start,end,candidates)` 索引；
  pipeline 在收集块结果时建 `span_chunk_ids`（全局 span → 来源块列表），
  对账时写进 `AmbiguousMention.chunk_ids`。
- 探针：三块重叠（块 0 判歧义、块 1 归 ORG、块 2 归 MISC）→
  `ambiguous 条数: 1 | chunk_ids: ['d#c0', 'd#c1', 'd#c2']`；
  看板 `len(ambiguous_mentions)` 不再虚高。

## N2（nit）撤销归属的确定性计数

- 修法：`BackfillReport.reconciled_unattributed` 统计被撤销的 mention 数，
  `from_dict/to_dict/merge` 全链路透传；`entities_grounded/block_confidence`
  仍是 chunk-weighted（不反算，文档已说明原因——重叠块各计一次是既定口径）。
- 探针：三块重叠场景 `reconciled_unattributed: 2`，
  `entities_grounded/total: 2/4`（块加权，未重算）。

## N3（nit）序列化结果与内存报告隔离

- 修法：`_cap_relation_candidates` 返回 `[dict(c) ...]`；`to_dict()` 对
  `matched_literals/unmatched_aliases/seen_literals/ambiguous_mentions/
  relations_without_evidence` 全部返回副本。
- 探针：篡改序列化候选后 `内存候选未被改写: True`。

## N4（nit）显式缓存，保持纯函数与「只扫一次」

- 修法：`cached_backfill_provenance(cache: dict, result, text)`；抽取器在
  `extract()` 里建一个字典挂到 agent（与既有 `_audit_source_text` 同一种
  接线），中间件显式取同一个字典传入；函数内部不再 `setattr`。
- 键的选择**偏离了评审建议的 `id(result)`**：质量中间件会把
  `event.structured_output` 重新 `model_validate` 成新对象，与抽取器
  `to_core_result` 的产物对象不同；只认对象身份会让「同一 reply 只扫一次」
  失效（实测计数会从 1 变 2）。改用回填真正依赖的轻量内容指纹
  `(原文, 实体(type,name,aliases), 关系(head,predicate,tail))`，不调用
  `model_dump_json`，既保持命中又不全量序列化。
- 探针：`scans: 1 | model_dump_json: 0`。

## N5（nit）候选上限语义

- 修法：`limit is None` → 全量；`limit <= 0` → `[]`；否则前 `limit` 条副本。
  删除不可达的「替换末条」分支。
- 探针：`limit=None: 900 / 0: 0 / -5: 0 / 5: 5`；`5` 条里首条 selected。

## N6 / N7（nit）注释与架构文档

- `middlewares.py`：注释改为「零 span 记 `None`（未测）；确定性命中率写到
  `block_confidence`」，与 `rec.span_ratio = report.ratio if report.total
  else None` 一致。
- `docs/ARCHITECTURE.md`：S2 节补「仅在开启本地回填且存在块信号时」；
  同时更新候选全量/落盘裁剪、显式缓存、关系端点去空白与单块补扫口径。

## N8（nit）单块文档不再第二次扫全文

- 修法：`merge_backfill_reports(..., single_chunk_covers_document=False)`；
  `pipeline.run` 在 `len(chunks) == 1` 时传 True，跳过别名补扫。多块或部分
  块失败时保持默认 False，仍按整篇补扫。
- 探针：`单块补扫次数: 0 | 多块路径补扫次数: 1`。
- 验收 10 的口径同步写明：块扫描每块一趟；整篇补扫是不同用途的第二次线性
  扫描，单块文档已省略。

## N9（nit）证据文档与契约样例

- `03-r10-fix-evidence.md`：S2 的 candidates 改为真实 JSON 字符串
  `["[\"ORG\", \"苹果\"]", "[\"MISC\", \"苹果\"]"]`；N1 段把「diff 仅新增
  这两个字段」更正为「还补了两段 `seen_literals`」。
- `03-local-backfill-contract.json`：补非空 `unmatched_aliases`
  （`{"type":"ORG","entity":"阿里巴巴","literal":"Acme"}`），两个 backfill
  样例都补 `reconciled_unattributed`；响应/模型输出里的别名数组同步含
  `Acme`，样例自洽。

## N10（nit）同 owner 同 span 去重

- 修法：`reconcile_span_ambiguity` 先按实体收敛相同 `(start,end)` 的
  mentions，再做冲突检测；`test_overlap_same_entity_keeps_single_span_mention`
  的测试名与断言一致（`mentions == ["苹果"]`）。
- 探针：`同 owner 同 span mention 数: 1`。

## N11（nit）关系去重键同步去空白

- 修法：`naive_merge(..., strip_relation_endpoints=False)`；slim 分块路径传
  True，对 `head/tail` 做 `strip()` 后做去重键；关闭态保持 262319b 精确键。
- 用例/探针：只差空白的同一关系 `slim: 1 | legacy: 2`。

## 测试命令

```bash
# 先红（实现前）
ARK_API_KEY=dummy <venv>/python -m pytest -q tests/test_r11_review_fixes.py
# 11 failed -> docs/evidence/03-r11-red-tests.txt

# 修复后全量
ARK_API_KEY=dummy <venv>/python -m pytest -q -p no:cacheprovider
# 379 passed, 65 skipped -> docs/evidence/03-r11-full-suite.txt
```

DB 用例（`RUN_DB_TESTS=1`）本轮未跑：本机无 pgvector；本轮改动不涉
SQL/迁移。API 边界用例用 `httpx.ASGITransport` + 注入假依赖，不依赖 DB。
