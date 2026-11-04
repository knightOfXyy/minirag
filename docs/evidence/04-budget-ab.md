# 工单 04：分块预算 1500 → 3500 A/B 与上线判据（评审 r5 修复后：关系 raw+normalized 双口径 gate + overlap 证据修正）

- 分支/worktree：`yzc/tmp-yzd-feat-relation-202610-04`
  （`/home/dell/orca/workspaces/minirag/tmp-yzd-feat-relation-202610-04`）
- 代码提交：`4698445`（本轮 r5 修复：S1/S2 + N1–N4）；原始 A/B 数据
  采集自 `e78dd35`、采集时 `git_dirty=false`；上一轮 r4 修复为 `5036760`
- 证据提交：`d67d682`（报告/日志/修复说明）+ 本次重生成 JSON 的 evidence
  commit（gate/thresholds/overlap/coverage 由 `--from-json` 从既有逐轮数据
  复算，未重跑 LLM）
- 模型：`deepseek-flash`（provider `deepseek`，`api.deepseek.com`），temperature=0，
  **5 轮独立重复**（无 seed）；压力语料 3 篇 / 3 seed / 79,362 字；
  真实文档 **12 篇**（6 篇黄金集短样本 + 5 篇 samples + 1 篇 5,360-unit 长文
  `docs/ARCHITECTURE.md`，用于真正压 3500 档）
- 原始数据：`docs/evidence/04-budget-ab.json`（逐轮、逐篇聚合与单块极值 +
  **逐篇预测实体/关系面（原始端点 + 归一化端点）**；JSON 不含逐块文本明细，
  单块的 token/units 只保留最大/汇总值）；本轮未重跑 LLM，
  gate/thresholds 由 `tools/chunk_budget_ab.py --from-json` 从既有逐轮数据
  复算，JSON `gate_recompute` 记录源提交与重算时间。

## 结论（先看这里）

1. **配置面达成**：现有配置项 `MORAY_CHUNK_MAX_UNITS=3500` 可设；`overlap_units`
   默认 200 与行为不变。**默认值仍为 1500，本单没有切换。**
2. **实体轴：两套口径都进 gate，原始口径不通过**。类型前缀标签
   （`案件编号 A2024-942` → `A2024-942`）归一化后，3500 档实体 macro-F1
   **+6.09pt（mean）/ +3.36pt（逐轮对齐 worst-of-N）**；但原始未归一化验收口径是
   **−2.92pt（mean）/ −5.44pt（逐轮对齐 worst-of-N）**，不满足验收 2 的 0.5pt
   门槛。评审 r4 B1 修复后，gate 的实体轴**同时**检查 raw 与 normalized 的
   mean/worst（4 条 check），原始口径掉 2.92/5.44pt 时 raw 两条直接 fail，
   辅助口径无法再单独放行。归一化只影响评测、生产仍会吐带前缀的名字；
   是否接受该规则/扩金标由 owner 决定（见文末）。
3. **关系轴仍不放行，gate 同时检查 raw 与 normalized 全量 F1**。修掉关系端点
   未归一化的评测 bug 后（F1），3500 的 2,858 条预测关系里 **1,080 条
   （37.79%）**曾因头/尾带 `案件编号 ` 前缀永远匹配不上 gold；修正后覆盖子集
   recall 由“假失败”翻为 **+8.30pt（mean）/ +2.63pt（worst）**，已通过
   “不下降”。但 precision 仍 **−12.28pt / −28.66pt**；全量关系 F1 的
   **归一化口径 −6.32pt / −16.33pt**，**原始未归一化口径（验收口径）
   −10.88pt / −19.28pt**（评审 r5 S1 补进 gate）。评审 r2 F2 把全量 F1 加回
   `checks`、r5 S1 再把 raw 全量 F1 并列为必填后，gate 仍 `passed=false`
   （本轮 **8 条 failure**，含实体 raw 两条 + 关系 raw 两条）。
   **默认保持 1500，未添加切换 commit。**
4. **成本面：调用/token 达成，墙钟未改善**。单文档调用 26 → 9（**−65.38%**），
   输入 token **−43.64%**，输出 token **−22.61%**；端到端墙钟 **+14.25%**
   （块变大、单次调用变长，如实列报）。压力语料与 12 篇真实文档截断块均 **0**；
   真实 tokenizer 口径最大块 **4,448 input tokens**（来自 5,360-unit 长文），
   input+output 余量 **1,038,726 tokens（99.06%）**。

## 可复现命令

```bash
cd /home/dell/orca/workspaces/minirag/tmp-yzd-feat-relation-202610-04
PYTHONPATH=src .venv/bin/python tools/chunk_budget_ab.py \
  --gold data/gold/stress --real-gold data/gold --samples data/samples \
  --real-doc docs/ARCHITECTURE.md \
  --env-file ../moray/.env \
  --budgets 1500,3500 --overlap-units 200 \
  --repeat 5 --concurrency 6 --temperature 0 \
  --json-out docs/evidence/04-budget-ab.json
```

（本轮实际用装有 agentscope 的 venv：`/home/dell/orca/workspaces/minirag/moray/.venv/bin/python`；
密钥只从 `../moray/.env` 读取，不落仓。）

评审 r4 只改判据、不重跑 LLM；gate/thresholds 由既有逐轮数据重算：

```bash
PYTHONPATH=src .venv/bin/python tools/chunk_budget_ab.py \
  --from-json docs/evidence/04-budget-ab.json \
  --json-out docs/evidence/04-budget-ab.json
```

同一份 02 压力语料、同一个模型、同一组采样参数、同一个 `slim + 本地回填`
抽取路径，只把 `Chunker(max_units=...)` 从 1500 换成 3500；`overlap_units`
两档都固定 200。每轮逐篇记录调用数、真实 input/output token、墙钟、截断块/信号、
失败块、预测面，并用 `usage.input_tokens`（provider 真实 tokenizer）记最大单块输入，
用 `/models` 的 `context_window` / `max_output_tokens` 算余量。

### 本次 A/B 的真实 token 消耗（只算压力语料 5 轮）

| 预算 | 输入 token（5 轮合计） | 输出 token（5 轮合计） |
|---|---:|---:|
| 1500 | 738,690 | 227,712 |
| 3500 | 416,320 | 176,220 |
| 合计 | **1,155,010** | **403,932** |

口径：这是抽取请求本身的 provider token，**不含本地评测计算开销**（评测是纯离线
Python，不发请求）；不含 12 篇真实文档批次（每篇 1–2 块）、冒烟与单篇诊断调用。
金额以 provider 账单为准。

## 主表：两档同口径（真实 LLM + slim 回填，5 轮独立重复）

| 指标 | 1500 | 3500 | 变化（mean / worst-of-N） |
|---|---:|---:|---:|
| 实体 macro-F1（归一化后，辅助口径） | 0.8974 / 0.8874 | 0.9583 / 0.9346 | **+6.09pt / +3.36pt** |
| 实体 macro-F1（原始未归一化，**验收口径 + gate**） | 0.8974 / 0.8874 | 0.8682 / 0.8466 | **−2.92pt / −5.44pt** |
| 实体 strict-F1（mean） | 0.9624 | 0.9570 | −0.54pt |
| 关系 F1（全量，归一化口径，辅助，也进 gate） | 0.7042 / 0.5876 | 0.6410 / 0.6151 | **−6.32pt / −16.33pt** |
| 关系 F1（全量，原始未归一化，**验收口径 + gate**） | 0.7042 / 0.5876 | 0.5954 / 0.5908 | **−10.88pt / −19.28pt** |
| 关系 recall（金标覆盖子集，micro） | 0.7731 / 0.6842 | 0.8561 / 0.8275 | **+8.30pt / +2.63pt** |
| 关系 precision（金标覆盖子集，micro） | 0.6353 / 0.4979 | 0.5125 / 0.4896 | **−12.28pt / −28.66pt** |
| 单文档分块调用数 | 26.0 | 9.0 | **−65.38%** |
| 单文档 input token | 49,246.0 | 27,754.7 | −43.64% |
| 单文档 output token | 15,180.8 | 11,748.0 | −22.61% |
| 单块 output token（均值） | 583.88 | 1,305.33 | +123.55%（块变大，总量仍降） |
| 单文档端到端墙钟（s） | 13.0936 | 14.9589 | **+14.25%** |
| 截断块（压力语料，5 轮最大值） | 0 | 0 | 持平 |
| 截断块（12 篇真实文档） | 0 | 0 | 持平 |
| 失败块 | 0 | 0 | 持平 |

**口径警告（评审 r2 F7）**：实体 macro-F1 与全量关系 F1 是“逐篇先算 F1、
再跨篇/跨轮平均”的 **macro**；覆盖子集是“TP/FP/FN 跨篇池化”的 **micro**。
两列不能当成同一口径直接比较。按逐篇 macro 重算覆盖子集，recall 仍是
+8.30pt（worst +2.63pt），precision 是 −16.13pt（worst −28.67pt），
方向与 micro 一致（precision 更差），不改变 gate 结论。JSON `gate.caliber`
显式记录三种口径。

`mean` 是 5 轮独立重复的均值；表中“worst”列是**各档自己最差的一轮**，
而“变化”列的 `worst-of-N` 是**逐轮对齐后最坏的一轮差**（`delta_summary.min_pt`），
两者不是同一算法——例如全量关系 F1 两档自身最差轮相减是 +2.75pt，逐轮对齐
最坏是 −16.33pt，本表用后者做门槛。两档是先后独立跑、provider 不暴露 seed，
**不是配对检验**；95% CI 按独立重复计算（下表）。墙钟本轮上升，如实列报：
调用数虽然 −65%，但 3500 档每块 prefill/decode 更多，单次调用明显变长。

### 均值差与 95% CI（5 轮独立重复，unpaired）

| 口径 | 均值差 | 95% CI | 最坏一轮 |
|---|---:|---:|---:|
| 实体 macro-F1（归一化后） | +6.09pt | [+4.03, +8.15] | +3.36pt |
| 实体 macro-F1（原始，验收口径） | −2.92pt | [−4.84, −1.01] | −5.44pt |
| 关系 F1（全量，归一化口径） | −6.32pt | [−17.38, +4.75] | −16.33pt |
| 关系 F1（全量，原始口径，验收） | −10.88pt | [−20.83, −0.92] | −19.28pt |
| 关系 recall（覆盖子集） | +8.30pt | [+1.35, +15.25] | +2.63pt |
| 关系 precision（覆盖子集） | −12.28pt | [−29.45, +4.88] | −28.66pt |

`delta_summary` 的完整数值、每轮差值与 `n` 都在 JSON 里；CI 用 t 分布
（n=5，t=2.776），标签明确写 `unpaired independent repeats`。

### 逐轮 macro（mean-of-N 与 worst-of-N 的原始来源）

| 轮 | 1500 实体（归一化） | 3500 实体（归一化） | 实体差 | 1500 关系（全量） | 3500 关系（全量） | 关系差 |
|---|---:|---:|---:|---:|---:|---:|
| 1 | 0.9010 | 0.9346 | +3.36pt | 0.6823 | 0.6455 | −3.68pt |
| 2 | 0.8944 | 0.9571 | +6.27pt | 0.7784 | 0.6151 | −16.33pt |
| 3 | 0.8874 | 0.9660 | +7.86pt | 0.5876 | 0.6434 | +5.58pt |
| 4 | 0.9067 | 0.9696 | +6.29pt | 0.6864 | 0.6549 | −3.15pt |
| 5 | 0.8975 | 0.9642 | +6.67pt | 0.7862 | 0.6462 | −14.00pt |
| 聚合 | mean 0.8974 / worst 0.8874 | mean 0.9583 / worst 0.9346 | **mean +6.09 / worst +3.36** | mean 0.7042 / worst 0.5876 | mean 0.6410 / worst 0.6151 | **mean −6.32 / worst −16.33** |

上表关系列是**归一化口径（辅助）**。评审 r5 S1 补进 gate 的**原始未归一化
全量关系 F1** 逐轮为 1500→3500：0.6823→0.5928、0.7784→0.5978、
0.5876→0.5908、0.6864→0.6023、0.7862→0.5934；逐轮差
`[-8.95, -18.06, +0.32, -8.41, -19.28]pt`，mean **−10.88pt** /
逐轮对齐 worst **−19.28pt**。原始值在
`budgets.<budget>.rounds[*].raw_macro.relation.f1`。

覆盖子集逐轮 recall（1500 → 3500）：0.7895→0.8596、0.8012→0.8275、
0.6842→0.8596、0.7895→0.8743、0.8012→0.8596，五轮全为正（+7.01 / +2.63 /
+17.54 / +8.48 / +5.84pt）；precision 逐轮为 −4.14 / −28.66 / +1.61 /
−4.60 / −25.64pt。逐轮 precision / TP/FP/FN 在
`budgets.<budget>.rounds[*].relation_covered_*`。

## 实体轴：类型前缀标签归一化（评审 B2 / r2 F1/F4/F5）

**规则（写在代码里，不是本次特判）**：按实体类型声明受控前缀标签，
`TYPE_LABEL_PREFIXES = {"EVENT": ("案件编号", "案号", "编号")}`；
relaxed 匹配前剥掉前缀（`案件编号 A2024-942` → `A2024-942`）。
**strict 口径不动**：它比 `mention.surface`，前缀仍是模型原文，不能被别名“救活”。
实现：`src/moray/evaluation/stress/metrics.py`
（`normalize_entity_name_aliases` / `apply_type_label_aliases`）。

评审 r2 的两处加固：

- **F1**：`apply_type_label_aliases` 现在同时归一化**关系头/尾**——先按端点
  解析到的实体类型剥，类型查不到时只回退到**文档中实际出现过的声明类型**
  （评审 r4 N6，避免未来类型/换语料时全表乱剥造假 TP）。A/B 工具落盘原始端点
  与归一化端点，1080 条错配可逐条审计。
- **F4**：归一化改为 `evaluate_case(..., normalize_type_labels=False)` 的
  **opt-in**；02/03 的入口（`stress/runner.py`、`tools/backfill_stress_ab.py`）
  保持默认 False，历史基线口径不被静默改写。只有 04 的
  `tools/chunk_budget_ab.py` 传 True。

**B1 / F5：这是辅助口径，不能替代验收口径。** 评审 r4 B1 修复后，gate 的
实体轴同时要求 **raw（验收）** 与 **normalized（辅助）** 两套 macro-F1 的
mean/worst 都 ≤0.5pt；原始口径 −2.92pt / −5.44pt，因此 raw 两条 check 直接
fail，不再出现“归一化全绿就放行”。归一化规则只影响评测、不影响线上输出
（生产仍会吐 `案件编号 A2024-942`）。是否接受该规则、是否在生产侧归一化，
或扩 02 金标后再判，仍待 owner 决定（见文末）。

两口径并列（5 轮）：

| 口径 | 1500 mean / worst | 3500 mean / worst | 变化 mean / worst |
|---|---:|---:|---:|
| 归一化后（辅助口径，也进 gate） | 0.8974 / 0.8874 | 0.9583 / 0.9346 | **+6.09pt / +3.36pt** |
| 原始未归一化（验收口径，也进 gate） | 0.8974 / 0.8874 | 0.8682 / 0.8466 | **−2.92pt / −5.44pt** |

原始口径里 3500 的 EVENT 是 **P=0 / R=0**：120 条 gold 全漏、120 条预测
（名字带 `案件编号 ` 前缀）全记 FP；归一化后 EVENT 变成 **P=1 / R=1**。
归一化后按类型（3 篇 × 5 轮微平均）：

| 类型 | 1500 P | 1500 R | 3500 P | 3500 R | 说明 |
|---|---:|---:|---:|---:|---|
| PER | 0.9934 | 1.0000 | 0.9772 | 1.0000 | 3500 略低但仍高 |
| ORG | 0.8438 | 1.0000 | 0.9343 | 1.0000 | 3500 更好 |
| LOC | 1.0000 | 1.0000 | 1.0000 | 1.0000 | 持平 |
| EVENT | 0.0000 | 0.0000 | 1.0000 | 1.0000 | 1500 模型不吐 EVENT；3500 归一化后全对 |
| MISC | 1.0000 | 0.3714 | 1.0000 | 0.4357 | 3500 略好 |

## 关系轴：全量口径 vs 金标覆盖子集（F1/F2/F7）

- **全量口径**：预测三元组 vs 全部 gold，未标注句面的预测照旧记 FP。
- **金标覆盖子集**：先由 gold 关系定位“头与尾同句”的句子，只统计落在这些
  句子里的预测与 gold；预测落在标注句之外不能把漏报“过滤掉”（recall 分母
  仍是全部被覆盖的 gold 关系）。实现：
  `src/moray/evaluation/stress/metrics.py::relation_covered_stats`。
- **F1 修复前后**：3500 档 2,858 条预测关系里 **1,080 条（37.79%）**原始头/尾
  带 `案件编号 ` 前缀（1500 档 0/2,159 条）；旧实现只归一化实体、不归一化关系，
  这些关系永远匹配不上 gold，覆盖子集 recall 的 −0.94/−2.34pt 是**假失败**。
  修正端点后 recall 变为 **+8.30pt / +2.63pt**，五轮全为正。
- **这是输出表面形态退化，不是评测噪声**（评审 r4 B1）：3500 档模型把
  `案件编号 ` 并进实体名/关系端点的比例从 **0% 升到 37.79%**（EVENT 预测
  120 条全部带前缀），入库后实体名会对不上；此前 Ingestor 建 MISC 占位就是
  这类表面形态问题的后果。gate 因此必须同时看 raw 口径，不能只靠评测侧
  归一化把差异抹平。
- **F2 放行 gate**：工单验收 2 要求“关系 F1 不下降”，所以 gate 的 `checks`
  现在同时包含 **全量关系 F1 mean/worst 不下降** 与覆盖子集 recall/precision；
  全量口径不再只是“列报”。
- **r5 S1：全量关系 F1 也分 raw/normalized 双口径**。评审 r4 对实体轴确立了
  “raw 是验收口径、归一化只是辅助”的规则，关系轴此前仍只把归一化值放进
  `checks`。本轮把 `raw_relation_f1_*` 设为必填，并新增
  `relation_raw_full_f1_{mean,worst}_not_lower` 两条 check；3500 档实际
  raw −10.88/−19.28pt，因此即便归一化口径也下降、raw 更差，gate 都 fail。
  归一化口径只作为辅助列报，不能替代原始验收口径。

| 口径 | 1500 | 3500 | 变化 mean / worst |
|---|---:|---:|---:|
| 全量关系 F1（归一化，辅助） | 0.7042 / 0.5876 | 0.6410 / 0.6151 | **−6.32pt / −16.33pt** |
| 全量关系 F1（原始，**验收**） | 0.7042 / 0.5876 | 0.5954 / 0.5908 | **−10.88pt / −19.28pt** |
| 覆盖子集 recall（micro） | 0.7731 / 0.6842 | 0.8561 / 0.8275 | **+8.30pt / +2.63pt** |
| 覆盖子集 precision（micro） | 0.6353 / 0.4979 | 0.5125 / 0.4896 | **−12.28pt / −28.66pt** |
| 覆盖子集 TP / FP / FN（合计） | 1322 / 812 / 388 | 1464 / 1393 / 246 | TP +142、FP +581、FN −142 |
| 全量 TP / FP / FN（合计） | 1322 / 837 / 388 | 1464 / 1394 / 246 | TP +142、FP +557、FN −142 |

**F7 口径说明**：上表前两行是 macro，覆盖子集两行是 micro。按逐篇 macro
重算覆盖子集，recall 仍是 +8.30pt（worst +2.63pt），precision 为 −16.13pt
（worst −28.67pt）——方向与 micro 一致，precision 更差，结论不变。
gate 返回的 `caliber` 字段把三种口径写死，报告不再把两列当同一口径比较。

结论：**F1 修复后 recall 不再是问题，precision / 全量 F1 的下降才是真实
金标缺口**。3500 把更多真实存在但 02 未标注的句面关系抽出来（归一化口径
全量 FP 837 → 1394），TP 反而从 1322 升到 1464、FN 从 388 降到 246；F1 下降
完全由 precision 驱动。原始口径还叠加 1,080 条端点前缀无法匹配 gold，因此
全量 F1 更低（−10.88/−19.28pt）。本单不改 02 金标，默认保持 1500。

### 全量关系 FP 来源分布（逐篇预测面从 JSON 重算，5 轮合计）

| 谓词 | 1500 FP | 3500 FP | 增量 |
|---|---:|---:|---:|
| `涉案关联` | 461 | 1037 | **+576** |
| `任职于` | 308 | 270 | −38 |
| `位于` | 60 | 71 | +11 |
| `注册地位于` | 0 | 10 | +10 |
| `供职于` | 4 | 6 | +2 |
| 其他（`通报` 等） | 4 | 0 | −4 |
| 合计 | **837** | **1394** | **+557** |

`涉案关联` 的增量仍然最大：卷四每案一句里列 5 名涉案人，gold 只记第 1 人；
3500 读到更多上下文后把同句其余 4 人也读出，这是 02 金标缺口，不是 3500
把已认可答案抽丢（TP 反而增加）。

## 截断护栏（工单 01 的 `DocumentMetrics`）

| 语料 | 1500 截断块 | 3500 截断块 | 1500 信号 | 3500 信号 | 失败块 |
|---|---:|---:|---:|---:|---:|
| 02 压力语料（3 篇 × 5 轮） | 0 | 0 | 0 | 0 | 0 |
| 真实文档批次（12 篇） | 0 | 0 | 0 | 0 | 0 |

真实文档批次 = `data/gold/*.tagged.txt` 的 **6 篇黄金集短样本** +
`data/samples/` 的 **5 篇** + `--real-doc docs/ARCHITECTURE.md` 的
**1 篇真实长文**（5,360 units）。评审 r2 F3 修复点：

- 上一轮 11 篇最大块仅 341 units，3500 档从未被压到；本轮长文在 3500 档
  切成 **2 块、最大块 3,483 units**，截断 0；1500 档 4 块、最大 1,497 units。
- JSON `real_doc_coverage.exercises_candidate_budget=true`，`note` 写明
  最大文档 5,360 units / 最大块 3,483 units。
- 该长文是仓库技术文档，不是案件卷宗；它能证明 3500 档的真实长文分块与
  截断行为，但不能替代“真实案件长文”的语料覆盖。后续可用 `--real-doc`
  传入脱敏案卷，不需要改代码。

## 真实 tokenizer 上下文余量（不是 estimate_tokens）

`usage.input_tokens` 是 provider 对**完整请求**的真实计数，已经包含
正文 + 系统提示 + 结构化 schema；`max_chunk_input_tokens` 取压力语料与真实
文档两批的最大值（单块多次尝试时取和，属保守上界）。

| 口径 | 1500 | 3500 |
|---|---:|---:|
| 最大单块 input tokens（真实 tokenizer） | 2,614 | **4,448** |
| 最大单块 output tokens | 5,482 | 5,402 |
| 模型 context_window | 1,048,576 | 1,048,576 |
| input 余量（tokens / %） | 1,045,962 / 99.75% | **1,044,128 / 99.58%** |
| input+output 余量（tokens / %） | 1,040,480 / 99.23% | **1,038,726 / 99.06%** |
| 模型 max_output_tokens | 393,216 | 393,216 |
| output 上限余量 | 387,734 | 387,814 |

- 3500 档最大 input 4,448 tokens 来自 5,360-unit 长文；压力语料本身最大
  3,836 tokens。即使把系统提示、schema、正文全算进去，也只占上下文的
  0.42%，距上限还有 **1,038,726 tokens（99.06%）** 的安全余量；实测截断 0。
- 最大块估算 3,597 units（overlap 可让块超过 `max_units` 至多一个 overlap，
  这是改造前就有的语义），真实 tokenizer 计数 4,448；两者分开标注。

## overlap 不变的证明（评审 S3：冻结基线，不再自证）

- **基线冻结**：`DEFAULT_OVERLAP_UNITS=200` 写死在 A/B 工具，基线档
  `overlap_units` 取这个常量，不再与候选档共用同一个 `args.overlap_units`。
- **候选配置**：`candidate.overlap_units=200`，等于冻结基线；不等即失败。
- **实际行为（评审 r5 S2 修正）**：相邻块“后块起点早于前块终点”的重叠对数
  并不是逐对都成立。按仓库 `Chunker` 复算，压力语料 1500 档每篇 26 块、
  实际重叠 **8/25** 对；3500 档每篇 9 块、实际重叠 **7/8** 对。长句
  （>200 units）会让 `overlap_seed` 为空（`chunking.py:140-148`），该对
  不会重叠；因此报告不再写“相邻块逐对重叠”。
- **合成用例**才是逐对断言：`tests/test_chunk_budget.py` 用正常句界的正文
  验证每一对相邻块 **0 < overlap ≤ 200**（评审 N1 加固）；压力语料只保证
  `overlap_pairs_min > 0`，gate 的 `overlap_observed` 因此只是冒烟检查。
- 证据字段：JSON `overlap_frozen_baseline` +
  `overlap_observed.{baseline,candidate}`（实际对数/可重叠对数/比例）+
  `gate.checks.overlap_*`。

## 残余文档级跨块关系（显式统计，不算本单回归）

口径：对每条 gold 关系，只要**存在一对头/尾出现落在同一个块内**，就算该预算
可覆盖；否则算跨块残余。残余逐条列在 JSON 的
`residual_cross_chunk.<budget>.residual_relations`。

| 预算 | 可同块 | 跨块残余 | 残余比例 |
|---|---:|---:|---:|
| 1500 | 338 / 342 | 4 | 1.1696% |
| 3500 | 342 / 342 | 0 | **0.0000%** |

另外单列“文档级远距首现”类别：头尾**首次出现**间隔超过一个块预算的关系，
两档都是 **60 / 342 = 17.5439%**；这类关系靠“卷尾重复点名”才能被同块覆盖。
它们是**调大块预算也无法从文档结构上救回**的类别，本单不处理、不计作回归、
也不静默丢弃；JSON 中样本逐条带 `covered_by_repeated_mention` 标记，样例
截断到 20 条时另有 `far_first_occurrence_examples_total` 与
`..._truncated`（评审 r2 F11）。

评审 r2 F13：覆盖子集的 recall 分母只收“头尾同句”的 gold 关系；跨句 gold
关系既不在 covered spans，也不进分母。本轮 `uncovered_gold_relations_total=0`
（语料恰好全部同句），但字段已落盘；将来出现跨句 gold 关系时会被显式计数，
不会静默消失。

> 工单描述里的“约 7%”来自另一次 1.8 万字压力案卷的离线探针，不是本 02 语料
> 的直接复现；本报告只报本次实测的 0% / 1.17% 与 17.54% 两个口径，不冒充 7%。

## 验收逐条对照

| # | 验收条目 | 结果 |
|---|---|---|
| 1 | 3500 可经现有配置项设置；overlap 默认值与行为不变（配置级测试） | **达成**：默认 1500/200；3500 可设且 overlap 仍 200；合成用例逐对断言 0<x≤200；压力语料实际重叠 1500 8/25、3500 7/8（长句使 seed 为空，非逐对） |
| 2 | 1500/3500 同口径：实体 macro-F1 跌 ≤0.5pt、关系 F1 不降、附类型 P/R | **未通过（不切换）**：实体原始验收口径 −2.92/−5.44（gate raw 两条 fail）；归一化后 +6.09/+3.36 只作辅助口径且同样进 gate；全量关系 F1 归一化 −6.32/−16.33、原始 −10.88/−19.28（两条 raw check 也 fail）、覆盖子集 precision −12.28/−28.66（不过）；覆盖子集 recall +8.30/+2.63（过） |
| 3 | 同报告含调用数、输入/输出 token、端到端耗时，方向与探针一致 | **调用/token 达成，墙钟未改善**：调用 −65.38%，输入 −43.64%，输出 −22.61%，墙钟 +14.25%（如实） |
| 4 | 3500 档压力语料 + 真实文档截断块为 0（或不高于 1500） | **达成**：压力 0/0；真实 12 篇 0/0，含 5,360-unit 长文在 3500 档切成 2 块、最大 3,483 units、截断 0；`exercises_candidate_budget=true` |
| 5 | 真实 tokenizer 核算最大块总输入并给出安全余量 | **达成**：4,448 tokens（来自长文），input+output 余量 1,038,726（99.06%），见上表 |
| 6 | 残余文档级跨块关系比例显式统计并写入报告 | **达成**：1500=1.17%，3500=0%；文档级远距类别 17.54% 另列；跨句 gold 未覆盖计数已落盘（本轮 0） |
| 7 | 改默认值是独立可回滚步骤；未切换时线上行为不变 | **未切换**：默认仍 1500；切换 commit 未添加；配置未设时行为由测试锁定不变 |
| 8 | 既有分块偏移不变量测试全部通过 | **达成**：`tests/test_chunking.py` + 新增 3500 偏移/切片断言全绿 |

## 默认值切换：本单未执行

当前代码默认值仍是 `chunk_settings()` 的 `max_units=1500`、`overlap_units=200`。
本轮 gate 的实际 failure（JSON `gate.failures`）：

```
entity_raw_mean_within_0_5pt                  # -2.92pt（B1 新增）
entity_raw_worst_within_0_5pt                 # -5.44pt（B1 新增）
relation_full_f1_mean_not_lower                # -6.32pt（F2 新增，归一化）
relation_full_f1_worst_not_lower               # -16.33pt（F2 新增，归一化）
relation_raw_full_f1_mean_not_lower            # -10.88pt（r5 S1 新增，原始）
relation_raw_full_f1_worst_not_lower           # -19.28pt（r5 S1 新增，原始）
relation_covered_precision_mean_within_0_5pt   # -12.28pt
relation_covered_precision_worst_within_0_5pt  # -28.66pt
```

实体 normalized mean/worst（+6.09/+3.36）、覆盖子集 recall mean/worst、截断 0、
overlap 不变、调用数下降均通过；但实体 **raw** mean/worst（−2.92/−5.44）、
全量关系 F1 的归一化（−6.32/−16.33）与原始（−10.88/−19.28）两套、
覆盖子集 precision 未过，**所以不加切换 commit**；线上行为不变。
gate 的 `entity_caliber` 字段明确写：raw 是验收口径、normalized 是辅助口径，
两套都进 `checks`，任一超 0.5pt 即 fail。

若后续用更合适的评测口径重新达标，切换动作是单独一个 commit，且必须
**同步改两处默认值**，否则两处默认漂移、锁定测试会变红：

1. `src/moray/config.py::chunk_settings()` 的 `MORAY_CHUNK_MAX_UNITS`
   缺省值 `1500 → 3500`；
2. `src/moray/extraction/chunking.py::Chunker.__init__` 的 `max_units`
   默认值 `1500 → 3500`（或让 `Chunker` 默认读 `chunk_settings()`）；
3. 同步更新 `tests/test_chunk_budget.py::test_default_budget_and_overlap_are_unchanged`
   的期望值；overlap 保持 200 不动。

回滚直接 `git revert <该 commit>`；也可以不改代码、只在部署环境设
`MORAY_CHUNK_MAX_UNITS=3500`，删掉该 env 即回滚。

## 需要 owner 决策的下一步（本单不做，另开工单）

**B2（验收 #2 未达成）在本单如实保留为未达成，不通过扩金标或改口径把它
“做绿”。** 块预算 3500 在吞吐上显著更好（调用 −65%、输入 token −43.6%、
截断 0），但实体表面形态（37.79% 关系端点/EVENT 名带 `案件编号 ` 前缀）与
金标覆盖上的关系精度退化，故**不建议**切换默认；切换与否留给后续专门工单
处理前缀归一化与金标扩标后再评估。可选后续路径（二选一，另开工单）：

1. **扩展 gold 覆盖**：把卷四每案其余 4 名涉案人、每篇 18 句
   “法定代表人{PER}”等句面关系纳入答案；会改变 02 语料版本与既有基线。
   **不要在本单顺手改 02 金标。**
2. **正式改验收口径**：由 owner 明确预算 A/B 的关系判据改用更细的
   “金标覆盖子集/召回”粒度（例如按标注三元组邻域而非整句），并写进门槛。
   本单不擅自改。
3. **F5 实体归一化规则**：当前只在 04 A/B 评测侧 opt-in，报告把它定位为
   辅助口径、不能替代 raw 验收口径。请 owner 决定：接受该规则并同步工单
   验收口径、或在生产侧归一化实体名、或扩 02 金标后再判。决定落地前，
   原始口径仍是 −2.92/−5.44pt，不能算实体轴通过。

在任一落地前，生产保持 1500；本单不擅自改语料、匹配逻辑或默认值。

## 评审 r4 修复（B1 / S1 / S2 + nits）

- **B1（blocker）**：gate 实体轴改为 **raw + normalized 双口径**，4 条 check
  （mean/worst 各两条），任一超 0.5pt 即 fail；`raw_entity_macro_f1_*` 成为
  必填字段，`entity_caliber` 显式写验收/辅助口径。JSON 由
  `--from-json` 从既有逐轮数据重算，`gate.failures` 新增实体 raw 两条。
- **S1（should）**：`thresholds.relation_full_scope` 改为“全量口径 mean/worst
  也参与放行（gate.checks）”，与实现一致；`gate_recompute` 记录重算来源。
- **S2（should）**：`DEPLOYMENT.md` 与报告都改成“同步改 config + Chunker
  两处默认并更新锁定测试”，不再只说改一处。
- **N1** 删除无调用方的 `relation_covered_prf` 与未使用的 `_max`；
  **N2** first-occurrence gap 每关系只算一次、A/B 工具复用 `evaluate_case`
  返回的归一化副本，不再三次归一化；**N3** gate 校验两档预算不同、并在
  提供 rounds 时复算/校验 explicit worst delta；**N4** gate 夹具收敛到
  `tests/gate_fixtures.py` 一份；**N5** 过期“全量只列报”注释随夹具重构移除；
  **N6** 关系端点回退只对文档中实际出现的声明类型生效，并补反向用例；
  **N7** 真实文档 11/12 为短桩已在报告中披露（数据面，需 `--real-doc`
  传更长脱敏案卷，不阻塞本单）。
- **B2（blocker，owner 决策）**：本单不扩金标、不改验收口径，如实保留
  “未达成”，默认保持 1500；后续路径见上一节。

## 评审 r5 修复（S1 / S2 + N1–N4）

- **S1（should）**：关系轴补 **raw + normalized 双口径**。`raw_relation_f1_*`
  成为必填字段，gate 新增
  `relation_raw_full_f1_{mean,worst}_not_lower` 两条 check 与
  `relation_caliber`；本轮原始关系 F1 −10.88/−19.28pt，因此
  `gate.failures` 从 6 条增至 8 条。报告主表、CI 表、逐轮数据都补了原始行，
  不再只写归一化的 −6.32/−16.33。JSON 由 `--from-json` 重生成。
- **S2（should）**：overlap 验收表述改为“合成用例逐对断言；压力语料
  1500 8/25、3500 7/8，长句使 overlap_seed 为空”；JSON 新增
  `overlap_observed`（实际重叠对数/可重叠对数/比例 + 冒烟检查说明）。
  `gate.overlap_observed` 仍只检查 `>0`，但不再被当成逐对证明。
- **N1**：`metrics.py::apply_type_label_aliases` 的 docstring 改为“只对文档中
  实际出现过的声明类型回退”，与 r4 N6 的实现一致。
- **N2**：`real_doc_coverage` 改为每档用自己的预算计算
  （`1500.candidate_budget=1500`、`3500.candidate_budget=3500`），不再两档
  共用候选预算。
- **N3**：报告不再声称 JSON 含逐块文本明细；实际是逐轮/逐篇聚合 +
  单块最大 token/units 极值。
- **N4**：`_gate_input(result, budget, real, overlap_units)` 收敛 live /
  `--from-json` 两份手写 17 键 gate 输入，新增 raw 关系口径只改一处。

## 证据文件

| 文件 | 内容 |
|---|---|
| `docs/evidence/04-budget-ab.json` | 逐轮/逐篇聚合与单块极值、逐篇预测面（原始+归一化关系端点）、类型 P/R、双口径关系指标、overlap 实际/可重叠对数、残差明细、上下文余量、gate（实体与关系均 raw+normalized、caliber、gate_recompute）、CI |
| `docs/evidence/04-budget-ab.md` | 本报告 |
| `docs/evidence/04-r5-red-tests.txt` | 先红：评审 r5 新增测试在旧实现上的失败输出（9 failed） |
| `docs/evidence/04-r5-green-tests.txt` | 后绿：评审 r5 定向用例 |
| `docs/evidence/04-r5-full-suite.txt` | 全量 pytest（离线） |
| `docs/evidence/04-r5-fix-evidence.md` | 6 条评审发现逐条处理结果与证据 |
| `docs/evidence/04-r4-red-tests.txt` | 先红：评审 r4 新增/修改测试在旧实现上的失败输出 |
| `docs/evidence/04-r4-green-tests.txt` | 后绿：评审 r4 定向用例 |
| `docs/evidence/04-r4-full-suite.txt` | 全量 pytest（离线） |
| `docs/evidence/04-r4-fix-evidence.md` | 11 条评审发现逐条处理结果与证据 |
| `docs/evidence/04-r2-red-tests.txt` | 先红：评审 r2 新增/修改测试在旧实现上的失败输出（21 failed） |
| `docs/evidence/04-r2-green-tests.txt` | 后绿：评审 r2 定向用例（58 passed） |
| `docs/evidence/04-r2-full-suite.txt` | 全量 pytest（离线，514 passed / 66 skipped） |
| `docs/evidence/04-r2-fix-evidence.md` | 13 条评审发现逐条处理结果与证据 |
| `docs/evidence/04-r1-red-tests.txt` | 上一轮先红记录，保留 |
| `docs/evidence/04-r1-green-tests.txt` | 上一轮定向用例，保留 |
| `docs/evidence/04-r1-full-suite.txt` | 上一轮全量 pytest，保留 |
| `docs/evidence/04-r1-fix-evidence.md` | 上一轮 11 条发现处理结果，保留 |
| `docs/evidence/04-red-tests.txt` | 首版先红记录，保留 |
| `docs/evidence/04-green-tests.txt` | 首版定向用例，保留 |
| `docs/evidence/04-full-suite.txt` | 首版全量 pytest，保留 |
