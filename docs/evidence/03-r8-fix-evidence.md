# 工单 03 第 8 轮评审（r8）修复证据

- 被评审基线：`017afa7`（r7 证据提交后的 HEAD）。
- 先红提交：`059768e`（只加/改用例，未改实现）。
- 修复提交：`01e2316`（`specialists.py` ②④、`local_backfill.py` 合并身份键与
  关系证据配对、`ARCHITECTURE.md` 审校/复杂度口径）。
- 证据首版提交：`e2f4c3a`（本文件与 `03-r8-*.txt`；随后一个提交只补本行哈希）。
- 本轮修复面：r8 的 1 个 should（F1）+ 5 个 nit（F2~F6）。
- 只改本分支，未动 `dev/yzd-feat-relation-202610` / `main`。
- 先红：`docs/evidence/03-r8-red-tests.txt`（**7 failed / 17 passed**）。
- 修复后全量离线：`docs/evidence/03-r8-full-suite.txt`（**343 passed / 65 skipped**）。
- 定向回归：`docs/evidence/03-r8-green-targeted.txt`（**95 passed**）。
- 逐条探针：`docs/evidence/03-r8-probe-output.txt`。

## 一、逐条处理

### F1（should）别名冲突的关系端点降为质量信号，不再返工

- 位置：`specialists.py` 的 `_endpoint_issue` 瘦身分支。
- 改动：`len(owners) > 1` 时直接 `return None`。歧义只保留在
  `BackfillReport.ambiguous_mentions`，不写进 `issues/feedback`；标准名豁免、
  唯一归属别名放宽、真未知端点（`owners` 为空）仍按原逻辑拦。
- 依据：工单正文「同名不同类型，或别名归属冲突……本地回填不猜，把该 mention
  标记为『歧义未归属』并计入质量信号；**这版不做模型补问**」；
  `ARCHITECTURE.md` 同步改成「不报 issue、不触发返工」。
- 先红用例：
  - `test_ambiguous_alias_relation_endpoint_is_signal_not_rework_issue`
  - `test_ambiguous_alias_relation_endpoint_passes_in_one_model_call`
  - 旧期望被校正：`test_r6_..._does_not_emit_false_not_grounded_issue`、
    `test_r4_..._treats_ambiguous_alias_relation_endpoint_as_signal`。
- 探针（`text="同名发布了X公司。"`，别名 `同名` 归属腾讯/网易）：

  ```
  修复前：calls=3 accepted=False
          issues=['关系头实体「同名」字面归属不唯一或未知（疑似臆测）']
  修复后：calls=1 accepted=True issues=[]
          ambiguous_mentions=1（信号仍在）
  ```

- 影响面：短文档默认路径不再稳定多花 2 次模型调用；分块单轮审校也不会因为
  合法歧义把整篇判 `accepted=False`。

### F2（nit）关闭态 ④ 恢复 262319b 原文措辞，差分覆盖 ④

- 位置：`specialists.py` ④ 的关闭态分支。
- 改动：`backfill is None` 且 `confidence is not None` 时恢复
  `"关系 … 置信度高却无原文证据"`；只有 `confidence is None`（未知）保留
  `"无原文证据（置信度未知）"`。verdict 与入库数据不变。
- 测试：`test_r7_review_fixes.py` 的差分 fixture 新增
  `high_conf_no_evidence`（`evidence_span=None, confidence=0.9`），
  `test_off_mode_rule_review_matches_262319b_on_all_fixtures` 现在逐字节比较
  ①②③④。
- 探针：`matches_262319b=True`，issues 恰为
  `['关系 腾讯-[发布]->混元 置信度高却无原文证据']`。

### F3（nit）验收 8 的 owner 裁定补齐元数据与证据快照版本

- 位置：`docs/evidence/03-r4-fix-evidence.md`「r7 F2 口径裁定」、
  `docs/evidence/03-r7-fix-evidence.md` 验收表。
- 补齐：决策人 **owner**、时间 **2026-11-03**、裁定内容 **mean-of-3 判达标**、
  唯一数据快照 **`03-r3-stress-ab.json` 的 `git_sha=9f95ed4`**。
- 明确披露：r8 后的 HEAD **没有**用真实 LLM 重放验收 8/9（本机无凭证），
  所以记录的是「在 9f95ed4 快照上按 mean-of-3 达成」，不是「HEAD 已复测」；
  worst-of-N（单轮 −1.30pt、seed20261104 逐篇均值 −2.88pt）未达标，
  进工单 04 gate，超过 −0.5pt 不得翻默认开关。
- 验收表第 8 条已改为「达成（mean-of-3；证据快照 9f95ed4，HEAD 未重放）」，
  不再用一句无版本的口径宣称达成。

### F4（nit）关系证据配对从 O(H·T·S) 降到按句分桶

- 位置：`local_backfill._backfill_relations`。
- 改动：每个 mention 的句下标只算一次（句表有序，二分定位）；尾 mention 按
  句分桶，头 mention 只与本句内的尾配对。候选仍然「全部保留 + 最近一对
  selected」；同距时的稳定排序与旧实现一致（`relations_with_evidence`
  用例全绿）。
- 新增用例：`test_relation_evidence_pairing_is_linearish`（5600 字 / 800 句 /
  1600 mention，上界 1s）。
- 探针实测（同一台机、同一解释器）：

  | chars | mentions | sents | 修复前 | 修复后 |
  |---|---|---|---|---|
  | 5600 | 1600 | 800 | 5.202s | **0.0075s** |
  | 17500 | 5000 | 2500 | 未跑（旧实现超线性） | **0.0147s** |

- 验收 10 的字面（AC 单趟扫描）不受影响；本条只收紧关系配对预算。

### F5（nit）只有空白差异的重复标准名合并成同一实体

- 位置：`local_backfill._coalesce_entities` 身份键。
- 改动：`(type, name)` → `(type, name.strip())`，与 `literal_index` 的去空白
  口径一致；关闭态不走该函数，262319b 行为不变。
- 新增用例：`test_whitespace_variant_duplicate_is_coalesced_not_ambiguous`。
- 探针（实体 `腾讯` + ` 腾讯` + `混元`，正文「腾讯发布了混元。」）：

  ```
  修复前：entities_total=3 ambiguous=1 两个腾讯都 0 mention
  修复后：entities_total=2 ambiguous=0 腾讯 1 mention、混元 1 mention，review=pass
  ```

- 数据归属未被写错，本轮只消除「纯空白重复被误报成歧义」的噪音。

### F6（nit）验收 1 如实记为「部分达成（历史不可追）」

- 历史事实：`185fb41` 实现与测试同提交，没有独立先红提交；无法补做。
- 处理：`03-r4-fix-evidence.md` 验收表第 1 条改为「部分达成（首版历史不可
  追）」，并记录 r4/r7/r8 的先红可核；`03-r7-fix-evidence.md` 保持同样口径。
- 后续门禁建议：新工单以「测试提交先于实现提交」作为硬门禁，避免每轮
  review 重复同一句免责声明。

## 二、十条验收复核（r8 修复后）

| # | 结论 | 依据 |
|---|---|---|
| 1 | **部分达成（首版历史不可追）** | r4/r7/r8 先红可核；`185fb41` 同提交是历史事实（F6） |
| 2 | 达成 | N 次出现 → N mention、span 对原文切片；全量回归 |
| 3 | 达成 | 最长匹配/前缀子串/部分重叠用例全绿 |
| 4 | 达成 | 别名/标准名同实体；幻觉别名进 `unmatched_aliases` |
| 5 | 达成 | 同句整句证据；跨句/跨块留空；F4 改动后候选与 selected 不变 |
| 6 | 达成 | 同名歧义 0 归属 + `ambiguous_mentions`；关系端点**只作信号**（F1） |
| 7 | 达成 | 开关默认关；关闭态抽取 golden + `rule_review` ①②③④ 差分（F2） |
| 8 | **达成（mean-of-3；快照 9f95ed4，HEAD 未重放）** | owner 2026-11-03 裁定；worst-of-N 进工单 04 gate（F3） |
| 9 | 达成（证据早于 r5~r8） | 单块 token 2365.98→582.13（−75.32%），截断 6→0；差异面不含 prompt/schema |
| 10 | 达成 | AC 每块单趟；关系证据配对已按句分桶（F4） |

## 三、回归与未动范围

- 全量离线：**343 passed, 65 skipped**（`03-r8-full-suite.txt`）。
- 定向回归：**95 passed**（`03-r8-green-targeted.txt`）。
- 未跑 `RUN_DB_TESTS=1`（本机无 pgvector 实例）；未复跑真实 LLM A/B
  （无凭证，不消耗费用）。
- 未新增/修改 HTTP 端点；契约面（`.devflow/contract.md`）不涉及本工单。
- 未自审：本轮只做实现与本地证据，复审由协调者另派全新 reviewer。

## 四、复现命令

```sh
cd /home/dell/orca/workspaces/minirag/tmp-yzd-feat-relation-202610-03-mention-span

# 先红（测试提交 059768e、实现未改时）
ARK_API_KEY=dummy PYTHONPATH=src /tmp/opencode/review-venv/bin/python \
  -m pytest -q -p no:cacheprovider tests/test_r8_review_fixes.py \
  tests/test_r7_review_fixes.py tests/test_r6_review_fixes.py \
  tests/test_r4_review_fixes.py          # 7 failed, 17 passed

# 修复后全量
ARK_API_KEY=dummy PYTHONPATH=src /tmp/opencode/review-venv/bin/python \
  -m pytest -q -p no:cacheprovider          # 343 passed, 65 skipped

# 逐条探针
PYTHONPATH=src /tmp/opencode/review-venv/bin/python /tmp/opencode/r8-probe.py
```
