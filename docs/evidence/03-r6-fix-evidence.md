# 工单 03 第 6 轮评审（r6）修复证据

- 基线：`4d6f0a7`（r5 修复后的 HEAD）。
- 先红提交：`f5a5c4c`（只加 `tests/test_r6_review_fixes.py`）。
- 修复提交：`306393f`（`specialists.py` + `ARCHITECTURE.md`）。
- 本轮修复面：`src/moray/orchestration/specialists.py`（`rule_review` 的 ②③④⑤）
  + `docs/ARCHITECTURE.md` 审校口径说明 + 新增 `tests/test_r6_review_fixes.py`。
- 先红：只加用例、未改实现时，`tests/test_r6_review_fixes.py` **5 failed / 2 passed**
  （`docs/evidence/03-r6-red-tests.txt`）。
- 修复后全量离线：
  `ARK_API_KEY=dummy PYTHONPATH=src pytest -q -p no:cacheprovider`
  → **336 passed, 65 skipped**（r5 329 + 本轮 7 条用例），
  见 `docs/evidence/03-r6-full-suite.txt`。
- 只改本分支，未动 `dev/yzd-feat-relation-202610` / `main`。

## 逐条处理

### S1（should）「同名不同类型」不再被 ③ 当成「未去重」

- 改动：`specialists.py:135-145`。瘦身模式（`backfill is not None`）下 ③ 的
  身份键改为 `(type, name.strip())`；只有真正同型同名重复才报「未去重」。
  关闭态继续用纯 `name`（配合 N2 的 `names` 不 strip）。
- 证据：
  - 端到端 `test_same_name_different_type_is_ambiguity_not_duplicate_rework`
    （正文「苹果发布了新手机，苹果很好吃。」，实体 `苹果/ORG + 苹果/MISC`）：
    先红时 3 次模型调用 / `accepted=False`，修复后
    **1 次调用 / `accepted=True`**，且回填仍保留 2 条 `ambiguous_mentions`。
  - `test_true_duplicate_same_type_and_name_still_blocked_by_rule_review`：
    直接构造同 `(type,name)` 重复，③ 仍拦（端到端链路上回填会先 coalesce）。
  - 探针 `03-r6-probe-output.txt`：
    ```
    mentions: [[], []]
    amb: [('苹果', 0), ('苹果', 9)] grounded: 0 / 2
    review: pass []
    model calls: 1 accepted: True
    ```

### S2（should）跨块歧义-only 不再因 chunk-weighted 计数误报

- 改动：`specialists.py:187-218`。⑤ 改用文档级实体键集合：
  `matched_literals` 中 `n==0` 的键，减去 `ambiguous_only`（歧义候选键 -
  已命中键），得到真正未出处的实体；不再拿 `grounded/total` 的块加权相加
  与文档级歧义集合比较。旧调用方手写的、没有 `matched_literals` 的精简
  backfill 字典保留原计数兜底（兼容既有用例）。
- 证据：
  - `test_two_chunk_ambiguous_only_passes_without_rework`：走
    `run_chunked_team`（`provider` + 2 块，每块 腾讯/网易 声明同一别名「同名」、
    混元正常命中）。先红 `needs_fix ['回填命中率 2/6…']`，修复后
    **`accepted=True` 且无「回填命中率」issue**。
  - `test_two_chunk_true_missing_entity_still_needs_fix`：每块追加
    正文不存在的「幽灵公司」→ 仍 `accepted=False` 且报回填命中率。
  - 探针 `03-r6-probe-output.txt`（两块，`entities_total=6`，
    `grounded=2`，2 条歧义）：修复前 `needs_fix`，修复后 `pass`。

### N1（nit）歧义端点不再重复报「端点在正文中未命中」

- 改动：`specialists.py:159-166` 新增 `_endpoint_grounded_or_ambiguous`：
  端点字面在 `literal_owners` 中归属 `>1` 时视为「已解释」（不写 mention 是
  既定策略），④ 跳过它；另一端真未知仍会被拦，② 的准确文案
  「字面归属不唯一或未知」保留。
- 证据：`test_ambiguous_endpoint_does_not_emit_false_not_grounded_issue`
  （正文「同名发布了X公司。」）。修复前 issues 同时含
  `关系头实体「同名」字面归属不唯一或未知…` 与
  `关系 同名-[发布]->X公司 无原文证据（端点在正文中未命中）`；
  修复后只剩前者。

### N2（nit）关闭态的审校口径受开关控制

- 改动：`specialists.py:97-122`
  - 关闭态 `names = {e.name}`（不 strip），③ 恢复纯 `name` 比较；
  - ② 关闭态只认「精确标准名 + 归属唯一」，别名不放宽（保持 r5 F1），
    同名异型标准名（`owners>1`）仍报 ②；
  - 瘦身模式继续 `strip` + 标准名豁免 + 唯一别名放宽。
- 证据：
  - `test_off_mode_does_not_strip_names_for_duplicate_check`：
    实体 `腾讯` 与 ` 腾讯`（仅空白差）→ 关闭态 `PASS`（修复前误报未去重）。
  - `test_off_mode_same_name_different_type_endpoint_not_exempted`：
    `腾讯/ORG + 腾讯/MISC` 的标准名端点 → 关闭态 `NEEDS_FIX` 且报
    「关系头实体「腾讯」…」；同一结果在瘦身模式 `PASS`（歧义不返工）。

### N3（nit）验收 8 worst-of-N 未达标：口径已由 owner 裁定（r7 F2）

- 本轮不修改实现或证据数字，也不改变验收门槛。`docs/evidence/03-r4-fix-evidence.md`
  已并列写明 3 轮均值 +0.07pt 与 worst-of-N（单轮 −1.30pt、
  seed20261104 逐篇 −2.88pt）。
- **r7 F2 裁定**：owner 于 2026-11-03 选定 **mean-of-3** 口径（3 轮均值
  +0.0733pt ≤ 0.5pt 门槛 → 达成），但要求同一份证据并列披露 worst-of-N，
  并给工单 04 留 gate 接口（worst-of-N 超过 −0.5pt 不得翻默认开关）。
  完整口径、复算方式与 `git_sha=9f95ed4` 差异面见
  `docs/evidence/03-r4-fix-evidence.md` 的「r7 F2 口径裁定」与
  `docs/evidence/03-r7-fix-evidence.md`。

## 回归与未动范围

- 全量：**336 passed, 65 skipped**（含 r5/r4 全部既有回归）。
- 未跑 `RUN_DB_TESTS=1`（无 pgvector 实例，沿用仓内门控）。
- 未复跑真实 LLM A/B（不消耗凭证）；N3 的绝对数字仍引用既有 JSON 证据。
- 未授权自审：本轮只做实现与本地证据，复审由协调者另派全新 reviewer。
