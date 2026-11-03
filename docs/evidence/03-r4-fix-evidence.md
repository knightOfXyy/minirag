# 工单 03 第 4 轮评审修复证据（2026-11-03）

- 分支：`yzc/tmp-yzd-feat-relation-202610-03-mention-span`
- 被评审基线：`3652eab`（r3 修复末）
- 本轮先红提交：`de82da5`（只加 `tests/test_r4_review_fixes.py`，8 failed / 2 passed）
- 实现提交：`2c91a24`（B1/S1/N1/N4/N5/N6/N7 + 压测脚本多轮采样）；`9f95ed4`
  只改压测证据的「脏工作树」判定（忽略未跟踪 IDE 目录）；证据与测试清理
  在后续提交（HEAD），代码面不变
- 全量离线回归：`ARK_API_KEY=dummy PYTHONPATH=src python -m pytest -q` →
  **319 passed, 65 skipped**（原始输出 `03-r4-pytest-output.txt`）
- r4 回归：修复前 **8 failed / 2 passed**，修复后 **10 passed**
- 只读复现评审 B1：`domain_fingerprint(general)` = `e084408b3bbb25dd`，
  `slim=True` = `8b9e2ebfd4afb900`，键不再相同。
- B1/S1/N1 的最小复现原始输出：`03-r4-repro.txt`。

## 一、逐条处理（10/10）

| # | 级别 | 发现 | 处理 | 证据 |
|---|---|---|---|---|
| B1 | blocker | 05 内容缓存键不含 `local_backfill`，slim/full 互相投毒 | `domain_fingerprint(domain, *, slim=False)` 把 slim 的 prompt+schema 一起 hash；`pipeline._domain_fingerprint` 传当前回填模式，非 DomainSpec 回退串也带 `backfill=` | `test_domain_fingerprint_changes_with_slim_mode`、`test_shared_cache_slim_then_full_does_not_poison_switch_off`、`test_shared_cache_full_then_slim_does_not_lose_backfill`（共享缓存下 full 仍自己抽、`backfill is None`/slim 仍有回填） |
| S1 | should | chunk1 漏抽实体时，chunk0 的别名仍被误报幻觉 | `merge_backfill_reports(..., document_text=...)` 对「仍未判定的候选别名」在整篇正文补一次 AC 扫描；`pipeline.run` 传 `document.text`。只有整篇都不存在的别名才计入 `unmatched_aliases` | `test_missing_entity_in_one_chunk_does_not_report_alias_hallucination`、`test_merge_backfill_reports_scans_document_text_for_unmatched_aliases`、`test_merge_backfill_reports_keeps_alias_absent_from_whole_document`（反向：真不存在仍报） |
| S2 | should | 验收 8 关系轴 full 0.0000 / slim 0.0133，0 对 0 空转 | 压测脚本新增 `--domain stress`（默认）：在 general 类型表上并入 02 gold 的 `供职于/户籍在/注册地位于/涉案关联`，两种模式用同一个域；证据同时报这些谓词的召回，要求 baseline 非零 | 见下节 A/B 与 `03-r3-stress-ab.json` 的 `domain`/`aggregate.*.predicate_recall`；baseline 关系 F1 与谓词召回均非零 |
| N1 | nit | 回填已接受唯一别名端点，`rule_review` 仍只认标准名；`known` 死变量 | ② 改用「字面 → 实体集合」索引，标准名或唯一归属别名都认；同一字面归属多个实体时不猜。删除死变量 `known` | `test_rule_review_accepts_unique_alias_relation_endpoint`、`test_rule_review_still_rejects_ambiguous_alias_relation_endpoint` |
| N2 | nit | 验收 8/9 证据的 `git_sha` 停在 `a4c10cd`（未提交工作树） | 重跑证据在开跑时采集 `git_sha` / `git_describe --dirty` / `git_dirty`（只算已跟踪改动）；本轮 JSON 指向被评审提交，不再用旧 sha | `03-r3-stress-ab.json` 的 `git_sha=9f95ed48…`、`git_dirty=false`；字段由 `tools/backfill_stress_ab.py::_git_identity` 生成 |
| N3 | nit | 实体 macro 只有单次 3 样本，且 1 篇跌 2.75pt | `--repeat 3`（可调）+ `--temperature 0` 显式固定；证据同时给逐轮明细与 `aggregate` 的均值/最小值，逐篇也报 mean/min；provider 不暴露 seed 的事实写进 `sampling.seed_note`，不再声称单次可复现 | `03-r3-stress-ab.json` 的 `sampling`、`repeats`、`aggregate`；`tests/test_llm_providers.py::test_temperature_is_opt_in_and_recordable` 钉住 `LLM_TEMPERATURE` 默认不改变请求体 |
| N4 | nit | 重叠块的计数信号被放大（`block_confidence`/`matched_literals` 非文档级去重） | 明确并钉住 **chunk-weighted** 口径：`BackfillReport` 字段注释、`merge_backfill_reports` docstring、`docs/ARCHITECTURE.md` 均写明重叠块各计一次，需要文档级去重的消费方按 `(entity_key, span)` 收敛 | `test_overlapping_chunk_counts_are_chunk_weighted`（重叠两块的 `entities_total=2`、`matched_literals=2`、`block_confidence=1.0`） |
| N5 | nit | 字面→实体索引建了两遍，口径易漂移 | 抽出 `_literal_index(entities)`（去空/strip/别名去重），`_entity_patterns` 与 `_backfill_relations` 共用 | `test_relation_endpoint_resolves_unique_alias_literal`、`test_relation_endpoint_ambiguous_alias_is_not_guessed` 仍通过；全量回归 319 passed |
| N6 | nit | `merge_relation_duplicates` 同时控制关系合并与 description 补全 | `naive_merge` 新增独立 `fill_description=False` 参数；pipeline 仅在 slim 打开时显式同时传 `merge_relation_duplicates=True, fill_description=True`，关闭态保持 262319b | `test_naive_merge_fill_description_is_independent_of_relation_merge`；`test_naive_merge_default_keeps_pre_change_semantics` 仍通过 |
| N7 | nit | 测试名宣称「identical to before」但只比 schema/prompt/flag | 改名为 `test_default_switch_off_selects_full_schema_and_keeps_flag_off`，docstring 指向真正做完整输出 golden 的两条用例 | `tests/test_backfill_extractor.py` 该用例；`test_switch_off_extraction_output_matches_262319b_golden`、`test_switch_off_chunked_output_matches_262319b_golden` 仍全绿 |

## 二、验收 8/9：真实 LLM A/B（02 压力语料，3 轮重复）

命令（密钥只从本地 env 读取，不落仓；`LLM_TEMPERATURE` 由脚本设置）：

```bash
PYTHONPATH=src python tools/backfill_stress_ab.py \
  --gold data/gold/stress --env-file ../moray/.env \
  --repeat 3 --temperature 0 --concurrency 6 \
  --json-out docs/evidence/03-r3-stress-ab.json
```

- 证据元数据：`git_sha=9f95ed48…`、`git_describe=9f95ed4`、
  `git_dirty=false`；`sampling.repeat=3`、`temperature=0.0`、`seed=null`
  （provider 的 `ChatModel.Parameters` 不暴露 seed，见 `seed_note`）。
- 域：`stress_eval` = general 类型表 + 02 gold 的
  `供职于/户籍在/注册地位于/涉案关联`，full/slim 同一域、同一模型
  （`deepseek-flash`）、同一语料（3 篇 / 79,362 字）、同一分块预算
  （1500 units / overlap 200，每模式 78 块）。
- 3 轮重复，每轮 full+slim 各一遍；下表为 `aggregate` 的均值（括号内最小值）。

| 指标 | full | slim | 结论（3 轮均值） |
|---|---|---|---|
| 实体 macro relaxed F1 | 0.8977（min 0.8910） | **0.8984（min 0.8962）** | **+0.07pt**（单轮区间 −1.30 ~ +1.00pt） |
| 实体 strict F1 | 0.0135 | **0.9676** | slim 回填 span 精确 |
| 关系 F1（3 篇 macro 均值） | 0.3245（min 0.2974） | **0.7195（min 0.6817）** | **+39.50pt**（单轮区间 +34.08 ~ +45.98pt） |
| 02 四谓词关系召回 | 0.2982（min 0.2619） | **0.9184（min 0.9184）** | baseline 非零，关系轴可判 |
| span 一致率 min / mean | 0.0099 / 0.0142 | **1.0000 / 1.0000** | min 取 3 轮最坏值；slim 不低于改造前 |
| 单块平均 output token | 2365.98 | **582.13** | **−75.32%**（单轮 −73.82% ~ −78.14%，≥30% 达标） |
| 截断块数 / 截断信号 | 逐轮 4/6/2，均值 4.00 | **逐轮 0/0/0** | 不增加（取最坏值 6→0） |
| 失败块数 | 0 | 0 | 无静默丢块 |

逐篇实体 macro relaxed F1（3 轮均值）：seed20261102 `0.8893 → 0.8985`
（+0.92pt）、seed20261103 `0.8779 → 0.8996`（+2.17pt）、seed20261104
`0.9259 → 0.8971`（**−2.88pt**）。逐篇关系 F1：
`0.2787→0.7774`、`0.3546→0.5914`、`0.3403→0.7895`。

**结论与波动如实保留**：验收 8 的实体门槛按「3 轮语料 macro 均值」判定为
+0.07pt（跌幅远小于 0.5pt），关系 F1 在非零 baseline（0.3245）上 +39.50pt；
但单轮经验区间仍到 −1.30pt，且 seed20261104 逐篇均值 −2.88pt。这正是
r4 N3 指出的单次采样脆弱性：本轮用 3 轮均值/最小值把它显式暴露，不再用
单次 +0.93pt 宣称稳健。

## 三、工单 03 十条验收复核（本轮修复后）

| # | 条目 | 结论 | 依据 |
|---|---|---|---|
| 1 | 先红后绿 | **本轮可核** | r4 回归 `de82da5` 先红 8 failed → 修复后 10 passed；首版历史不可追溯仍如实标注 |
| 2 | N 次出现 → N mention、span 100% 原文切片 | 达成 | 既有用例 + r4 全量回归；A/B 的 slim span 一致率见上表 |
| 3 | 前缀/子串最长匹配 | 达成 | 既有最长匹配/部分重叠/20k 性能用例全绿 |
| 4 | 别名/标准名回填；幻觉别名信号 | **修复 S1 后达成** | 整篇正文口径：`test_missing_entity_in_one_chunk_does_not_report_alias_hallucination` |
| 5 | 同句证据；跨句/跨块不臆造 | 达成 | 既有证据回填 + S3 规则④用例全绿 |
| 6 | 同名歧义未归属 | 达成 | 既有同名/别名冲突/重叠块去重用例全绿 |
| 7 | 关闭态逐字段一致 + 默认关 | **缓存维度补齐** | B1 三条用例：共享 05 缓存下 slim/full 键不同，关闭态 `backfill is None`；262319b 输出/chunk golden 仍全绿 |
| 8 | 02 压力语料 F1/关系 F1/span | **本轮用可判域重跑** | 见上节：关系 baseline 非零；3 轮 mean/min |
| 9 | 单块 token ≥30% 降、截断不增 | **同一实验重测** | 见上节：逐块 token 与截断块数（3 轮均报） |
| 10 | 纯本地、每块只扫一遍（线性） | 达成 | 带计数用例 `test_backfill_does_not_scan_full_text_per_alias`；S1 的整篇补扫只针对少量候选别名，仍为线性 |

## 四、未做 / 剩余风险

- `RUN_DB_TESTS=1` 本轮未跑（会 TRUNCATE 共享表，沿用仓内既有门控）。
- provider 参数不暴露 `seed`，A/B 用 `temperature=0` + 3 轮重复报均值/最小值，
  不是单次可复现；逐篇波动仍如实保留。
- 本轮不修改产品默认行为：`LLM_TEMPERATURE` 未设置时请求体与历史逐字节一致。
