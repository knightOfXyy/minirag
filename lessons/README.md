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
