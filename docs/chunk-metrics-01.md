# 工单 01：分块级度量埋点与输出截断护栏 — 交付说明

**范围**：只加观测与信号，不改变抽取结果、提示词、切块默认参数与生产请求形态
（生产维持非流式；流式仅用于基线测量）。

## 验收状态（第 3 轮评审修复后）

| # | 状态 | 说明 |
|---|---|---|
| 1 每块一条记录 | 达成 | 假模型逐块断言字段无缺省；新增非流式也测得到的 `latency_s`。 |
| 2 文档级聚合 | **部分达成** | 调用数/返工率/截断块数完全可测。**生产默认非流式：仅 `wait_s`/`latency_s` 可测**，`prefill_s/decode_s` 协议层不存在，如实为 `None`/SQL NULL；分段耗时需用 `--stream` 测量（流式仅用于基线）。生产路径的分段采集要**另开单**（需改请求形态 + 黄金集回归 + A/B），本单不做。 |
| 3 截断计数 + 告警 | 达成 | 分块、短文档、**短文档永久失败**、永久失败块四条路径都进文档级度量；**同一次 reply 内的多次模型调用按调用累积**（B1）：截断后自纠的第 2 次 `stop` 不会覆盖第一次 `length`，每个 length 都计数并 WARNING。永久失败另落一行失败看板记录，token 成本与截断信号不再丢。 |
| 4 第 2 次尝试通过 | 达成 | attempts=2，两次 token 累计。 |
| 5 API 与批量字段一致 | 达成 | 离线 API 传输层 + 作业 handler 各跑一遍并比对字段集。 |
| 6 真实样本基线 | 达成 | 两份 JSON 均由 `scripts/chunk_metrics_baseline.py` 用真实凭证原样产出（`meta` 含 provider/chat_model/UTC 时间/git_sha），非手工 backfill；命令见下。 |
| 7 只走现有数据面 | 达成 | 复用 `pipeline_runs`/`Dashboard`，无新第三方运行时依赖。 |

## 新增/修改面

| 文件 | 作用 |
|---|---|
| `src/moray/extraction/chunk_metrics.py`（新） | `ChunkCallMetrics`（每块一条，核心字段无缺省）、`DocumentMetrics`（文档级聚合：chunk_calls/total_attempts/truncated_chunks/truncation_signals/rework_rate/wait/latency/prefill/decode）、`is_length_finish_reason`；`to_dict()` 按块序稳定排序（r1 nit5）；合并三处内联 `_sum` 为同一个函数（r3 nit7）；`attempt_finish_reasons`/`truncated_attempts` 的语义按"模型调用"而非"尝试"写清（r3 nit3） |
| `src/moray/extraction/extractor.py` | 每次尝试记录结束原因、墙钟与分段计时；同一次 reply 内**逐个模型调用**记录 finish_reason（B1）；每个 `length` 立即 WARNING（`extra={"event":"output_truncated","truncated":True,...}`）；成功/失败共用一份块级度量构造（r3 nit7），都带 `call_metrics`（`ExtractionError.metrics`） |
| `src/moray/extraction/pipeline.py` | 信号量排队计时（wait_s）；逐块组装 `DocumentMetrics`；失败语义不变（默认整篇抛错，异常上挂 `document_metrics`）；失败块已花 token 也累计进文档用量；`run(..., raise_on_chunk_error=False)` 仅供需要"失败也出部分结果"的调用方显式选择 |
| `src/moray/extraction/document.py` | `ExtractionOutput.call_metrics` / `document_metrics`（pydantic 嵌套 dataclass 转换） |
| `src/moray/extraction/provider.py` | `DynamicExtractorProvider` 的闭包**失败也记账**（S1）：`Extractor` 抛 `ExtractionError` 时把异常上已花的 token 与截断信号累加进 `usage_sink`/`metrics_sink`，再原样抛；短文档永久失败不再是成本黑洞 |
| `src/moray/orchestration/specialists.py` | `ReviewedExtraction.document_metrics`；分块失败仍整篇抛错（与本单前一致），文档级度量随异常返回；**短文档**在调用方显式传 `metrics_sink` 时才向 provider 要带度量的闭包（nit4，不隐式替换 extractor）；**短文档永久失败时把 `metrics_sink` 包成 1 块 `DocumentMetrics` 挂回异常**（S1）；短文档明细的 `chunk_id` 覆写成真实 `document_id`（nit5）；`document_id` 由调用方传入真实值（nit1） |
| `src/moray/config_instrumented.py`（新） | `Instrumented{Volcengine,DeepSeek,OpenAI}ChatModel`：捞回 agentscope 丢弃的供应商原生 `finish_reason`（含 length）；流式实测首 token 分段计时；`LastCallSignals` 按模型调用累积（B1）。**不改请求参数/解析内容**；覆盖签名原样转发 `*args/**kwargs`（OpenAI 基类按 3 位置参数调用）；已删除未使用的 `start` 参数与同步解析器死分支（nit8） |
| `src/moray/config.py` | `build_chat_model` 套埋点子类（行为同构，isinstance 仍成立） |
| `src/moray/observability/schema.sql`、`dashboard.py` | pipeline_runs 新增分块度量列（含 `chunk_latency_ms`）+ `source` 来源列（可重复 ALTER 平滑升级）+ chunk_metrics JSONB；`summary()` 聚合截断/返工/latency/prefill/decode，支持按 source 过滤，零测量时为 NULL；旧库的 0 默认值按 `>0` 排除，不再被当"已测"（r1 nit2），注释写明实测 0ms 语义（r3 nit9）；下钻 JSONB 复用 `DocumentMetrics.to_dict()["chunks"]` 的块序（r3 nit4）；失败行 token 复用 `summary()` 的同一份聚合（r3 nit7）；`from_failure` 从失败块汇总 token 成本（r2 S3） |
| `src/moray/api/{schemas,app}.py` | `/extract` 响应新增 `chunk_metrics`；实时路径同样落 pipeline_runs（`source='api'`）；永久失败也落一行失败观测；观测库写失败只 warning，不让抽取 500 |
| `src/moray/jobs/handlers.py` | 作业结果新增 `chunk_metrics`，与 API 消费同一 `DocumentMetrics.to_dict()`；永久失败同样落失败观测（`source='job'`） |
| `scripts/chunk_metrics_baseline.py`（新） | 真实样本基线，可复跑（`--repeat N/--stream/--out`） |

## 结束原因语义

- `stop` / `tool_calls`：正常（结构化工具调用结束时网关返回 `tool_calls`）。
- `length` / `max_tokens`：**输出撞长度上限被截断**——唯一显式计数并 WARNING 的信号；
  截断后若返工救回，最终 `truncated_chunks` 不增但 `truncation_signals` +1（撞过顶就要看见）。
- `unknown`：模型层没回传，绝不静默当成功。
- `cache_hit`：该块命中了【分块结果内容级缓存】（工单 05），**没有发起任何
  模型调用**，因此 `attempts=0`、`input/output/cached_input_tokens` 全 0，
  用量侧只记 `Usage.cache_hits=1`。它仍占一条块级记录，`chunk_calls` 继续等于
  块数——缓存命中率再高，分块维度的分母也不漂移；`summary.cache_hit_chunks`
  给出命中块数，看板可区分"省了钱"与"网关没回报结束原因"。

## 与内容级缓存（工单 05）的合并口径

两个特性在同一条分块路径上叠加，合并时守住三条：

1. **成本口径不变**：命中缓存不计任何真实 token（只 `cache_hits += 1`），
   `input_tokens/output_tokens/cached_input_tokens` 汇总只来自真实调用。
2. **度量口径不变**：每块一条 `ChunkCallMetrics` 不受缓存影响，命中块记
   `finish_reason=cache_hit`；文档级 `chunk_calls`、`n_chunks`、明细条数三者
   始终等于块数。
3. **失败语义不变**：缓存未命中时的 `ExtractionError` 与无缓存路径走同一条
   失败分支——已花 token 计入文档用量，度量进 `document_metrics`（或挂到异常
   上）；`raise_on_chunk_error=True` 仍整篇抛错。命中块没有异常可言，天然不参与。

字段口径（r3 nit3）：`attempt_finish_reasons` 是该块**每一次模型调用**的结束原因
（一次 attempt 内可能多次调用，如截断后自纠），末位才是 `finish_reason`；
`truncated_attempts` 是**被 length 截断的模型调用次数**，不是 attempt 数——
同一次 attempt 里截断两次就计 2，文档级 `truncation_signals` 仍按"出现过截断
信号的块数"计，不重复计调用。

## 分段计时诚实性

生产默认 **非流式**（本单不改请求形态）：网关一次性返回，首 token 时间点在协议里
不存在，`prefill_s/decode_s` 如实为 `null`；看板列存 SQL NULL，而不是 0；
`summary()` 在一条都没测到时也返回 `None`，并用 `prefill_measured_runs` /
`decode_measured_runs` 标明有多少行真正测到。`wait_s`（并发配额排队）始终可测。

**明确口径**：生产路径**只有 `wait_s` 与 `latency_s` 可测**；`prefill_s/decode_s`
（分段耗时）**必须用 `--stream` 测量**，属基线/诊断用途，不是生产默认行为。
基线 JSON 里非流式的 `prefill_s/decode_s` 必须是 `null`，**不许写 0**——0 会被
下游当成"极快"的实测值污染 P50/P99。

为回答"时间花在哪"，每块另有 **`latency_s`**：在 `_call_structured` 里包住
`agent.reply` 计墙钟，非流式也测得到；看板落 `chunk_latency_ms`，与
`prefill_ms/decode_ms` 分列，**不拿总延迟冒充分段耗时**。流式 `prefill_s` 的
计时起点是解析器首次迭代（响应头已到），不含网络 RTT/服务端排队，是 **下界**。
需要真实分段基线的用 `--stream` 实测（流式仅用于测量）。
本单**不加生产流式开关**：改请求形态是另一个变更，必须单独开单 + 黄金集回归 + A/B，
不能夹带在观测单里（见文末"另开单建议"）。

## 真实样本基线（2026-11-03，由脚本原样产出）

两份 JSON 顶层都有 `meta`（provider/chat_model/generated_at/git_sha），由
`scripts/chunk_metrics_baseline.py` 写入，**不是手工 backfill**；换模型或换
prompt 后先比 `meta` 再比数字。本轮用可用真实凭证（provider=`deepseek`，
chat_model=`deepseek-flash`）执行：

```bash
set -a && source <你的 .env> && set +a
python scripts/chunk_metrics_baseline.py --repeat-tokens 12 \
  --out data/gold/chunk_metrics_baseline.json
python scripts/chunk_metrics_baseline.py --repeat-tokens 12 --stream \
  --out data/gold/chunk_metrics_baseline_stream.json
```

非流式 `data/gold/chunk_metrics_baseline.json`：3 块，输出 token 均值
**1978.33/块**，截断 0、返工 0；墙钟 7.33s（并发 4）。**该文件的分段耗时为
`null`**（生产非流式不可测），但每块有真实的 `latency_s`（6.15/7.27/6.33s），
`summary.latency_s=19.75s`（并发调用的墙钟之和，会大于总墙钟）。

流式 `data/gold/chunk_metrics_baseline_stream.json`（实测分段）：

| 块 | prefill_s | decode_s | latency_s | out tokens |
|---|---|---|---|---|
| c0 | 0.521 | 5.233 | 6.09 | 1812 |
| c1 | 0.371 | 5.994 | 6.63 | 2023 |
| c2 | 1.440 | 26.920 | 29.00 | 9754 |

总 prefill 2.33s、decode 38.15s（占二者之和的 ~94%）——**时间几乎全在解码**，
这正是工单 03（输出瘦身）与 04（块预算）的数据依据。本轮流式的 c2 在同一个
`reply` 内发生了 2 次模型调用（`attempt_finish_reasons=["tool_calls","tool_calls"]`），
token 按调用累计；绝对值随模型生成长度抖动，对比时看比例与截断计数，
不看单次绝对值。

## 契约样例（不联网假模型，POST /extract，ingest=false，8 块含 1 返工 + 1 截断救回）

请求：`POST /extract {"text": "<8 句长文>", "document_id": "sample01"}` → `200`。
响应新增字段 `chunk_metrics`（**示意值**：不联网假模型注入的确定性脚本，
其中 prefill/decode 是假计时、不是真实网关测量）：

```json
"summary": {
  "document_id": "sample01", "n_chunks": 8, "chunk_calls": 8,
  "total_attempts": 10, "truncated_chunks": 0, "truncation_signals": 1,
  "rework_rate": 0.25, "reworked_chunks": 2, "cache_hit_chunks": 0,
  "wait_s": 1.2e-05, "latency_s": 3.49,
  "prefill_s": 1.12, "decode_s": 2.37,
  "input_tokens": 1073, "output_tokens": 399, "cached_input_tokens": 40
}
```

被截断又救回的块：`finish_reason="stop", attempts=2, truncated=false,
truncated_attempts=1, attempt_finish_reasons=["length","stop"]`。

截断当刻的告警日志（WARNING，带结构化字段；同一次 reply 内多次模型调用时
每次 length 都会单独打一条，`model_call=i/n` 标明是第几次调用）：

```
结构化输出被长度上限截断 event=output_truncated document_id=doc#c0003
domain=general attempt=1/2 model_call=1/1 in_tokens=99 out_tokens=200
finish_reason=length truncated=True
```

看板 `GET /metrics/dashboard` 同一次假模型运行的**示意值**：`chunk_calls=8,
total_attempts=10, truncated_chunks=0, truncation_signals=1, reworked_chunks=2,
chunk_rework_rate=0.25, chunk_latency_ms=3490`；`prefill_ms/decode_ms` 在真实
非流式运行里是 NULL，假模型样例里的数值只是字段示意。该行 `source='api'`；
批量作业行 `source='job'`，可按来源过滤。批量作业结果的 `chunk_metrics`
由同一份 `DocumentMetrics.to_dict()` 产出，字段集逐字一致
（离线 `tests/test_chunk_metrics_review_fixes.py` 对两个真实入口分别断言；
DB 侧 `tests/test_chunk_metrics_integration.py` 再验证落库）。

## 边界路径

- **短文档**（≤ 分块阈值）：仍走原抽取-审校闭环。API/作业入口**显式传**
  `metrics_sink=[]` 时，才向 provider 要带度量回传的闭包，把每轮
  `call_metrics` 合并成 1 块 `DocumentMetrics`；不传就用调用方自己的
  extractor，不隐式替换。首轮 `length` 返工救回时
  `truncation_signals=1`、`truncated_chunks=0`。明细的 `chunk_id` 覆写成
  调用方的真实 `document_id`（r3 nit5），不再是 `<domain>-dyn` 占位。
- **短文档永久失败**（第 3 轮 S1）：`Extractor` 三次尝试后抛
  `ExtractionError` 时，异常上已带本次真实的 token 与截断信号；provider 闭包
  会先把它们累加进 `usage_sink`/`metrics_sink` 再抛，`run_chunked_team`
  再把 `metrics_sink` 包成 1 块 `DocumentMetrics` 挂回异常。API/作业的失败
  看板行因此有 `input_tokens/output_tokens>0`、`truncation_signals>=1`、
  `chunk_calls=1`，不再是 token=0 的成本黑洞。
- **永久失败块**：整篇仍按原语义抛 `ExtractionError`（不吞成部分结果），但
  API 与批量 handler 会先用异常上的 `document_metrics` 落一行失败观测
  （`review_passed=false`、`truncated_chunks/truncation_signals` 原样保留，
  **`input_tokens/output_tokens/cached_input_tokens` 从失败块明细汇总**），
  再原样抛出。失败块已花 token 计入文档用量与看板成本行，成本不再被系统性低估。
- **异常未带度量**：`ExtractionError.metrics is None` 时 pipeline 补一条
  `finish_reason="unknown"` 记录，保证 `chunk_calls == 块数`（r1 nit6）。
- **进度回调**：`on_chunk` 恢复为"每块完成即回调"（不再等全部 gather 完）；
  永久失败块仍不回调，与改动前一致。

## 另开单建议：流式分段耗时采集（未在本单实施）

- **动机**：验收 #2 的"总 prefill/decode"在生产默认非流式下拿不到；本单只能
  用 `latency_s` 做诚实替代、用 `--stream` 出基线。要把生产看板的
  `prefill_ms/decode_ms` 填上真实分段，必须让生产请求走流式。
- **为什么不在本单**：改生产请求形态超出"只加观测"的边界，且可能影响延迟、
  计费与解析路径，必须独立评审。
- **需要跑的回归**：黄金集实体/关系 P/R/F1 不降；同一批真实样本的非流式 vs
  流式 A/B（总墙钟、token、截断计数、失败率）；提示缓存命中率不退化；
  现有 `wait_s/latency_s/prefill_s/decode_s` 列语义不变（NULL 才表示不可测）；
  三条入口（/extract、批量 handler、脚本）字段集仍逐字一致。
- **默认开关**：即便实施也应默认关闭、可回滚，并保留本单的非流式路径作为对照。

## 测试

- 离线：`ARK_API_KEY=dummy pytest -q`（CI 同款；MCP/DB/实时模型用开关隔离）。
  其中 `tests/test_chunk_metrics.py`、`tests/test_instrumented_model.py`、
  `tests/test_chunk_metrics_review_fixes.py`、`tests/test_run_recorder.py`、
  `tests/test_chunk_metrics_round3_fixes.py` 覆盖确定性验收与三轮评审修复
  （含 OpenAI 3 参数调用、None 不写 0、短文档/失败块信号、**短文档永久失败的
  token/截断信号（r3 S1）**、source、API/handler 同字段、同一次 reply 两次
  模型调用的截断回归、失败行 token 成本、`latency_s`、真实 document_id、
  `metrics_sink` 显式化、块序稳定、unknown 兜底、旧库 0 值排除）。
- 字段契约（r3 S2）：`tests/test_chunk_metrics_round3_fixes.py` 在**不依赖 DB**
  的情况下比对 `DocumentMetrics.to_dict()` 与 DB 套件的 `SUMMARY_KEYS`/
  `CHUNK_KEYS`——以后字段再漂移，CI 会直接红，不再被 `RUN_DB_TESTS` 的 skip
  掩盖（这类"跳过即绿"的假红/假绿是 r3 的评审重点）。
- DB/全量：`RUN_DB_TESTS=1 ARK_API_KEY=dummy pytest -q`（+ pgvector/MCP/API/
  作业/缓存集成；API 与批量两路径字段一致 + source 过滤 + NULL 分段耗时（
  `prefill/decode` 生产非流式为 NULL，**不是 0**）+ SQL 聚合）。
  01 r3 本轮在**新建的临时库**（`POSTGRES_DB=moray_r3_check`，不 TRUNCATE 共享
  开发库）验证过全绿；同一临时库上，修复前的
  `test_chunk_metrics_integration.py` 会红在 `SUMMARY_KEYS`/`CHUNK_KEYS` 缺
  `latency_s`，修复后该文件由红转绿，这就是 S2 的证据。01 r2 × 03 r2 × 05
  合并后的稳定分支也在此套件上复跑通过（缓存命中列 `cache_hit_chunks` /
  `cache_hit` 与回填信号并存）。
- **条数口径**：passed/skipped 的具体数字随 01/02/03/05 各工单合并持续演进，
  本文**不写死**；以**最近一次运行**的输出为准（文档只描述"跑什么、看什么"）。
- 无新增运行时第三方依赖（仅子类化 agentscope 已装包；看板仍走现有 PostgreSQL）。

