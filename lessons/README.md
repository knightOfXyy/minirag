# lessons/ —— 学习区

每节课一个**可独立运行**的脚本，注释就是讲义。运行方式：

```bash
# 确保已用 Python 3.12 建好虚拟环境（见 README.md）
.venv/bin/python lessons/l01_hello_agent.py
```

| 课次 | 文件 | 学到的 AgentScope 2.0 概念 |
|---|---|---|
| L01 | `l01_hello_agent.py` | `Agent`、`reply`、`reply_stream`、Event 事件流、Credential/Model |
| L02 | `l02_tool_toolkit.py` | `FunctionTool`、`Toolkit`、ReAct 循环事件、权限与 HITL（确认/挂起/恢复） |
| L03 | `l03_structured_output.py` | Pydantic 结构化输出、`structured_schema`、Schema 设计、出处溯源、校验失败处理 |
| L04(上) | `l04_domain_schema.py` | 动态 Schema：领域配置目录、`create_model` 动态枚举、稳定内核 + `attributes`、热插拔 |
| L04(下) | `l04b_extraction_service.py` | 抽取服务 `Extractor`、校验失败带错重试、span 质量探针、token 计量、JSON 落盘 |
| L05 | `l05_chunking.py` | 分块器、块内→全局偏移换算、map-reduce 分块抽取、跨块朴素归并、规则法金标准（支持 `OFFLINE=1`） |
| L06 | `l06_middleware.py` | Middleware 洋葱模型、审计/成本/质量三中间件、可解释质量分（支持 `OFFLINE=1`） |
| L07 | `l07_pgvector.py` | 本地 bge 嵌入、余弦相似度、pgvector 向量入库/最近邻检索、别名精确+向量双通道 |
| L08 | `l08_knowledge_base.py` | relations/evidence 表、Ingestor 入库器（幂等）、结构化+语义联合检索、实体知识卡片（离线默认，`--live` 真实抽取） |
| L09 | `l09_disambiguation_hitl.py` | 实体消歧三档（自动合并/人工/新建）、规则与 LLM Judge、母子公司防护、事务合并、审核队列 HITL（`--llm` 用消歧 Agent） |
| L10 | `l10_multi_agent.py` | 抽取-审校-返工质量闭环、可注入角色函数、规则审校员；AgentScope `TeamPipeline`（leader 用 TeamAssign 派活、成员并发隔离）、`GoalPipeline`（`--live`） |
| L11 | `l11_batch_resume.py` | 持久化作业表 extraction_jobs、JobStore（幂等创建/原子认领 `FOR UPDATE SKIP LOCKED`/状态机/孤儿回收）、BatchRunner 批量与断点续跑、毒丸重试上限、jsonb codec |
| L12 | `l12_mcp.py` | FastMCP server（locate/card/similar/relations 4 工具）、stdio/HTTP、AgentScope `MCPClient` 接入、Agent 自主 ReAct 调用 MCP 工具（默认进程内，`--stdio`/`--agent`） |
| L13 | `l13_observability.py` | OTEL `TracingMiddleware`（agent/model span，memory/console/otlp）、`pipeline_runs` 质量看板（通过率/span 一致率/返工/消歧自动化率/成本聚合）、handler 接线（`--console`） |
| L14 | `l14_api.py` | FastAPI 服务化：`/extract`、作业、实体定位/卡片/相似、关系、HITL 审核、看板、健康检查；依赖注入 + lifespan（默认进程内 httpx，`--http` 真实服务） |
| L15 | `l15_evaluation.py` | 评测收官：黄金集（tagged 语料 + 关系 sidecar）、严格/宽松实体匹配与关系三元组 P/R/F1、宏/微平均、baseline→improved 可复现对比（`--live` 评 LLM），CI/Dockerfile/部署文档 |
| 进阶 | `multiworker_demo.py` | 多 worker 机制：租约（locked_by/locked_at/expires_at）、心跳续租、过期接管、优雅停机、进程内并发；生产入口 `python -m moray.jobs.worker` |
| 进阶 | `dynamic_domain_config.py` | 动态领域配置（PostgreSQL 配置中心）：`domain_configs` 表热更实体类型/属性规格、按版本缓存解析、动态结构化模型、必填/类型/枚举属性校验；CLI `moray.config_center.cli` |
| 进阶 | `gen_stress_gold.py` | 确定性压力案卷黄金语料：固定种子生成超长合成案卷（正文 >5× 生产块预算）+ 带类型实体清单/关系标准答案，正文未出现的实体与头尾不可定位的关系自动剔除，产物落 `data/gold/stress/`（离线回归资产） |
| 进阶 | `eval_stress_gold.py` | 压力语料分块 A/B：确定性规则抽取路径按多组 (块预算/重叠) 复跑同一语料，输出实体 strict/relaxed + 按类型、关系三元组的 P/R/F1 并落 `eval_grid_report.json`（零 LLM 请求） |
