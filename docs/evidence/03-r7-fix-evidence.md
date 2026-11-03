# 工单 03 第 7 轮评审（r7）修复证据

- 被评审基线：`896ca25`（r6 修复后的 HEAD）。
- 先红提交：`00f169b`（只加 `tests/test_r7_review_fixes.py`）。
- 修复提交：`c5580ff`（`specialists.py` + `ARCHITECTURE.md` + r6 用例校正）。
- 本轮修复面：`src/moray/orchestration/specialists.py` 的 `rule_review` ②③⑤、
  `docs/ARCHITECTURE.md` 审校/重复/命中口径、`tests/test_r6_review_fixes.py`
  （校正两条「测了错的东西」的用例）、新增 `tests/test_r7_review_fixes.py`。
- 先红：只加用例、未改实现时 **3 failed / 1 passed**
  （`docs/evidence/03-r7-red-tests.txt`）。
- 修复后全量离线：`ARK_API_KEY=dummy PYTHONPATH=src pytest -q -p no:cacheprovider`
  → **339 passed, 65 skipped**（`docs/evidence/03-r7-full-suite.txt`）。
- 只改本分支，未动 `dev/yzd-feat-relation-202610` / `main`。

## 一、逐条处理（should 2 / nit 2）

### F1（should）关闭态 ② 与 262319b 逐字面等价

- 改动：`specialists.py:111-127`。关闭态（`backfill is None`）直接走
  262319b 原文逻辑：`if literal in names: return None`——**不 strip、不看
  `owners` 唯一性**；只有瘦身模式保留 `strip` + 标准名豁免 + 唯一别名放宽。
- 端到端差分（评审给出的两个用例，`docs/evidence/03-r7-offmode-diff.txt`）：

  | 输入 | 262319b | r7 HEAD |
  |---|---|---|
  | 实体 `" 腾讯"`，关系 head `" 腾讯"` | 1 次调用 / accepted=True | **1 次调用 / accepted=True** |
  | 实体 `"腾讯"`，关系 head `" 腾讯"` | 3 次调用 / accepted=False | **3 次调用 / accepted=False** |

  两边 `issues` 原文也一致。默认生产模式的「白跑 3 次」回归已消除。
- 六类 fixture 差分（干净端点/别名/带空白名/同名异型/真重复/缺端点）：
  `docs/evidence/03-r7-diff-probe-output.txt`，HEAD 与 262319b 归档的
  `verdict + issues` **逐字节相同**。该差分同时进了仓内回归
  `test_off_mode_rule_review_matches_262319b_on_all_fixtures`（测试内归档
  262319b 函数原文，不依赖测试机保留旧 commit）。
- 新增端到端用例
  `test_off_mode_whitespace_name_endpoint_passes_in_one_model_call`：
  带空白标准名端点 1 次调用 accepted=True（先红时 3 次 / accepted=False）。
- 文档：`ARCHITECTURE.md:73-78` 改成与代码一致——关闭态只认标准名原样
  精确命中，不 strip、不看归属唯一性；瘦身模式才报「归属不唯一或未知」。
- 校正 r6 用例：`test_off_mode_same_name_different_type_endpoint_not_exempted`
  改成 `..._matches_262319b`，断言基线只有 ③「未去重」、**没有** ②
  端点问题（原用例把 r6 新引入的偏离行为当成了基线）。

### F2（should）验收 8 口径：owner 裁定 mean-of-3，worst-of-N 并列披露

- 处理：不改代码、不改门槛。owner 2026-11-03 裁定按工单原文
  **mean-of-3** 判达标（+0.0733pt ≤ 0.5pt），但要求同一份证据并列
  worst-of-N，并给工单 04 留 gate 接口。
- 口径钉死（可复算）：3 轮重复、`temperature=0`、语料 3 篇
  （seed20261102/03/04）；`mean([+1.00, -1.30, +0.52]) = +0.0733pt`。
  数据入口 `docs/evidence/03-r3-stress-ab.json` 的
  `repeats[*].delta` / `aggregate.delta.entity_macro_f1_delta_pt`。
- worst-of-N 并列披露：单轮最低 **−1.30pt**、seed20261104 逐篇均值
  **−2.88pt**。文档原句：**「均值达标、最差值未达标，放量决策须另行评估」**。
- 可复现性：证据 `git_sha=9f95ed4` 早于 r5/r6/r7。`9f95ed4..HEAD` 的差异
  只有审校端点/重复口径、`local_backfill` 关系端点 strip 与整篇别名补扫、
  05 缓存键、聚合脚本 min/max 口径；**不涉及实体/关系匹配、prompt/schema**，
  数字仍代表 HEAD，但不能从 HEAD 直接重放（需真实 LLM 凭证）。
- 工单 04 接口：gate 同时报告 mean-of-N 与 worst-of-N；**worst-of-N 超过
  −0.5pt 时不得翻默认开关**（需 owner 单独批准）。
- 完整文字在 `docs/evidence/03-r4-fix-evidence.md` 的「r7 F2 口径裁定」。

### F3（nit）空 `matched_literals` 退回计数口径

- 改动：`specialists.py:192-217`。命中判定改为
  `keyed = matched is not None and (bool(matched) or total == 0)`；
  空字典/缺失键不再被当成「全部命中」，走
  `grounded + ambiguous_only < total` 的旧计数兜底。
- 证据：`test_empty_matched_literals_falls_back_to_count_based_undercoverage`
  先红（当前实现返回 `pass []`）→ 修复后 `NEEDS_FIX` 且报
  「回填命中率 1/2…」。r5/r6 的跨块歧义-only 用例仍全绿（见回归）。

### F4（nit）删除生产不可达的 slim ③ 重复分支，钉住真实合并语义

- 选择评审给的**第一种**修法：瘦身链路的重复输出在
  `backfill_provenance._coalesce_entities` 已按 `(type,name)` 合并，
  `rule_review` 不再维护一条只在手写结果上可达的「重复拦截」分支；
  关闭态 ③ 保持 262319b 纯 `name` 比较。
- 证据：
  - 删除 r6 的 `test_true_duplicate_same_type_and_name_still_blocked_by_rule_review`
    （它点亮的正是不可达 slim 分支）。
  - 新增端到端 `test_duplicate_same_type_and_name_is_coalesced_end_to_end`：
    `run_extraction_team` 真实入口下，两次输出 `腾讯/ORG`（别名 `鹅厂`/
    `企鹅`）→ 合并为 1 个实体、别名取并集 `["鹅厂","企鹅"]`、
    `entities_total=2`、**1 次调用 / accepted=True**。
- 文档：`ARCHITECTURE.md:79-83` 明确「真正同型同名重复会被静默合并
  （别名/属性取并集），不返工」；同名异型仍走 mention 歧义信号。

## 二、十条验收逐条（结论 + 口径 + 证据路径）

| # | 结论 | 口径 | 证据路径 |
|---|---|---|---|
| 1 | 部分达成（历史不可追） | 首版实现与测试同提交是历史事实；r7 先红可独立复现 | `docs/evidence/03-r7-red-tests.txt`（3 failed / 1 passed，`00f169b`） |
| 2 | 达成 | N 次出现 → N mention，span 对原文切片 | `tests/test_local_backfill.py` 相关用例；r7 全量 339 passed |
| 3 | 达成 | 最长匹配优先，前缀/子串不误覆盖 | `tests/test_local_backfill.py` / `test_r3_review_fixes.py`；r7 全量 |
| 4 | 达成 | 别名/标准名同实体；正文不存在的别名进 `unmatched_aliases` | `tests/test_r3_review_fixes.py` / `test_r4_review_fixes.py`；r7 全量 |
| 5 | 达成 | 同句整句证据；跨句/跨块 `evidence_span=None` 不臆造 | `tests/test_r3_review_fixes.py` / `test_local_backfill.py`；r7 全量 |
| 6 | 达成 | 同名歧义 → 0 归属 + `ambiguous_mentions`（候选带 type） | `tests/test_local_backfill.py` / `test_r6_review_fixes.py`；r7 红/绿 |
| 7 | 达成 | 开关默认关；关闭态抽取 golden + `rule_review` 差分均与 262319b 一致 | `tests/test_backfill_extractor.py`；`03-r7-offmode-diff.txt`；`03-r7-diff-probe-output.txt` |
| 8 | 达成（mean-of-3） | 3 轮均值 +0.0733pt ≤ 0.5pt；worst-of-N 未达标并列披露 | `docs/evidence/03-r3-stress-ab.json`；`03-r4-fix-evidence.md`「r7 F2 口径裁定」 |
| 9 | 达成（证据早于 r5/r6/r7） | 单块 output token 2365.98→582.13（−75.32%），截断块 6→0 | `docs/evidence/03-r3-stress-ab.json`；差异面见 F2 |
| 10 | 达成 | 纯本地确定性；每块正文只扫一次（模式数 2/50/200 迭代恒 1 次） | `tests/test_local_backfill.py::test_scan_is_single_linear_pass_independent_of_pattern_count`；r7 全量 |

## 三、回归与未动范围

- 定向回归（r3–r7 + orchestration + confidence）：
  **64 passed**（`docs/evidence/03-r7-green-targeted.txt`）。
- 全量离线：**339 passed, 65 skipped**（`docs/evidence/03-r7-full-suite.txt`）。
- 未跑 `RUN_DB_TESTS=1`（无 pgvector 实例）；未复跑真实 LLM A/B（不消耗凭证）。
- 未改契约面（本工单不新增 HTTP 端点；`ExtractResponse.backfill` 仍是可选字段）。
- 未授权自审：本轮只做实现与本地证据，复审由协调者另派全新 reviewer。

## 四、复现命令

```sh
cd /home/dell/orca/workspaces/minirag/tmp-yzd-feat-relation-202610-03-mention-span
ARK_API_KEY=dummy PYTHONPATH=src /tmp/opencode/review-venv/bin/python \
  -m pytest -q -p no:cacheprovider                       # 339 passed, 65 skipped
PYTHONPATH=src /tmp/opencode/review-venv/bin/python \
  -m pytest -q -p no:cacheprovider tests/test_r7_review_fixes.py
# 关闭态差分（262319b 归档已在 /tmp/opencode/r7-base-262319b）
PYTHONPATH=src /tmp/opencode/review-venv/bin/python /tmp/opencode/03-r7-diff-probe.py
PYTHONPATH=/tmp/opencode/r7-base-262319b/src /tmp/opencode/review-venv/bin/python \
  /tmp/opencode/03-r7-diff-probe.py
```
