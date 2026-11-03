# 工单 03 第 13 轮评审修复证据（2026-11-03）

- 分支：`yzc/tmp-yzd-feat-relation-202610-03-mention-span`
- 起点：`0065885`（r12 修复后的定点）；本轮只处理 `review-r13-03.md` 的
  2 should + 6 nit，不做顺手重构。
- 开关：`MORAY_LOCAL_BACKFILL` 仍**默认关闭**；关闭态 golden 未改。
- 先红后绿：`tests/test_r13_review_fixes.py` 9 failed / 1 passed（N5 是
  既有行为的特征化测试）→ 实现后 10 passed。
  - 红：`03-r13-red-tests.txt`
  - 绿（本轮相关用例 + r10/r11/r12/local_backfill 回归）：
    `03-r13-green-targeted.txt`（60 passed）
  - 全量：`03-r13-full-suite.txt`（401 passed / 65 skipped）
  - 确定性探针：`03-r13-probe-output.txt`（`tools/backfill_r13_probe.py`）

## 逐条处理

| # | 评审发现 | 处理 | 证据 |
|---|---|---|---|
| S1 | `cached_backfill_provenance` 缓存键漏 attributes/description/confidence，重试只改属性会回灌旧值（绕过领域校验） | 缓存键改为 `(原文, 实体(type,name,aliases,attributes 排序 JSON,description,confidence), 关系(head,predicate,tail))`。缓存值仍是整份 filled 结果，但只有内容完全一致才命中 | `test_backfill_cache_key_includes_revised_fields[attributes/description/confidence]`（3 条参数化，修前全红）；探针 S1 段 |
| S2 | 验收 8 未在 HEAD 复验；`03-baseline-ab.txt` 仍写「已闭环」且数字与 JSON 不一致 | ① 删除 `03-baseline-ab.txt` 的旧 r3「已闭环」结论：历史快照 9f95ed4 当时其实未达成（worst −1.30pt），统一指到 `03-r3-stress-ab.json` 的 delta；② 用 HEAD + 本轮修复重放 02 压力语料 full/slim（3 轮），同时报 mean-of-N 与 worst-of-N，结果达成后同步更新 summary 验收表 | `03-baseline-ab.txt`、`03-r13-stress-ab.json`、下节「验收 8 HEAD 重放」 |
| N1 | 序列化深拷贝漏两层嵌套的 `chunk_ids`，测试名与断言不符 | 新增 `_copy_backfill_entry()`：`relations_without_evidence` 与 `relation_evidence_candidates` 两处都连 `chunk_ids` 复制；`to_dict`/`serialize_backfill_for_output` 共用 | `test_to_dict_deep_copies_nested_chunk_ids`、`test_serialize_backfill_deep_copies_nested_chunk_ids`（修前红）；探针 N1 段 |
| N2 | 进程内 2000 条候选上限与验收 5「保留全部候选」冲突，summary 仍写达成 | 选择评审给的口径 (a)：明确承认块间 ≤2000/关系、出网 ≤20，是对验收 5 的收窄；同步改 summary 验收表与 `ARCHITECTURE.md`，不再声称无条件保留全部候选 | `03-local-backfill-summary.md` 验收 5 行、`ARCHITECTURE.md` 关系证据候选段 |
| N3 | 短文档/直连 Extractor 路径没有存值规范化，实体名/关系端点带空白落库会建 MISC 占位 | `_coalesce_entities` 输出 `name.strip()`；`_backfill_relations` 对 head/tail 去空白后再解析/定位/写结果；短文档路径（`Extractor.extract`）因此与 slim 归并同口径 | `test_backfill_normalizes_entity_names_and_relation_endpoints`（修前红）；探针 N3 段 |
| N4 | `entity_signal_key` 用未去空白 name，跨块拆信号键 | `entity_signal_key` 内部 `name.strip()`，与 `entity_identity_key` 共用口径 | `test_entity_signal_key_strips_name`、`test_merged_matched_literals_use_stripped_signal_key`（修前红）；探针 N4 段 |
| N5 | `MergeMode` 与四个关键字 shim 两套真相 | shim 在 docstring 中标注 **deprecated**，写明「显式传值即覆盖 mode」「不要与 mode 混用」；补一条四个 shim 全部覆盖 SLIM 的特征化测试 | `test_all_four_shims_override_slim_mode`；`pipeline.naive_merge` docstring |
| N6 | 负数候选上限改抛异常，坏配置会把整次响应打成 500 | 序列化边界对负数 **clamp 到默认 20 + WARNING**（`None=全量 / 0=空 / 正数=前 N` 的其余语义不变）；配置层应在启动时另行校验 | `test_negative_candidate_limit_clamps_to_default_and_warns`（修前红）；探针 N6 段 |

## 验收 8 HEAD 重放

命令（密钥只从本地 env 读取，不落仓；跑前先提交，确保证据对应干净 HEAD）：

```bash
PYTHONPATH=src python tools/backfill_stress_ab.py \
  --gold data/gold/stress --env-file ../moray/.env \
  --max-units 1500 --overlap-units 200 --concurrency 6 --repeat 3 \
  --json-out docs/evidence/03-r13-stress-ab.json
```

结果文件 `03-r13-stress-ab.json`：`git_sha=0df406c`、`git_dirty=false`、
`model=deepseek-flash`、`temperature=0`、3 篇 stress seeds（79,362 字）。

| 轮 | 实体 macro relaxed Δ | 关系 F1 | 单块 output token | 截断块 |
|---|---|---|---|---|
| 1 | +2.04pt | 0.3366→0.5906 | 2925.37→656.18 | 4→0 |
| 2 | **−0.28pt（worst）** | 0.4717→0.7861 | 3060.97→549.55 | 3→0 |
| 3 | +1.34pt | 0.4681→0.6579 | 2364.33→624.86 | 6→0 |
| 聚合 | 0.8947→0.9050，**mean +1.0333pt / worst −0.28pt** | 0.4255→0.6782 | 2783.56→610.20（−77.73%） | 6→0 |

- 门槛核对：worst-of-N −0.28pt 在 ≤0.5pt 内；关系 F1 不下降；span 一致率
  min/mean 0.0087/0.0148→1.0/1.0。**验收 8/9 在 HEAD 达成。**
- 与历史快照的关系：`9f95ed4` 的 worst −1.30pt 是当时未达成的真实记录；
  本轮没有改历史 JSON，只新增 HEAD 重放并把旧「已闭环」文字改写成
  「历史/当前」两段，避免两套数字同时冒充结论。
- 开关仍默认关闭（`MORAY_LOCAL_BACKFILL` 未设置时关闭），翻默认不在本轮范围。


## 验收条目未回退确认

本轮只改 r13 的 8 条，未触及验收 1/2/3/4/6/7/9/10 的实现语义；全量
`03-r13-full-suite.txt` 401 passed / 65 skipped 覆盖了这些条目的既有用例。

- 验收 2/3/4/6：多模式回填、最长匹配、别名/幻觉、歧义未归属用例全绿；
  N3/N4 只统一存值与信号键口径，不改变归属判定。
- 验收 5：证据定位用例全绿；「保留全部候选」按 N2 明确收窄为块间 ≤2000/
  关系、出网 ≤20，summary 与 ARCHITECTURE 已同步。
- 验收 7：`MORAY_LOCAL_BACKFILL` 仍默认关闭；关闭态 schema/prompt/归并
  golden 用例全绿。
- 验收 8：HEAD `0df406c` 的 3 轮重放 mean +1.0333pt / worst −0.28pt，
  关系 F1 不降、span 1.0，达成（见上节）。
- 验收 9：本轮未改 schema/prompt；token/截断数字沿用同一 HEAD 重放结果。
- 验收 10：单趟线性用例（含 20k/16k 守护）全绿；S1 只让缓存更精确，
  N1/N3/N4/N6 都是 O(1)/O(n) 的确定性操作，未引入逐名字全文扫描。

