# Moray 🐟

**企业级实体提取（NER/RE）+ 实体知识库系统**，基于 [AgentScope 2.0](https://docs.agentscope.io) 构建。

本仓库同时是一套 **AgentScope 2.0 渐进式教程**：课程总纲见 [`docs/ROADMAP.md`](docs/ROADMAP.md)，
每节课的可运行代码在 [`lessons/`](lessons/)。

## 架构愿景

文档输入 → **抽取 / 消歧 / 校验多智能体** → 结构化实体与关系 → **PostgreSQL + pgvector 知识库** → 查询问答 / API 服务。

- 可配置的实体 Schema（Pydantic 强类型）
- 实体对齐、共指消解、出处溯源（provenance）
- 语义检索 + 结构化查询
- 中间件审计、事件流可观测、人工确认（HITL）
- FastAPI 服务化与抽取质量评测

## 环境要求

- **Python ≥ 3.11**（AgentScope 2.x 硬性要求；本机用 `/usr/bin/python3.12`）
- 火山方舟 ARK API Key（DeepSeek/豆包模型，OpenAI 兼容网关）
- Docker（第 7 课起用于 PostgreSQL + pgvector）

## 快速开始

```bash
# 1. 用 Python 3.12 创建虚拟环境（系统默认 3.10 装不了 agentscope 2.x）
python3.12 -m venv .venv                       # 或 virtualenv -p python3.12 .venv
source .venv/bin/activate

# 2. 安装依赖
pip install -r requirements.txt

# 3. 配置密钥
cp .env.example .env                           # 选一家：LLM_PROVIDER=volcengine|deepseek|openai
                                               # 再填对应的 Key（默认 volcengine → ARK_API_KEY）

# 4. 运行第一课
python lessons/l01_hello_agent.py
```

## 基础设施：PostgreSQL + pgvector（L07 起）

```bash
docker run -d --name moray-pgvector \
  -e POSTGRES_USER=moray -e POSTGRES_PASSWORD=moray_dev_2026 \
  -e POSTGRES_DB=moray_kb -p 5433:5432 pgvector/pgvector:pg16
# 或：docker compose up -d（需 compose 插件）
```

本地中文嵌入模型 `BAAI/bge-small-zh-v1.5`（512 维，ONNX 免 torch）。
国内首次下载会自动走 `https://hf-mirror.com` 并关闭 HF xet 协议
（见 `src/moray/embedding/embedder.py`）。

## HTTP API 服务（L14 起）

```bash
python -m moray.api.server --port 8000          # 查询/HITL/看板/作业
python -m moray.api.server --port 8000 --llm    # 额外开启 POST /extract 实时抽取
# 交互式文档：http://127.0.0.1:8000/docs
```

## 模型配置

默认使用火山方舟 Coding Plan 网关（OpenAI 兼容，已实测 chat 与 function calling）：

```
ARK_BASE_URL=https://ark.cn-beijing.volces.com/api/coding/v3
ARK_CHAT_MODEL=deepseek-v4-flash-ga
```

> Embedding 模型 Coding Plan 不支持，项目第 7 课起改用本地 `BAAI/bge-small-zh-v1.5`（离线免费）。

## 目录

```
docs/ROADMAP.md      课程总纲与 AgentScope 2.0 知识地图（L00–L15 已完结）
docs/ARCHITECTURE.md 系统架构、数据流与模块地图
docs/DEPLOYMENT.md   部署/运维（DB、API、Docker、worker、CI、评测回归）
lessons/             学习区：每节课一个可独立运行脚本（注释即讲义）
src/moray/           生产区：由 lessons 沉淀的企业级代码（随课程推进创建）
tests/               测试（离线单测 + DB/MCP/API 集成，开关 RUN_DB_TESTS）
data/gold/           L15 黄金评测集（tagged 语料 + 关系 sidecar）
```

## 质量评测（L15）

```bash
python lessons/l15_evaluation.py   # 离线黄金集 P/R/F1：规则基线 vs 改进版
python -m pytest tests/ -q         # 132 离线单测；RUN_DB_TESTS=1 全量 175
```
