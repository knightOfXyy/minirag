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

## 1b. Docker Compose 一键部署（含多 worker）

`docker-compose.yml` 编排三个服务：`postgres`、`api`（查询，无 LLM）、
`worker`（可多副本，跑 LLM）。多副本靠 DB 租约不重不漏（见第 4 节）。

```bash
cp .env.example .env          # 填好 ARK_API_KEY；可调 WORKER_REPLICAS 等
docker compose up -d --build  # 默认起 1 api + WORKER_REPLICAS(默认2) 个 worker
docker compose up -d --scale worker=4   # 临时扩到 4 个 worker
docker compose logs -f worker           # 看各副本分发情况
docker compose down
```

compose 部署的几个关键点（yml 里有注释）：

- **容器内连库地址变了**：服务间走 compose 网络，`POSTGRES_HOST=postgres`、
  `POSTGRES_PORT=5432`（不是宿主的 `localhost:5433`）。yml 已显式注入覆盖
  `.env`，所以同一份 `.env` 宿主/容器通用。
- **嵌入模型缓存共享**：api 和 worker 都挂命名卷 `hf_cache:/root/.cache`，
  bge 模型只下载一次。
- **作业文档要容器可见**：worker 按 `extraction_jobs.document_ref` 读文件，
  故挂了 `./data:/app/data:ro`。若文档在别的路径，需额外挂载或改用对象存储。
- **多副本不能写死 `container_name`**：worker 服务不设容器名（写死会重名），
  `worker_id` 自动用 `主机名-pid-uuid` 生成，天然唯一、便于排查。
- **优雅停机**：worker `stop_grace_period: 30s`，SIGTERM 后在途任务
  `release` 回 pending，被其他副本/重启后接走。
- **离线验证**：`docker compose --profile offline up worker-offline`
  用确定性假抽取跑一轮，不需要 ARK key。

> 若你之前用第 1 节的 `docker run` 起过名为 `moray-pgvector`、占用 5433 的
> 容器，compose 的 postgres 会和它重名/撞端口。二选一：先
> `docker rm -f moray-pgvector` 再 compose up，或把 compose 里 postgres 的
> `container_name` 与宿主端口映射去掉（数据卷独立，互不影响）。

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

## 4. 批量 worker（L11，多 worker 租约机制）

worker 是独立进程，消费 `extraction_jobs`，可用 cron / k8s CronJob 周期启动，
也可 `--poll` 常驻。可以**同时起多个进程/副本**水平扩展，跨进程互斥与崩溃恢复
由数据库**租约（lease）**保证：

- **认领**：`claim_next` 用 `UPDATE ... FOR UPDATE SKIP LOCKED` 原子取走
  `pending` 新任务或**租约已过期**的僵死任务，并写 `locked_by / locked_at /
  expires_at`；两个 worker 绝不会领到同一行。
- **心跳**：处理期间后台协程按 `lease/3` 周期续租。只要活在跑、心跳不断，
  别的 worker 绝不抢；若心跳失败（说明已被接管），该 worker 放弃写该任务状态。
- **接管**：worker 崩溃/卡死 → 心跳停止 → 租约过期 → 另一 worker 接管重跑。
  因 Ingestor 幂等，重跑只重复劳动、不产生脏数据。
- **优雅停机**：收到 SIGINT/SIGTERM 后不再认领新任务，在途任务做完或
  `release` 回 pending（不必等租约超时）。
- **进程内并发**：`--concurrency N` 让单进程并行处理 N 个任务（贴合 LLM 的
  RPM/TPM 配额）；跨进程互斥仍由 DB 租约保证，两者正交。

```bash
# 真实 LLM 抽取（默认处理一轮后退出，适合 cron / k8s CronJob，多副本安全）
python -m moray.jobs.worker
python -m moray.jobs.worker --worker-id w1     # 多副本建议给稳定 id 便于排查

# 常驻轮询 + 单进程并行 3 个 + 10 分钟租约（适合长文档/慢推理模型）
python -m moray.jobs.worker --poll --poll-interval 10 \
    --concurrency 3 --lease-seconds 600

# 离线教学：确定性假抽取，零 token 演示多 worker 分发
python -m moray.jobs.worker --offline --once
```

> 租约时长要大于单任务最坏耗时：推理模型 + 长文档建议 `--lease-seconds 600`
> 以上；心跳自动按租约 1/3 续租，正常情况下活多久都不会被判死。

## 4b. 动态领域配置（domain_configs 配置中心，热更）

要"不改代码、不重启"地定义抽取哪些实体类型、每类抽哪些属性（必填/类型/枚举），
把领域包写进 `domain_configs` 表即可。API 与 worker 按 `domain_code` 经
`DbDomainResolver` 取最新配置（按 `updated_at` 版本缓存，改表即生效）。

```bash
# 首次把内置 general/finance 种入表（已存在则跳过）
PYTHONPATH=src python -m moray.config_center.cli seed
PYTHONPATH=src python -m moray.config_center.cli list
PYTHONPATH=src python -m moray.config_center.cli show finance
PYTHONPATH=src python -m moray.config_center.cli import my_domain.json
PYTHONPATH=src python -m moray.config_center.cli disable finance
```

导入 JSON 形态（与 `DomainSpec` 对应）：

```json
{
  "code": "recruit", "name": "招聘",
  "relation_predicates": ["要求掌握"],
  "types": [{"code": "JOB", "label": "职位", "guide": "招聘岗位",
    "attributes": [
      {"key": "title", "required": true, "type": "string"},
      {"key": "salary_min", "type": "integer"},
      {"key": "seniority", "type": "string", "enum": ["初级", "中级", "高级"]}
    ]}]
}
```

- 规格既进系统提示词/动态 JSON Schema（引导模型），也在 `validate_against_domain`
  里做强校验（必填/类型/枚举），不合规会触发 Extractor 的纠错重试。
- `--llm` 的 API 与真实模式 worker 自动启用该机制；未声明的属性键放行（保留开放发现）。
- 离线演示：`python lessons/dynamic_domain_config.py`。

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
