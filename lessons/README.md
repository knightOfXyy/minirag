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
