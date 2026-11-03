# 工单 03 第 5 轮评审（r5）修复证据

- 基线：`928daa8`（r4 修复后的 HEAD）。
- 先红提交：`8419748`（只加/改回归用例，10 failed / 22 passed）。
- 修复提交：`d7dad32`（F1-F9 主体）+ `2205848`（歧义-only 输入加固）。
- 证据提交：`c61e018` + `2205848`。
- 全量离线：`ARK_API_KEY=dummy PYTHONPATH=src pytest -q -p no:cacheprovider`
  → **329 passed, 65 skipped**（r5 前 319 passed / 65 skipped，新增 10 条用例）。
- 只改本分支，未动 `dev/yzd-feat-relation-202610` / `main`。

## 逐条处理

### F1（should）`rule_review` 接受唯一别名端点只在瘦身模式

- 改动：`src/moray/orchestration/specialists.py:94-121`。标准名永远合法；
  「唯一归属」别名只有 `backfill is not None`（瘦身模式）才放宽；关闭态
  保持改造前严格口径，仍报 ②。
- 证据：`tests/test_r5_review_fixes.py::test_rule_review_rejects_unique_alias_endpoint_when_backfill_disabled`
  （先红失败 → 修复后过）；探针：
  ```
  [F1] full/off  : needs_fix ['关系头实体「鹅厂」不在实体列表中（疑似臆测或漏抽实体）']
  [F1] slim/on   : pass []
  ```
  对应评审实测的 `3652eab` 行为（关闭态 needs_fix）已恢复。

### F2（should）歧义未归属只做质量信号，不进返工 feedback

- 改动：`src/moray/orchestration/specialists.py:157-180`。⑤ 不再把
  `ambiguous_mentions` 写成 issue；歧义继续保留在
  `BackfillReport.ambiguous_mentions` 并随 `ReviewedExtraction.backfill` /
  `backfill_sink` 上行。
- 额外收口：歧义候选实体天然 `entities_grounded=0`，若直接拿
  `block_confidence<1` 报「未找到出处」，歧义-only 输入仍会触发返工。
  现在用 `matched_literals` + `ambiguous_mentions.candidates` 计算
  「已归属 + 仅歧义命中」的覆盖数，只有真正未出现的实体才报返工；
  同时保留反向用例，避免一刀切放过真漏抽。
- 证据：
  - `tests/test_r5_review_fixes.py::test_ambiguous_mention_does_not_trigger_model_rework`
    （歧义-only 输入；在 `d7dad32` 上先红 1 failed，见
    `03-r5-red2-ambiguous-only.txt`）；
  - `test_true_missing_entity_still_triggers_rework_alongside_ambiguity`
    （真漏抽仍拦）；
  - 探针（正文只有歧义字面、`block_confidence=0.0`）：
    ```
    [F2] 模型调用次数: 1
    [F2] accepted     : True
    [F2] review issues: []
    [F2] 歧义信号条数 : 1
    [F2] block_confidence: 0.0
    ```
    修复前同输入为 3 次调用 / `accepted=False`。

### F3（nit）整篇别名补扫用 `source_text`

- 改动：`src/moray/extraction/pipeline.py:436-442`：
  `document_text = document.source_text or document.text`。
- 证据：`tests/test_r5_review_fixes.py::test_whole_document_alias_scan_uses_source_text_not_feedback`；
  探针（别名只出现在反馈里）：
  ```
  [F3] unmatched_aliases: [{'entity': '腾讯', 'literal': '鹅厂'}]
  [F3] seen_literals    : ['腾讯']
  ```
  修复前反馈里的「鹅厂」会把幻觉信号掩盖掉（`unmatched_aliases=[]`）。

### F4（nit）② 的 issue 文案区分「归属不唯一」与「不在实体列表」

- 改动：`src/moray/orchestration/specialists.py:110-121`。字面在实体列表里
  但归属多个实体时输出「关系X实体「Y」字面归属不唯一或未知（疑似臆测）」；
  真正不存在的字面仍输出「不在实体列表中」。
- 证据：`tests/test_r4_review_fixes.py::test_rule_review_still_rejects_ambiguous_alias_relation_endpoint`
  已改为同时断言「归属不唯一」出现、「不在实体列表」不出现（先红失败）。

### F5（nit）`aggregate.delta` 的 `*_min_*` 取最坏值

- 改动：`tools/backfill_stress_ab.py:311-333`：`truncat*` 取 max，
  `*_min_*` 取 min，其余取均值。
- 证据：`tests/test_r5_review_fixes.py::test_aggregate_delta_min_fields_take_worst_value`；
  并只用 `repeats[*].delta` 重算了 `docs/evidence/03-r3-stress-ab.json` 的
  `aggregate.delta` / 顶层 `delta`（不含任何模型调用）：
  `span_ratio_min_before` 0.0116 → **0.0099**，与 `03-r4-fix-evidence.md`
  的 min 口径一致；`truncated_chunks_before` 仍为 6。

### F6（nit）`LLM_TEMPERATURE` 可发现 + describe 可见

- 改动：`.env.example:36` 增加 `LLM_TEMPERATURE` 注释行；
  `src/moray/config.py:180` 的 `describe()` 增加 `temperature`。
- 证据：`tests/test_r5_review_fixes.py::test_env_example_documents_temperature`、
  `test_describe_includes_temperature`，以及更新后的
  `tests/test_llm_providers.py::test_describe_never_leaks_the_key`（先红均失败）；
  探针：`temperature unset → None`、`temperature=0.0 → 0.0`。

### F7（nit）采样参数进 05 内容缓存键

- 改动：`cache.py` 的 `build_cache_key` / `get_or_extract` / `has_cached`
  增加 `sampling_fingerprint`；`Extractor` 由 `get_settings()` 生成
  `temperature|reasoning_effort|thinking` 摘要；`pipeline` 查询/预热探测都带上。
- 证据：`tests/test_r5_review_fixes.py::test_cache_key_changes_with_sampling_fingerprint`、
  `test_shared_cache_does_not_reuse_result_across_sampling_params`；探针：
  ```
  [F7] key(temp=-) : chunk:d28e2ce6db1a2b4012
  [F7] key(temp=0.0): chunk:283fb8f8f4001ad943
  [F7] 两次真实调用次数: 1 1
  ```
  修复前第二次会命中第一次的键（`0 1`）。

### F8（nit）三处重复实现收敛

- `pipeline.py`：新增 `slim` property，`_domain_fingerprint()` 与 `run()`
  共用，不再各算一次 `getattr(extractor, "local_backfill", False)`。
- `specialists.py`：改为复用 `local_backfill.literal_index()`（原
  `_literal_index` 转公开），② 与 `_resolve`/④ 统一 `strip` 口径。
- `config.py`：新增 `_optional_float_env(name, minimum, maximum)`，
  `_positive_float_env` 与 `_optional_temperature_env` 共用解析/`isfinite`/报错。
- 证据：全量回归 329 passed（既有端点/回填/配置用例未改语义）；代码见
  本轮 diff。

### F9（nit）指纹用例钉死期望值

- 改动：`tests/test_r4_review_fixes.py::test_domain_fingerprint_changes_with_slim_mode`
  由「不相等」改为精确断言
  `e084408b3bbb25dd` / `8b9e2ebfd4afb900`，并注释「有意改领域定义才更新」。
- 证据：探针 `[F9]` 两行与断言一致；用例通过。

### F10（nit，口径/产品决策）验收 8 单轮越界如实标注

- 未改验收门槛（需 owner 决策）。`docs/evidence/03-r4-fix-evidence.md`
  增加「r5 F10 口径说明」：原文「达成」只指 3 轮均值；若按 worst-of-N，
  单轮 −1.30pt、seed20261104 −2.88pt **未达标**，是否改门槛/扩语料待决策。
- 证据：文档段落 + `docs/evidence/03-r3-stress-ab.json` 的逐轮 `delta`
  （单轮 −1.30 / +1.00 / +0.52pt）。

## 复现命令

```sh
ARK_API_KEY=dummy PYTHONPATH=src python -m pytest -q -p no:cacheprovider
ARK_API_KEY=dummy PYTHONPATH=src python /tmp/opencode/03-r5-probe.py
```

先红：在 `8419748` 的独立 worktree 上跑
`tests/test_r5_review_fixes.py tests/test_r4_review_fixes.py tests/test_llm_providers.py`
→ 10 failed / 22 passed（完整输出见 `03-r5-red-tests.txt`）。
歧义-only 加固的先红：在 `d7dad32` 上覆盖最新测试文件后跑
`tests/test_r5_review_fixes.py` → 1 failed / 9 passed
（见 `03-r5-red2-ambiguous-only.txt`）。
