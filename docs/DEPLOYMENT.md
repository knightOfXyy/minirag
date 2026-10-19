# 部署与运维说明

Moray 的运行形态分三层，可独立扩缩：

| 组件 | 形态 | 是否需要 LLM |
|---|---|---|
| API 查询服务（L14） | `moray.api.server` 常驻 HTTP | 否（默认） |
| 批量抽取 worker（L11） | `BatchRunner` 独立进程/定时任务 | 是 |
| PostgreSQL + pgvector | 有状态存储（实体/向量/关系/作业/看板） | 否 |

## 1. 数据库

```bash
docker run -d --name moray-pgvector \
  -e POSTGRES_USER=moray -e POSTGRES_PASSWORD=moray_dev_2026 \
  -e POSTGRES_DB=moray_kb -p 5433:5432 pgvector/pgvector:pg16
```

连接配置走环境变量（见 `.env.example`）：`POSTGRES_HOST/PORT/USER/PASSWORD/DB`。
首次启动各 Store 的 `init_schema()` 会自动建表（含 ivfflat 向量索引）。

## 2. 本地运行 API

```bash
python -m moray.api.server --port 8000          # 查询/HITL/看板/作业
python -m moray.api.server --port 8000 --llm    # 额外开启 POST /extract
# 文档：http://127.0.0.1:8000/docs
```

## 3. Docker 镜像

```bash
docker build -t moray:latest .
docker run -d --name moray-api -p 8000:8000 \
  --env-file .env \
  -e POSTGRES_HOST=host.docker.internal \
  moray:latest
```

镜像默认不含 LLM 实时抽取（查询路径），避免慢模型拖垮 Web。
实时抽取请：批量走 worker，或显式覆盖 command 加 `--llm` 并注入 `ARK_API_KEY`。

> 嵌入模型 `BAAI/bge-small-zh-v1.5` 首次运行需从 HuggingFace 拉取（ONNX）。
> 镜像已默认 `HF_ENDPOINT=https://hf-mirror.com`、`HF_HUB_DISABLE_XET=1`；
> 离线环境请把模型缓存目录挂载进容器。

## 4. 批量 worker（L11）

worker 是独立进程，轮询 `extraction_jobs`，靠**作业幂等 + Ingestor 幂等**实现
至少一次投递下的断点续跑（`FOR UPDATE SKIP LOCKED` 抢占、陈旧作业回收）。
可用 cron / k8s CronJob 周期启动；多副本安全。

## 5. 可观测性（L13）

- 质量看板：`GET /metrics/dashboard`（通过率 / span 一致率 / 返工轮次 /
  消歧自动化率 / token 成本）。
- 调用链：`setup_tracing(mode="otlp", endpoint=...)` 导出 OTLP 到
  Jaeger/Tempo；不传则 memory/console。

## 6. 评测回归（L15）

改任何抽取逻辑后，先用离线黄金集回归（零成本、确定性）：

```bash
python lessons/l15_evaluation.py            # 规则基线对比，导出 data/gold/eval_report.json
python -m pytest tests/ -q                  # 离线单测（CI 同款）
RUN_DB_TESTS=1 python -m pytest tests/ -q   # 含 DB/MCP/API 集成测试
```

## 7. CI

`.github/workflows/ci.yml` 在 Python 3.11/3.12 上跑离线测试矩阵；
需要 PostgreSQL / 模型凭证的集成测试默认自动 skip。
