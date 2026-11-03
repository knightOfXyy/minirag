# 工单 04：分块预算 1500 → 3500 A/B 与上线判据（评审 r2 修复后，双口径 + 全量 F1 gate）

- 分支/worktree：`yzc/tmp-yzd-feat-relation-202610-04`
  （`/home/dell/orca/workspaces/minirag/tmp-yzd-feat-relation-202610-04`）
- 代码提交：`e78dd35`（评审 r2 修复：关系端点归一化、全量关系 F1 入 gate、
  归一化 opt-in、真实长文批次、口径/统计加固）；证据采集时 `git_dirty=false`
- 证据提交：本分支 `devflow(04): 评审 r2 A/B 证据（关系端点修正、全量 F1 gate；默认仍 1500）`
- 模型：`deepseek-flash`（provider `deepseek`，`api.deepseek.com`），temperature=0，
  **5 轮独立重复**（无 seed）；压力语料 3 篇 / 3 seed / 79,362 字；
  真实文档 **12 篇**（6 篇黄金集短样本 + 5 篇 samples + 1 篇 5,360-unit 长文
  `docs/ARCHITECTURE.md`，用于真正压 3500 档）
- 原始数据：`docs/evidence/04-budget-ab.json`（逐轮、逐篇、逐块明细 +
  **逐篇预测实体/关系面（原始端点 + 归一化端点）**，全部由本次运行生成，无手改派生字段）

## 结论（先看这里）

1. **配置面达成**：现有配置项 `MORAY_CHUNK_MAX_UNITS=3500` 可设；`overlap_units`
   默认 200 与行为不变。**默认值仍为 1500，本单没有切换。**
2. **实体轴：辅助口径改善，但不写成“验收通过”**。类型前缀标签
   （`案件编号 A2024-942` → `A2024-942`）归一化后，3500 档实体 macro-F1
   **+6.09pt（mean）/ +3.36pt（逐轮对齐 worst-of-N）**；但原始未归一化口径仍是
   **−2.92pt（mean）/ −4.08pt（各档自身最差值相减；逐轮对齐 worst −5.44pt）**，
   不满足验收 2 的 0.5pt 门槛。归一化只是辅助口径，生产仍会吐带前缀的名字；
   是否接受该规则/扩金标由 owner 决定（见文末）。
3. **关系轴仍不放行，且 gate 已把全量 F1 加回**。修掉关系端点未归一化的评测
   bug 后（F1），3500 的 2,858 条预测关系里 **1,080 条（37.79%）**曾因头/尾带
   `案件编号 ` 前缀永远匹配不上 gold；修正后覆盖子集 recall 由“假失败”翻为
   **+8.30pt（mean）/ +2.63pt（worst）**，已通过“不下降”。但 precision 仍
   **−12.28pt / −28.66pt**，全量关系 F1 **−6.32pt / −16.33pt**；评审 r2 F2
   把全量 F1 加回 `checks` 后，gate 仍 `passed=false`（4 条 failure）。
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
| 实体 macro-F1（原始未归一化） | 0.8974 / 0.8874 | 0.8682 / 0.8466 | **−2.92pt / −4.08pt**（own-worst；逐轮对齐 −5.44pt） |
| 实体 strict-F1（mean） | 0.9624 | 0.9570 | −0.54pt |
| 关系 F1（全量口径，macro） | 0.7042 / 0.5876 | 0.6410 / 0.6151 | **−6.32pt / −16.33pt** |
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
| 实体 macro-F1（原始） | −2.92pt | —（只作并列列报） | −4.08pt（own-worst；对齐 −5.44） |
| 关系 F1（全量） | −6.32pt | [−17.38, +4.75] | −16.33pt |
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
  解析到的实体类型剥，类型查不到时按前缀表全表回退。A/B 工具落盘原始端点与
  归一化端点，1080 条错配可逐条审计。
- **F4**：归一化改为 `evaluate_case(..., normalize_type_labels=False)` 的
  **opt-in**；02/03 的入口（`stress/runner.py`、`tools/backfill_stress_ab.py`）
  保持默认 False，历史基线口径不被静默改写。只有 04 的
  `tools/chunk_budget_ab.py` 传 True。

**F5：这是辅助口径，不写成“验收通过”。** 原始口径仍是 −2.92pt / −4.08pt
（不满足 0.5pt），归一化规则只影响评测、不影响线上输出（生产仍会吐
`案件编号 A2024-942`）。是否接受该规则、是否在生产侧归一化，或扩 02 金标后
再判，仍待 owner 决定（见文末）。

两口径并列（5 轮）：

| 口径 | 1500 mean / worst | 3500 mean / worst | 变化 mean / worst |
|---|---:|---:|---:|
| 归一化后（辅助口径） | 0.8974 / 0.8874 | 0.9583 / 0.9346 | **+6.09pt / +3.36pt** |
| 原始未归一化（验收口径） | 0.8974 / 0.8874 | 0.8682 / 0.8466 | **−2.92pt / −4.08pt**（逐轮对齐 −5.44） |

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
  带 `案件编号 ` 前缀（1500 档 0 条）；旧实现只归一化实体、不归一化关系，
  这些关系永远匹配不上 gold，覆盖子集 recall 的 −0.94/−2.34pt 是**假失败**。
  修正端点后 recall 变为 **+8.30pt / +2.63pt**，五轮全为正。
- **F2 放行 gate**：工单验收 2 要求“关系 F1 不下降”，所以 gate 的 `checks`
  现在同时包含 **全量关系 F1 mean/worst 不下降** 与覆盖子集 recall/precision；
  全量口径不再只是“列报”。本轮全量 F1 −6.32/−16.33pt，因此即便覆盖子集
  recall 已通过，gate 仍 `passed=false`。

| 口径 | 1500 | 3500 | 变化 mean / worst |
|---|---:|---:|---:|
| 全量关系 F1（macro） | 0.7042 / 0.5876 | 0.6410 / 0.6151 | **−6.32pt / −16.33pt** |
| 覆盖子集 recall（micro） | 0.7731 / 0.6842 | 0.8561 / 0.8275 | **+8.30pt / +2.63pt** |
| 覆盖子集 precision（micro） | 0.6353 / 0.4979 | 0.5125 / 0.4896 | **−12.28pt / −28.66pt** |
| 覆盖子集 TP / FP / FN（合计） | 1322 / 812 / 388 | 1464 / 1393 / 246 | TP +142、FP +581、FN −142 |
| 全量 TP / FP / FN（合计） | 1322 / 837 / 388 | 1464 / 1394 / 246 | TP +142、FP +557、FN −142 |

**F7 口径说明**：上表前两行是 macro，覆盖子集两行是 micro。按逐篇 macro
重算覆盖子集，recall 仍是 +8.30pt（worst +2.63pt），precision 为 −16.13pt
（worst −28.67pt）——方向与 micro 一致，precision 更差，结论不变。
gate 返回的 `caliber` 字段把三种口径写死，报告不再把两列当同一口径比较。

结论：**F1 修复后 recall 不再是问题，precision / 全量 F1 的下降才是真实
金标缺口**。3500 把更多真实存在但 02 未标注的句面关系抽出来（全量 FP
837 → 1394），TP 反而从 1322 升到 1464、FN 从 388 降到 246；F1 下降完全由
precision 驱动。本单不改 02 金标，默认保持 1500。

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
- **实际行为**：相邻块“后块起点早于前块终点”的重叠对数，两档最小值分别为
  **8（1500）和 7（3500）**，都 >0；gate 的 `overlap_observed` 也检查这一点。
- 证据字段：JSON `overlap_frozen_baseline` + `gate.checks.overlap_*`。
- 配置级测试：默认 1500/200、设置 3500 后 overlap 仍 200，并逐对断言
  相邻块重叠长度 **0 < overlap ≤ 200**（评审 N1 加固）。

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
| 1 | 3500 可经现有配置项设置；overlap 默认值与行为不变（配置级测试） | **达成**：默认 1500/200；3500 可设且 overlap 仍 200；相邻块逐对重叠 0<x≤200 |
| 2 | 1500/3500 同口径：实体 macro-F1 跌 ≤0.5pt、关系 F1 不降、附类型 P/R | **未通过（不切换）**：实体原始口径 −2.92/−4.08（未过）；归一化后 +6.09/+3.36 只作辅助口径；全量关系 F1 −6.32/−16.33、覆盖子集 precision −12.28/−28.66（不过）；覆盖子集 recall +8.30/+2.63（过） |
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
relation_full_f1_mean_not_lower            # -6.32pt（F2 新增）
relation_full_f1_worst_not_lower           # -16.33pt（F2 新增）
relation_covered_precision_mean_within_0_5pt   # -12.28pt
relation_covered_precision_worst_within_0_5pt  # -28.66pt
```

实体 mean/worst（归一化）、覆盖子集 recall mean/worst、截断 0、overlap 不变、
调用数下降均通过；但全量关系 F1 与 precision 未过，**所以不加切换 commit**；
线上行为不变。

若后续用更合适的评测口径重新达标，切换动作是单独一个 commit、只改
`src/moray/config.py::chunk_settings()` 里 `MORAY_CHUNK_MAX_UNITS` 的默认值
`1500 → 3500`（overlap 不动），回滚直接 `git revert <该 commit>`；也可以不改
代码、只在部署环境设 `MORAY_CHUNK_MAX_UNITS=3500`，删掉该 env 即回滚。

## 需要 owner 决策的下一步（不在本单改）

F1 修复后，“覆盖子集 recall 下降”已证明是评测 bug，不再是阻塞项；要让 3500
真正可切换，剩下的真实问题是 02 金标的刻意子集导致 precision/全量 F1 下降：

1. **扩展 gold 覆盖**（推荐）：把卷四每案其余 4 名涉案人、每篇 18 句
   “法定代表人{PER}”等句面关系纳入答案；会改变 02 语料版本与既有基线，
   属另一个工单。**不要在本单顺手改 02 金标。**
2. 或由 owner 明确预算 A/B 的关系判据改用更细的“金标覆盖子集/召回”粒度
   （例如按标注三元组邻域而非整句），并写进门槛；本单不擅自改。
3. **F5 实体归一化规则**：当前只在 04 A/B 评测侧 opt-in，报告把它定位为
   辅助口径、不写“验收通过”。请 owner 决定：接受该规则并同步工单验收口径、
   或在生产侧归一化实体名、或扩 02 金标后再判。决定落地前，原始口径仍是
   −2.92/−4.08pt，不能算实体轴通过。

在这三项任一落地前，生产保持 1500；本单不擅自改语料、匹配逻辑或默认值。

## 证据文件

| 文件 | 内容 |
|---|---|
| `docs/evidence/04-budget-ab.json` | 逐轮/逐篇/逐块原始数据、逐篇预测面（原始+归一化关系端点）、类型 P/R、双口径关系指标、残差明细、上下文余量、gate（含全量 F1 checks 与 caliber）、CI |
| `docs/evidence/04-budget-ab.md` | 本报告 |
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
