# 工单 03 第 15 轮评审修复证据（2026-11-03）

- 分支：`yzc/tmp-yzd-feat-relation-202610-03-mention-span`
- 起点：`77b6828`（r14 修复后的定点）；本轮只处理
  `reviews/03-r15-minirag-review.md` 的 2 should + 6 nit，不做顺手重构。
- 代码/测试提交：`a5e1ca2`（随后只追加本证据与文档）。
- 开关：`MORAY_LOCAL_BACKFILL` 仍**默认关闭**；关闭态 golden 未改。
- 先红后绿：在 `77b6828` 的 `git archive` 副本里跑本轮新增用例，
  **5 failed / 1 passed**（`03-r15-red-tests.txt`）；实现后本轮相关用例
  **74 passed**（`03-r15-green-targeted.txt`），全量 **414 passed / 65 skipped**
  （`03-r15-full-suite.txt`）。

## 逐条处理

| # | 评审发现 | 处理 | 证据 |
|---|---|---|---|
| F1 | should：跨块重叠时「块内已判同名歧义、其他块只抽到其中一个实体」仍会写 mention，与 `ambiguous_mentions` 自相矛盾 | `reconcile_span_ambiguity` 把 `report.ambiguous_mentions` 中 `candidates > 1` 的全局 span 当作**虚拟 owner**：落在这类 span 上的 mention 一律撤销；`_upsert_ambiguous_mention` 去重键改为 `(surface,start,end)`，候选按并集合并，不再按候选集合拆成两条 | `test_overlap_block_ambiguity_blocks_later_single_owner_mention`（修前红）：3 块完全重叠，块 0 ORG+MISC、块 1/2 仅 ORG → 两实体 `mentions == []`，歧义仅 1 条且 candidates 仍含 ORG+MISC；`03-r15-red-tests.txt` |
| F2 | should：验收 8/9 证据停在 `0df406c`，未覆盖 r14 改动后的 HEAD | 在 r15 代码 HEAD `a5e1ca2` 用 02 压力语料重跑 full/slim 3 轮（真实 DeepSeek，temperature=0），产出 `03-r15-stress-ab.json`；summary 的权威证据切换到 r15，r13 JSON 降为历史快照 | `03-r15-stress-ab.json`：`git_sha=a5e1ca2`、`git_dirty=false`；aggregate 见下表 |
| F3 | nit：跨块合并后 `relations_without_evidence` 会留下“其实有证据”的三元组 | `merge_backfill_reports` 合并结束后按三元组过滤：只要 `relation_evidence_candidates` 有该键，就从 missing 列表删除；真正无证据的关系保持不动 | `test_merge_drops_missing_relation_when_overlapping_block_has_evidence`（修前红）、`test_merge_keeps_missing_relation_without_any_evidence`（防误删，修前绿） |
| F4 | nit：跨块候选窗口不是“全局最近 K 条”，块 0 的候选全部排在块 1 前 | 跨块合并后每个 bucket 稳定排序：`selected`（首个提供证据的块）固定首条，其余按全局 `(distance, head_start, tail_start)` 升序；对外 `to_dict` 截断因此不会因块顺序丢掉更近的候选 | `test_merged_candidates_are_globally_distance_sorted`（修前红）：块 0 距离 50/60、块 1 距离 1/2 → 输出 `[50(selected),1,2,60]`，裁剪到 2 条仍保留 `[50,1]` |
| F5 | nit：验收 1 的“原始 185fb41 先红”不可追溯 | 不改历史，只在 summary 验收表与本节明确写：原始提交的红测试记录不存在，可核范围是 r2+ 每轮修复；不再声称“原始功能先红” | `03-local-backfill-summary.md` 验收 1 行；本文件与 `03-r2-red-tests.txt`、`03-r15-red-tests.txt` |
| F6 | nit：名为“开关关闭逐字段一致”的测试只断言 schema 默认值 | 测试改名 `test_switch_off_does_not_backfill_and_keeps_field_defaults`，docstring 明确它只覆盖关态不触发回填/字段默认值，并指向真正的 golden 用例 | `tests/test_backfill_extractor.py`；全量 414 passed，golden 用例 `test_switch_off_extraction_output_matches_262319b_golden`、`test_full_schema_and_prompt_match_pre_change_golden` 仍绿 |
| F7 | nit：验收 5“保留全部候选”被收窄为 2000/20（已披露） | 不改行为、不新增配置面；把 summary 验收 5 的措辞从“有条件达成”改为**“对工单的明确偏离，待工单 owner 确认”**，并列出进程内/出网两个上限；r14 F1 测试继续钉住上限 | `03-local-backfill-summary.md` 验收 5 行、`docs/ARCHITECTURE.md` 候选段；`test_relation_evidence_pairing_without_punctuation_is_bounded` |
| F8 | nit：API 与 jobs 两条主链重复回填取值/序列化；`source_text` 回退也重复 | 新增 `resolve_backfill(reviewed, sink)` 统一“取 reviewed.backfill 或 sink → 出网裁剪”；新增 `Document.audit_text` 属性统一 `source_text` 空字符串语义；API、jobs、extractor、pipeline 四处改为调用 | `test_resolve_backfill_prefers_reviewed_then_sink_and_caps`、`test_document_audit_text_preserves_empty_source_text`（修前红）；`test_extract_endpoint_caps_backfill_candidates` 与全量 API/编排用例仍绿 |

## 验收 8/9：r15 HEAD 重放

命令（密钥只从本地 env 读取，不落仓；跑前已提交，保证 `git_dirty=false`）：

```bash
PYTHONPATH=src python tools/backfill_stress_ab.py \
  --gold data/gold/stress --env-file ../moray/.env \
  --max-units 1500 --overlap-units 200 --concurrency 6 --repeat 3 \
  --json-out docs/evidence/03-r15-stress-ab.json
```

`03-r15-stress-ab.json`：`git_sha=a5e1ca2`、`git_dirty=false`、
`model=deepseek-flash`、`temperature=0`、3 篇 stress seeds（79,362 字）。

| 轮 | 实体 macro relaxed Δ | 关系 F1 | 单块 output token | 截断块 |
|---|---|---|---|---|
| 1 | −0.15pt（worst） | 0.3679→0.6345 | 2667.22→637.63 | 4→0 |
| 2 | +1.32pt | 0.3743→0.6862 | 2230.10→597.83 | 3→0 |
| 3 | +0.72pt | 0.2247→0.7781 | 2935.17→551.36 | 7→0 |
| 聚合 | 0.8934→0.8997，**mean +0.63pt / worst −0.15pt** | 0.3223→0.6996 | 2610.83→595.61（−76.83%） | 7→0 |

- 门槛核对：worst-of-N −0.15pt 在 ≤0.5pt 内；关系 F1 不下降；span 一致率
  min/mean 0.0093/0.0164→1.0/1.0；截断 7→0。
- **逐篇残留（如实披露）**：逐篇最差 **−4.55pt**（`stress_seed20261104`
  第 1 轮；该篇三轮 −4.55 / 0.00 / 0.00、均值 −1.52pt）。另两篇逐篇均值
  +2.28pt / +1.13pt。「≤0.5pt」是单轮 **macro** 的 mean-of-3 + worst-of-N
  口径，不等价于逐篇单轮都达标；翻默认前需单独评估该波动。
- 与 r13 证据的关系：`03-r13-stress-ab.json`（`0df406c`）保留为历史快照，
  不再冒充当前 HEAD。

## 验收条目未回退确认

本轮只碰跨块歧义归属、候选排序、missing 关系对账、回填取值/审计原文入口
与测试命名；未改 schema/prompt、默认开关或 262319b 关闭态语义。

- 验收 2/3/4：多模式回填、最长匹配、别名/幻觉用例全绿。
- 验收 5：证据定位用例全绿；候选窗口语义按 F4 修正，上限口径按 F7 明确
  为**未获 owner 确认的偏离**。
- 验收 6：单块歧义 + 跨块同 span 对账 + 本轮“单归属覆盖”新用例全绿。
- 验收 7：默认开关仍关闭；schema/prompt/输出 golden 用例全绿。
- 验收 8/9：r15 HEAD `a5e1ca2` 重放达成（见上表）。
- 验收 10：文本扫描仍单趟线性；本轮只加集合查表/稳定排序，没有逐名字
  全文扫描。
