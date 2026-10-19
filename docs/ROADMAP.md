# Moray · 实体提取与实体知识库 —— AgentScope 2.0 实战学习路线

> 目标：**做完一个企业级项目，同时学透 AgentScope 2.0。**
> 每节课都包含：① 要学的 AgentScope 概念 → ② 可运行代码 → ③ 动手任务 → ④ 面试/实战考点。

## 一、最终要交付的系统（长什么样）

```
                        ┌─────────────────────────────────────────────┐
  文档/文本输入 ──────▶  │           Entity Extraction Agents          │
 (新闻/合同/报告/JSON)   │                                             │
                        │  预处理 → 抽取Agent → 消歧Agent → 校验Agent   │
                        └───────────────┬─────────────────────────────┘
                                        │  结构化实体 + 关系 (Pydantic)
                                        ▼
              ┌──────────────────────────────────────────────┐
              │            Entity Knowledge Base              │
              │  PostgreSQL (实体/关系/出处) + pgvector (语义)  │
              │  实体对齐/合并 · 共指消解 · 版本与溯源           │
              └───────────────┬──────────────────────────────┘
                              │
              ┌───────────────┴────────────────────────────────┐
              │  查询/问答 Agent（带知识库工具）  +  FastAPI 服务  │
              └────────────────────────────────────────────────┘
```

核心能力：**可配置的实体 Schema、可溯源（provenance）、实体去重合并、语义检索、多智能体协作、可观测可评测。**

## 二、AgentScope 2.0 知识地图（我们会在对应课里全部覆盖）

| AgentScope 概念 | 2.0 的样子 | 在哪一课 |
|---|---|---|
| `Agent`（统一 ReAct 内核） | `reply()` / `reply_stream()` | L01 |
| Event System（事件流） | `async for event`，替代 1.x 的 print | L01 |
| Model & Credential | `VolcengineChatModel` + `Credential` | L01 |
| Tool / Toolkit / FunctionTool | Python 函数即工具 | L02 |
| Structured Output | Pydantic 模型 → 强类型结果 | L03（实体抽取核心）|
| Msg 体系 | `UserMsg` / `AssistantMsg` / Block | L01/L03 |
| Memory | 短期记忆、记忆压缩 | L05 |
| Middleware（取代 Hook） | 洋葱模型，日志/追踪/鉴权 | L06 |
| Embedding & 检索 | 向量嵌入 + pgvector（自研 RAG 层）| L07/L08 |
| Permission & HITL | 工具权限、人工确认 | L09 |
| Pipeline | `GoalPipeline` / `TeamPipeline` | L10 |
| State / Session | 多租户会话、状态持久化 | L11 |
| MCP 集成 | 把外部能力挂成工具 | L12 |
| Tracing / Studio | OpenTelemetry 可观测 | L13 |
| 服务化 | FastAPI 封装、评测体系 | L14/L15 |

## 三、课程进度

- [x] **L00 环境与认知**：2.0 vs 1.x、项目骨架、模型链路打通
- [x] **L01 第一个 Agent**：`Agent.reply` 与 `reply_stream` 事件流
- [x] **L02 工具与 Toolkit**：知识库读写工具、ReAct 循环、权限与 HITL
- [x] **L03 结构化输出（项目地基）**：Pydantic `Entity`/`Relation`/出处 Schema、受控枚举、溯源、校验
- [x] **L04 抽取流水线 v1**：领域动态 Schema 热插拔；`Extractor` 抽取服务（校验重试/span 质检/token 计量）、真实文档端到端、JSON 落盘
- [x] **L05 记忆与长文档**：`Chunker`（token预算+句子边界+overlap）、块内→全局偏移换算、分块 map-reduce 抽取、朴素归并、规则法抽取器/语料清洗
- [x] **L06 Middleware**：洋葱模型 7 挂载点；审计/成本/质量三中间件、token 与耗时自动采集、可解释质量分（34 测试）
- [x] **L07 向量嵌入与 pgvector**：本地 bge-small-zh（ONNX/fastembed）+ Docker pgvector，实体向量入库/余弦最近邻/别名精确匹配（37 单测 + 3 DB 集成测试）
- [x] **L08 知识库存储层**：relations/evidence 表、幂等 Ingestor（ExtractionOutput→向量+实体/关系/出处）、KnowledgeBase 精确+向量定位与关系图知识卡片（44 测试，含 7 DB 集成）
- [ ] **L09 实体消歧与 HITL**：相似度候选 → 消歧 Agent 判定 → 人工确认入库 ← *当前*
- [ ] **L10 多智能体编排**：抽取/消歧/校验三 Agent + `GoalPipeline` 质量闭环
- [ ] **L11 状态与 Session**：批量文档任务、断点续跑、多用户隔离
- [ ] **L12 MCP 扩展**：把知识库查询挂为 MCP 工具，给任意 Agent 复用
- [ ] **L13 可观测性**：Tracing/Studio、抽取质量指标看板
- [ ] **L14 服务化**：FastAPI（提交文档/查询实体/人工审核接口）
- [ ] **L15 评测与收尾**：黄金数据集、精确率/召回率、CI、部署

## 四、目录约定

```
lessons/      学习区：每节课一个独立、可直接运行的脚本，注释即讲义
src/moray/    生产区：把 lessons 里验证过的模式沉淀成的企业级代码
tests/        测试
docs/         架构、ADR、评测报告
```

约定：**先在 `lessons/` 里跑通理解，再重构进 `src/moray/`。** 这正是企业里"原型 → 产品"的真实路径。

## 五、已验证的环境事实（踩坑记录）

1. AgentScope **2.x 需要 Python ≥ 3.11**；本机用 `/usr/bin/python3.12` 建 venv（系统默认 3.10 只能装到 1.0.21）。
2. 火山方舟 Coding Plan 网关：`https://ark.cn-beijing.volces.com/api/coding/v3`（OpenAI 兼容，**已实测 chat + function calling 通过**）；平台按量网关是 `/api/v3`（需开通模型/接入点）。
3. Coding Plan **不支持 embedding 模型** → 向量部分改用本地 `bge-small-zh`（离线、免费、中文好）。
4. `agentscope[openai]` 这个 extra 在 2.x 不存在，`openai` 已是核心依赖；火山引擎用内置的 `VolcengineChatModel`。
