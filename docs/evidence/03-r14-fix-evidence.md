# 工单 03 第 14 轮评审修复证据（2026-11-03）

- 分支：`yzc/tmp-yzd-feat-relation-202610-03-mention-span`
- 起点：`0734f46`（r13 修复后的定点）；本轮只处理 `review-r14-03.md` 的
  1 should + 4 nit，不做顺手重构。
- 开关：`MORAY_LOCAL_BACKFILL` 仍**默认关闭**；关闭态 golden 未改。
- 先红后绿：在 `0734f46` 的 `git archive` 副本里跑本轮新增用例，
  6 failed / 1 passed → 实现后 7 passed。
  - 红：`03-r14-red-tests.txt`
  - 绿（本轮相关用例 + r8–r13/local_backfill/缓存/入库回归）：
    `03-r14-green-targeted.txt`（134 passed / 4 skipped）
  - 全量：`03-r14-full-suite.txt`（408 passed / 65 skipped）
  - 确定性探针：`03-r14-probe-output.txt`（`tools/backfill_r14_probe.py`）

## 逐条处理

| # | 评审发现 | 处理 | 证据 |
|---|---|---|---|
| F1 | should：关系证据配对在同句/无标点块上先物化全部 `H×T` 再裁到 2000，6000 字 1000×1000 实测 5.6s/440MB | 句内尾起点排序后，逐头从最近位置向两侧扩张；用容量 2000 的大顶堆只保留「最近 N 对」（距离相同按 `head_start/tail_start` 稳定排序）。已保留满 N 对且下一个候选比堆顶更远时立即停止该头，**不再先把全笛卡尔积 append 成 list**。容量以内结果与旧 `sorted(pairs)` 逐值一致 | `test_relation_evidence_pairing_without_punctuation_is_bounded`（旧实现 250k 条候选 → 红）、`test_bounded_pairing_matches_bruteforce`（把上限压到 5，与暴力排序逐值对比）；探针 F1 |
| F2 | nit：别名没有存值规范化，与 r13 N3 的标准名/关系端点口径不一致 | 新增 `_normalize_aliases()`：strip、去空、去重、跳过与标准名相同的项；`_coalesce_entities`、`backfill_provenance` 输出与 `merge_entity_metadata(strip_aliases=True)`（仅 SLIM/回填路径）共用。LEGACY 归并保持原样，关闭态逐字段一致 | `test_backfill_normalizes_alias_values`、`test_ingestor_receives_clean_alias_values`（回填产物送 Ingestor，落库入参别名已干净）；探针 F2 |
| F3 | nit：`_PAYLOAD_VERSION=1`，但 r13 N3/N4 改了缓存载荷取值语义 | 递增到 `2`，键命名空间整体切换，升级前的 05 结果缓存条目不再命中/回放。**升级需失效 05 缓存**（旧条目自然过期或按命名空间清理） | `test_payload_version_bumped_for_backfill_value_normalization`；探针 F3 |
| F4 | nit：验收 8 的「均在 ≤0.5pt 内」只对单轮 macro 成立，逐篇仍有 −1.59pt | 保持 mean-of-3 + worst-of-N 口径不变，在 summary 与 r13 证据里显式补充逐篇残留：`stress_seed20261104` 第 2 轮 **−1.59pt**（该篇三轮 +0.65/−1.59/+3.32、均值 **+0.79pt**） | `03-r13-stress-ab.json` 复算（探针 F4）；`03-local-backfill-summary.md`、`03-r13-fix-evidence.md` |
| F5 | nit：负数候选上限每次序列化都 WARNING，坏配置刷日志 | 模块级 `_NEGATIVE_CANDIDATE_LIMIT_WARNED` 按具体非法值去重：首次 WARNING，后续同值 DEBUG；clamp 行为不变 | `test_negative_candidate_limit_warns_once`；探针 F5。r13 旧用例改为断言前清一次去重状态（仍钉住「首次出现有 WARNING」） |

## F1 性能/内存对照（本机，probe 真实输出）

```text
3000 字 500×500: 旧(模拟全量物化+排序)=0.386s/37.7MB/250000 对;
                 新=0.189s/2.4MB/2000 条候选
6000 字 1000×1000: 新=0.184s/3.5MB/2000 条候选
```

- 评审在旧实现上实测 6000 字为 **5.57s / 440MB 峰值**；本轮新实现同形态
  降到 **0.184s / 3.5MB**（tracemalloc 口径），且保留的仍是最近的一批候选。
- 复杂度：每个头只按距离向外扩张到「当前第 N 近」为止，已满 N 后通常只看
  O(1)~O(log T) 个尾；不再随着无标点块长度增长而把全积留在内存。
- 候选口径同步写进 `docs/ARCHITECTURE.md` 与 summary 验收 5：**进程内每条
  关系最多保留最近 2000 条，出网 20 条**；不再声称「内存保留全量」。

## 验收条目未回退确认

本轮只碰关系配对的有界化、别名存值、缓存版本号与日志/文档；验收 1/2/3/4/
6/7/9/10 的实现语义未改，全量 `03-r14-full-suite.txt` 408 passed / 65 skipped。

- 验收 2/3/4/6：多模式回填、最长匹配、别名/幻觉、歧义未归属用例全绿。
- 验收 5：证据定位用例全绿；候选上限口径与 r13 N2 一致（本轮只把「先物化
  再裁」改成「边配对边裁」，不扩大也不缩小对外窗口）。
- 验收 7：默认开关仍关闭；关闭态 schema/prompt/归并 golden 用例全绿；
  LEGACY 归并不传 `strip_aliases`，别名存值保持 262319b 行为。
- 验收 8：本轮不改评测数字，只按 F4 补充逐篇披露；权威证据仍是
  `03-r13-stress-ab.json`（git_sha=0df406c）。
- 验收 9：本轮未改 schema/prompt，token/截断数字沿用同一证据。
- 验收 10：文本扫描仍单趟；关系配对从「全积物化+排序」降为「有界堆 +
  距离向外扩张」，无标点守护用例新增。
