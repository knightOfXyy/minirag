# Moray 架构总览

企业级实体抽取（NER/RE）+ 实体知识库，基于 **AgentScope 2.0**，15 课增量建成。
约 5k 行库代码 + 15 个可运行讲义 + 342 个测试。设计主线：**每一层都可离线、
确定性地单独验证**，LLM 只出现在明确的接缝处。

## 数据流

```
                         ┌──────────────── 批量路径（L11）────────────────┐
                         │                                                  │
 文档文本 ──► 分块(L05) ──► 抽取器 ──► 结构化结果 ──► 审校/返工(L10) ──► 入库(Ingestor L08)
   ▲           chunking    (LLM/规则)   Entity/Relation   规则+多智能体          │
   │                          L02-05        +Span(L03)                          ▼
   │                                                            消歧/合并/HITL(L09)
 实时 API ─────────────────► POST /extract (L14 FastAPI)                 │
                                                                          ▼
                                                          PostgreSQL + pgvector
                                                          实体/别名/提及/关系/证据
                                                          合并审核/作业/看板(L07-13)
                                                                          │
              查询：locate/card/ask(L08)  ·  MCP 工具(L12)  ·  HTTP API(L14)
                                                                          │
                                                          可观测：OTEL + 看板(L13)
                                                                          │
                                                          质量回归：黄金集 P/R/F1(L15)
```

## 模块地图

| 模块 | 职责 | 课 |
|---|---|---|
| `schema/` | 稳定核心 `Entity/Relation/Span` + 领域配置/动态枚举/`attributes` JSONB | L01/06 |
| `extraction/` | Agent 事件/block、FunctionTool/ReAct/HITL、结构化输出、分块 map-reduce、分块级度量 `chunk_metrics`、规则抽取器、质量校验 `verify_spans`、评测基线 | L02-05/15 |
| `config_instrumented.py` | 模型边界埋点：捞回供应商原生 `finish_reason`（截断护栏）、流式分段计时；不改请求/解析内容 | L04/01 |
| `extraction/cache.py` | 分块结果内容级缓存（键=领域指纹+模型+正文+模板版本）；内存/PostgreSQL 两后端（表 `extraction_result_cache`）、进程内+跨进程单飞、损坏降级、TTL/LRU | 05 |
| `embedding/` | bge-small-zh ONNX 本地嵌入（单例/L2 归一/批处理） | L07 |
| `kb/store.py` | 实体/别名/提及/关系/证据表、ivfflat 索引、余弦近邻、幂等 upsert、合并、审核队列 | L07-09 |
| `kb/ingestor.py` | 抽取结果→向量+实体+关系+溯源；缺端点补 MISC；可选消歧 | L08/09 |
| `kb/query.py` | `KnowledgeBase.locate/card/ask` | L08 |
| `kb/disambiguation.py` | 规则/LLM 判官（SAME/DIFFERENT）、子公司护栏、事务化合并 | L09 |
| `orchestration/` | 抽取-审校闭环（可注入 async 函数）+ AgentScope TeamPipeline 真实多智能体 | L10 |
| `jobs/` | `extraction_jobs` 持久队列：`SKIP LOCKED`、陈旧回收、幂等 handler、断点续跑 | L11 |
| `mcp_server/` | FastMCP 只读工具（locate/card/similar/relations），stdio/http | L12 |
| `observability/` | OTEL 追踪中间件、`pipeline_runs` 质量/成本看板（含分块度量与 api/job 来源）、成本/审计/质量中间件 | L06/13 |
| `api/` | FastAPI：抽取/作业/查询/HITL/看板；依赖注入 + lifespan | L14 |
| `evaluation/` | 黄金集加载、严格/宽松匹配、P/R/F1、宏/微平均、基线对比 | L15 |
| `evaluation/stress/` | 多种子确定性压力案卷（`data/gold/stress/`，正文>5×块预算）+ 答案可信闸门 + 零 LLM 分块规则抽取器 + 按块预算/重叠的网格 A/B 报告；报告自带 `overlap_note`/`entity_axis_note`：overlap 当前无判别力、实体轴只证明接线，块预算判据看关系轴 | 工单02 |

## 结构化输出瘦身与本地回填（工单 03）

- 开关：`MORAY_LOCAL_BACKFILL`（默认关）。打开后模型只输出逻辑实体
  （name/type/aliases/attributes）与逻辑关系（head/predicate/tail），不再输出
  mentions/字符偏移/证据句/自报置信度。
- 职责边界：`extraction/local_backfill.py` 是纯函数模块，负责 ①Aho–Corasick
  多模式一次扫描 ②最长匹配区间裁决 ③歧义未归属 ④关系「同句共现」证据
  ⑤`BackfillReport` 确定性信号；不读环境、不调模型。文件内按这 5 块组织，
  新读者从 `backfill_provenance` / `merge_backfill_reports` 两个入口读即可。
- 偏移：块内回填 -> `pipeline.remap_spans` 平移到全局；跨块信号汇总时
  `merge_backfill_reports` 再按 `chunk_start` 平移并带 `chunk_id`。
- 置信度：模型自报语义不变；slim 模式不自报 = `None`/SQL NULL（未知），
  不能被 0.0 冒充。下游（审校/看板/API）消费 `BackfillReport.block_confidence`
  等确定性信号。

## 贯穿全项目的工程原则

1. **LLM 接缝最小化**：抽取器实现统一 `async extract(Document)->ExtractionResult`，
   规则/LLM 可无缝替换；评测、入库、审校、API 都不依赖具体模型。
2. **幂等是可靠性的地基**：Ingestor 用 COALESCE 唯一索引/NOT EXISTS 去重，
   作业至少一次投递下断点续跑，合并迁移外键且清理自环/重复关系。
3. **可离线验证**：每课配确定性 fake（假模型/假嵌入器），httpx ASGITransport
   不起端口测 API；DB/MCP/实时 LLM 测试用开关或独立 flag 隔离。
4. ** provenance 可追溯**：实体提及带字符级 span，关系带证据 span，质量门
   `verify_spans` 防止幻觉偏移。
5. **人在环里**：消歧不确定不自动合并，进 `merge_reviews` 队列由 API/MCP 裁决。
6. **评测驱动**：改动先有黄金集分数（L15），用数字回归而非感觉。

## 测试

```bash
python -m pytest tests/ -q                 # 281 个离线确定性单测（CI 同款）
RUN_DB_TESTS=1 python -m pytest tests/ -q  # 全量 342（+61 个 pgvector/MCP/API/作业/合并集成）
```

## 从哪开始读代码

建议按课：`lessons/README.md` 是索引，每个讲义只调对应模块的公共接口，
是比模块内注释更友好的入口。生产装配见 `api/server.py` 与 `jobs/handlers.py`。
