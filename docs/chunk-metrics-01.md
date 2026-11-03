# 工单 01：分块级度量埋点与输出截断护栏 — 交付说明

**范围**：只加观测与信号，不改变抽取结果、提示词、切块默认参数与生产请求形态。

## 新增/修改面

| 文件 | 作用 |
|---|---|
| `src/moray/extraction/chunk_metrics.py`（新） | `ChunkCallMetrics`（每块一条，核心字段无缺省）、`DocumentMetrics`（文档级聚合：chunk_calls/total_attempts/truncated_chunks/truncation_signals/rework_rate/wait/prefill/decode）、`is_length_finish_reason` |
| `src/moray/extraction/extractor.py` | 每次尝试记录结束原因与分段计时；`finish_reason=length` 立即 WARNING（`extra={"event":"output_truncated","truncated":True,...}`）；成功/失败都带 `call_metrics`（`ExtractionError.metrics`） |
| `src/moray/extraction/pipeline.py` | 信号量排队计时（wait_s）；逐块组装 `DocumentMetrics`；失败语义不变（默认整篇抛错，异常上挂 `document_metrics`）；失败块已花 token 也累计进文档用量；`run(..., raise_on_chunk_error=False)` 仅供需要"失败也出部分结果"的调用方显式选择 |
| `src/moray/extraction/document.py` | `ExtractionOutput.call_metrics` / `document_metrics`（pydantic 嵌套 dataclass 转换） |
| `src/moray/orchestration/specialists.py` | `ReviewedExtraction.document_metrics`；分块失败仍整篇抛错（与本单前一致），文档级度量随异常返回 |
| `src/moray/config_instrumented.py`（新） | `Instrumented{Volcengine,DeepSeek,OpenAI}ChatModel`：捞回 agentscope 丢弃的供应商原生 `finish_reason`（含 length）；流式实测首 token 分段计时。**不改请求参数/解析内容** |
| `src/moray/config.py` | `build_chat_model` 套埋点子类（行为同构，isinstance 仍成立） |
| `src/moray/observability/schema.sql`、`dashboard.py` | pipeline_runs 新增 11 列（可重复 ALTER 平滑升级）+ chunk_metrics JSONB；`summary()` 聚合截断/返工/prefill/decode |
| `src/moray/api/{schemas,app}.py` | `/extract` 响应新增 `chunk_metrics`；实时路径同样落 pipeline_runs（此前只有批量落） |
| `src/moray/jobs/handlers.py` | 作业结果新增 `chunk_metrics`，与 API 消费同一 `DocumentMetrics.to_dict()` |
| `scripts/chunk_metrics_baseline.py`（新） | 真实样本基线，可复跑（`--repeat N/--stream/--out`） |

## 结束原因语义

- `stop` / `tool_calls`：正常（结构化工具调用结束时网关返回 `tool_calls`）。
- `length` / `max_tokens`：**输出撞长度上限被截断**——唯一显式计数并 WARNING 的信号；
  截断后若返工救回，最终 `truncated_chunks` 不增但 `truncation_signals` +1（撞过顶就要看见）。
- `unknown`：模型层没回传，绝不静默当成功。

## 分段计时诚实性

生产默认 **非流式**（本单不改请求形态）：网关一次性返回，首 token 时间点在协议里
不存在，`prefill_s/decode_s` 如实为 `null`，不用总延迟冒充；`wait_s`（并发配额
排队）始终可测。需要分段耗时的基线用 `--stream` 实测（流式仅用于测量，是否切
生产留给后续工单，需黄金集回归）。

## 真实样本基线（2026-11-03，deepseek-v4-flash-ga，news_long_002×12，1500 预算，3 块）

非流式 `data/gold/chunk_metrics_baseline.json`：输出 token 均值 **3212/块**，
截断 0、返工 0；墙钟 13.9s（并发 4）。

流式 `data/gold/chunk_metrics_baseline_stream.json`（实测分段）：

| 块 | prefill_s | decode_s | out tokens |
|---|---|---|---|
| c0 | 0.626 | 4.701 | 1622 |
| c1 | 0.679 | 4.223 | 1454 |
| c2 | 0.745 | 17.382 | 6316 |

总 prefill 2.05s（占 ~7%）、decode 26.31s（占 ~93%）——**时间几乎全在解码**，
这正是工单 03（输出瘦身）与 04（块预算）的数据依据。

**复跑校验（2026-11-03 第二次独立运行，同一命令/同一模型）**：非流式 3 块，
输出均值 2778.67 token/块；流式 prefill 合计 1.53s、decode 合计 22.86s
（6% / 94%），截断 0、返工 0。两轮结论一致（时间 93–94% 在 decode），
绝对值随模型生成长度抖动，对比时看比例与截断计数，不看单次绝对值。复跑：

```bash
set -a && source <你的 .env> && set +a
python scripts/chunk_metrics_baseline.py --repeat-tokens 12          # 非流式
python scripts/chunk_metrics_baseline.py --repeat-tokens 12 --stream # 分段实测
```

## 契约样例（不联网假模型，POST /extract，ingest=false，8 块含 1 返工 + 1 截断救回）

请求：`POST /extract {"text": "<8 句长文>", "document_id": "sample01"}` → `200`。
响应新增字段 `chunk_metrics`（实测输出，2026-11-03）：

```json
"summary": {
  "document_id": "chunked", "n_chunks": 8, "chunk_calls": 8,
  "total_attempts": 10, "truncated_chunks": 0, "truncation_signals": 1,
  "rework_rate": 0.25, "reworked_chunks": 2,
  "wait_s": 1.2e-05, "prefill_s": 1.12, "decode_s": 2.37,
  "input_tokens": 1073, "output_tokens": 399, "cached_input_tokens": 40
}
```

被截断又救回的块：`finish_reason="stop", attempts=2, truncated=false,
truncated_attempts=1, attempt_finish_reasons=["length","stop"]`。

截断当刻的告警日志（WARNING，带结构化字段）：

```
结构化输出被长度上限截断 event=output_truncated document_id=doc#c0003
domain=general attempt=1/2 in_tokens=99 out_tokens=200 finish_reason=length
truncated=True
```

看板 `GET /metrics/dashboard` 同次运行实测：`chunk_calls=8, total_attempts=10,
truncated_chunks=0, truncation_signals=1, reworked_chunks=2,
chunk_rework_rate=0.25, prefill_ms=1120, decode_ms=2370`。批量作业结果的
`chunk_metrics` 由同一份 `DocumentMetrics.to_dict()` 产出，字段集逐字一致
（`tests/test_chunk_metrics_integration.py` 对两条路径分别断言）。

## 测试

- 离线：`ARK_API_KEY=dummy pytest tests/test_chunk_metrics.py
  tests/test_instrumented_model.py`（18 条，覆盖全部确定性验收）。
- DB：`RUN_DB_TESTS=1 pytest tests/test_chunk_metrics_integration.py
  tests/test_dashboard_integration.py`（API 与批量两路径字段一致 + SQL 聚合）。
- 全量：`RUN_DB_TESTS=1 ARK_API_KEY=dummy pytest -q` → **163 passed**。
- 无新增运行时第三方依赖（仅子类化 agentscope 已装包；看板仍走现有 PostgreSQL）。
