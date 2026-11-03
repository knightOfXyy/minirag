# 工单 03 第 9 轮评审（r9）修复证据

- 分支：`yzc/tmp-yzd-feat-relation-202610-03-mention-span`
- 修复对象：r9 `review_verdict: changes-requested`，`findings=12`
  （1 should + 11 nit，无 blocker），报告 `/tmp/opencode/review-r9-03.md`
- 固定点：工单基线 `262319b`。本轮只改 r9 指出的点，不做顺手重构。
- 先红：`docs/evidence/03-r9-red-tests.txt`（8 failed / 4 passed）
- 修复后：`docs/evidence/03-r9-green-targeted.txt`（19 passed）、
  `docs/evidence/03-r9-full-suite.txt`（**355 passed / 65 skipped**）

## 一、逐条处理

### S1（should）看板全 NULL 回填列不再 coalesce 成 0%

- 位置：`src/moray/observability/dashboard.py` `Dashboard.summary`
- 改动：`avg(block_confidence)`、`sum(entities_grounded)`、
  `sum(entities_total)`、`avg(relation_evidence_ratio)` 去掉
  `coalesce(...,0)`，与同文件 `avg(span_ratio)`、`schema.sql` 的
  「未知 = NULL」口径一致。`ambiguous_mentions` 仍是
  `INTEGER NOT NULL DEFAULT 0`，保留原聚合。
- 用例：`test_dashboard_all_unmeasured_backfill_stays_null`
  - 假 pool 返回全 NULL 聚合行：四个值必须都是 `None`；
  - 同时断言生成的 SQL 不再含这四列的 `coalesce(avg/sum(...),0)`。
- 结果：先红 `assert 0.5 is None`（旧 SQL 聚合语义）；
  修复后通过。默认关闭部署的 `/metrics/dashboard` 现在显示「未测」而不是 0%。

### N1（nit）RunRecorder 回填字段只声明一次

- 位置：`dashboard.py` `RunRecorder`
- 改动：删除类体前部的重复 5 个字段（`block_confidence` / `entities_grounded` /
  `entities_total` / `relation_evidence_ratio` / `ambiguous_mentions`），保留
  带「工单 03」分区注释的一组。
- 用例：`test_run_recorder_backfill_fields_declared_once`
  - `dataclass` 的 `__annotations__` 是 dict，重复声明会被静默覆盖；因此直接
    读类源码，按字段注解计数，必须恰好 1 次。
- 结果：先红 `block_confidence 在 RunRecorder 源码里重复声明`；修复后通过。

### N2（nit）空白变体别名不再误报幻觉

- 位置：`src/moray/extraction/local_backfill.py` 别名幻觉判定
- 改动：`literal != e.name` → `literal != e.name.strip()`，与
  `literal_index` / `_coalesce_entities` 的去空白口径一致。
- 用例：`test_whitespace_only_alias_is_not_a_hallucination`
  - 实体 `name=" 腾讯"`、`aliases=["腾讯"]`，正文没有「腾讯」；
    该别名不得进入 `unmatched_aliases`。
- 结果：先红（旧逻辑把 `腾讯` 计入幻觉）；修复后 `unmatched_aliases == []`。

### N3（nit）被长实体吸收的短实体字面不再报「没有出处」

- 位置：`src/moray/orchestration/specialists.py` `rule_review` ⑤
- 改动：`uncovered` 再减去「实体任一标准名/别名出现在
  `BackfillReport.seen_literals` 里」的实体键。`seen_literals` 是 AC 一趟扫描的
  原始命中集合（含被最长匹配吸收的），不新增全文扫描。
- 用例：`test_entity_literal_absorbed_by_longer_entity_is_not_missing`
  - 正文「阿里巴巴发布新品。」，模型给 `阿里巴巴` 与 `阿里` 两个实体；
    `阿里` 只作为长名子串出现。审校必须 PASS，且不得出现
    「未在正文中找到出处」。
- 结果：先红 `assert NEEDS_FIX == PASS`；修复后 PASS。

### N4（nit）重叠块歧义去重不依赖候选顺序

- 位置：`local_backfill.py` `merge_backfill_reports`
- 改动：去重键 `tuple(am.candidates)` → `tuple(sorted(am.candidates))`。
- 用例：`test_ambiguous_mentions_deduped_regardless_of_candidate_order`
  - 两块同一 `(surface,start,end)`，候选顺序相反；合并后只剩 1 条，
    `chunk_ids == ["c0","c1"]`。
- 结果：先红 `len(...) == 2`；修复后 1 条，overlap 不再放大歧义数。

### N5（nit）中间件不再自创第三套 slim 判定

- 位置：`src/moray/observability/middlewares.py`、`extraction/extractor.py`
- 改动：
  - `Extractor.extract` 在 agent 上挂 `_audit_local_backfill =
    self.local_backfill`（与 `_audit_source_text` 同一入口）；
  - `ExtractionMetricsMiddleware` 只在 `_audit_local_backfill is True` 时回填，
    删除「无 mention 且 confidence 全 None」的启发式判据。
- 用例：
  - `test_middleware_does_not_backfill_when_switch_is_off`：开关关闭时
    `block_confidence is None`（不拿旁路扫描冒充信号）；
  - `test_middleware_backfills_when_extractor_switch_is_on`：开关打开时
    `entities_grounded=1 / entities_total=2 / block_confidence=0.5`；
  - `tests/test_observability.py` 两条 slim 用例显式设置该开关。
- 结果：先红（关闭态仍算出 0.5）；修复后两条都通过。
- 说明：`build_observability_stack` 目前仍未在 `src` 主链接线（评审已确认
  只是旁路风险）；即使接线，是否回填也只由抽取器开关决定，不再由中间件猜。

### N6（nit）full 动态关系字段与内核 Relation 对齐

- 位置：`src/moray/schema/factory.py` full 关系模型
- 处理：保留为保住 262319b full schema 逐字节而手抄的字段/文案，但增加 CI
  护栏，禁止内核加字段后动态模型静默漂移。
- 用例：`test_full_dynamic_relation_fields_match_core_relation`
  - `build_extraction_model(GENERAL_DOMAIN)` 的 relations 注解模型字段集合
    必须等于 `Relation.model_fields` 集合。
- 结果：当前通过（护栏用例）；若以后内核 `Relation` 新增字段而未同步，
  该用例会直接变红。

### N7（nit）验收 8 的 owner waiver 显式记在工单 03 内

- 事实：3 轮实体 macro 增量为 +1.00 / −1.30 / +0.52pt（均值 +0.0733）；
  单轮最低 −1.30pt，seed20261104 逐篇均值 −2.88pt，超过工单 ≤0.5pt。
- owner 裁定（2026-11-03，需求 owner 确认）：
  - **验收 8 按 mean-of-3 判达标**，唯一数据快照是
    `docs/evidence/03-r3-stress-ab.json` 的 `git_sha=9f95ed4`；
  - worst-of-N 不满足 ≤0.5pt，**放量翻默认开关前必须用 HEAD 重跑**；
    worst case 同时保留在工单 04 的 gate，超过 −0.5pt 不得翻默认开关；
  - 本裁定只解除工单 03 本轮「是否达成」的口径争议，不改变 worst case 未达标
    这一事实。HEAD（r5~r9 之后）**没有**真实 LLM 重放，本轮不伪造新数值。
- 结论：验收 8 的状态仍写「达成（mean-of-3；快照 9f95ed4，HEAD 未重放）」，
  不再只在工单 04 文档里引用。

### N8（nit）验收 9 证据文档数字与 JSON 对齐

- 位置：`docs/evidence/03-output-token-reduction.txt`
- 改动：删除未入库运行留下的 2074.69 / 520.40、截断 2→0、74.92%；
  改为 `03-r3-stress-ab.json` `delta` 的权威数字：
  - full 2365.98 → slim 582.13 tokens/块，降幅 **75.32%**；
  - 截断块 **6 → 0**。
- 并注明旧数字来自另一次未入库运行、已废弃，避免审阅者猜口径。
- 结果：文档数字与 JSON `delta` 逐项一致（2365.98 / 582.13 / 75.32 / 6 / 0）。

### N9（nit）验收 1 的首版先红如实记为「历史不可追」

- 事实：首版 `185fb41` 的实现与测试同一提交，无法补做独立先红提交；
  r4–r8 的先红提交是真实可核的。
- 处理：不改历史；沿用 `03-r8-fix-evidence.md` 的披露，并在本轮证据开头
  写明「r9 先红 8 failed」可核。后续门禁建议仍是「测试提交先于实现提交」。

### N10（nit）关闭态 confidence=None 分支补固定用例

- 位置：`specialists.py` ④ `r.confidence is None` 分支
- 处理：不改变行为；补一条差分 fixture 之外的固定用例，明确这是内核
  confidence 默认改为 None 后的**刻意扩展**，防止以后被当成回归删掉。
- 用例：`test_off_mode_confidence_none_branch_is_intentional`
  - 关闭态、无证据、`confidence=None` → `NEEDS_FIX`，issues 恰为
    `["关系 腾讯-[发布]->混元 无原文证据（置信度未知）"]`。
- 结果：通过（262319b 对 None 会 TypeError，语义上不可比，故不放进逐字节差分）。

### N11（nit）from_dict 显式 0 不被字典旧值覆盖

- 位置：`local_backfill.py` `BackfillReport.from_dict`
- 改动：`chunk_start` 默认值 `0` → `None`（哨兵）；
  `data.get("chunk_start", 0) if chunk_start is None else chunk_start`。
- 用例：
  - `test_from_dict_explicit_zero_chunk_start_wins`：显式传 0、字典残留 999 →
    必须取 0；
  - `test_from_dict_default_still_reads_fallback_chunk_start`：不传时仍按字典
    999 回读（保留原有兜底语义）。
- 结果：先红 `assert 999 == 0`；修复后两条通过。

### N12（nit）空 source_text 坐标系统一

- 位置：`src/moray/extraction/pipeline.py` 整篇别名补扫
- 改动：`document.source_text or document.text` →
  `document.source_text if document.source_text is not None else document.text`，
  与 `extractor.py` 的 `is not None` 口径一致。空字符串就是「空正文」。
- 用例：`test_empty_source_text_is_not_replaced_by_feedback_body`
  - `Document(text=带反馈正文, source_text="")`，报告里未被本块采集的别名
    `鹅厂` 必须仍是幻觉；旧逻辑会回退到 text，把反馈里的 `鹅厂` 当正文。
- 结果：先红 `unmatched_aliases == []`；修复后保留
  `{"entity": "腾讯", "literal": "鹅厂"}`。

## 二、回归与未动范围

- 全量离线：**355 passed / 65 skipped**（修复前 343 passed / 65 skipped）。
- 未跑 `RUN_DB_TESTS=1`（本机无 pgvector）；S1 用假 pool 断言 SQL + 全 NULL
  返回语义，DB 集成用例 `test_summary_includes_backfill_signals` 仍在仓库里，
  有 DB 的机器可直接复跑。
- 未重放真实 LLM A/B（无凭证）；N7 的数值只引用 `9f95ed4` 快照，不伪造 HEAD。
- 未改 `262319b` golden、未改开关默认值、未新增 HTTP 端点（契约面不涉及本工单）。
- 关闭态逐字段 golden（`test_switch_off_extraction_output_matches_262319b_golden`）
  与 full schema/prompt golden 在全量回归中继续通过。

## 三、复跑命令

```sh
# 先红（在 74a971c 的临时 worktree 里只放本轮测试）
ARK_API_KEY=dummy /tmp/opencode/review-venv/bin/python -m pytest -q \
  tests/test_r9_review_fixes.py

# 修复后定向 + 全量
ARK_API_KEY=dummy /tmp/opencode/review-venv/bin/python -m pytest -q \
  tests/test_r9_review_fixes.py tests/test_observability.py
ARK_API_KEY=dummy /tmp/opencode/review-venv/bin/python -m pytest -q
```
