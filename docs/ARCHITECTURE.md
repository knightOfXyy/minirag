# Moray 架构总览

企业级实体抽取（NER/RE）+ 实体知识库，基于 **AgentScope 2.0**，15 课增量建成。
约 5k 行库代码 + 15 个可运行讲义 + 342 个测试（数字随测试演进，以实际运行为准）。设计主线：**每一层都可离线、
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
- 缓存键：`domain_fingerprint(domain, slim=...)` 把回填模式一起 hash 进
  prompt+schema 指纹；`sampling_fingerprint`（temperature / reasoning_effort /
  thinking）也并入 05 内容缓存键。slim/full 的产物形状与语义不同，05 内容缓存
  绝不能共用键（否则关闭态会拿到 slim 的实体与 `backfill`）；换采样档位同样
  不得复用旧产物。
- 整篇别名口径：`unmatched_aliases` 先取各块 `seen_literals` 并集；`pipeline`
  再把 `document.source_text or document.text` 传给 `merge_backfill_reports`，
  对候选别名补一次线性 AC 扫描。某块漏抽实体但正文里真实存在该别名时，不算
  幻觉；只在返工反馈里出现的别名仍算幻觉。
- 计数口径：`entities_total/matched_literals/...` 跨块按块相加，是
  **chunk-weighted** 计数（重叠块对同一原文各计一次），不是文档级去重值；
  需要文档级计数时由消费方按 `(entity_key, span)` 收敛。
- 审校口径：`rule_review` 的关系端点与回填共用 `literal_index`——标准名永远
  合法；「唯一归属」的别名只在瘦身模式（有 `backfill` 信号）下放宽。关闭态
  逐字面保持 262319b：只认实体标准名**原样**精确命中（不 strip、不看归属
  唯一性；带空白的端点也按原样比较）。同一字面归属多个实体时只有瘦身模式
  会报「归属不唯一或未知」，不做归属猜测；④ 不再对歧义端点重复报「端点在
  正文中未命中」。
- 重复实体口径：回填链路先按 `(type, name)` 合并同一逻辑实体的重复输出，
  审校面对的是合并后的结果，因此瘦身模式不再另做「未去重」判定——真正同型
  同名的重复会被静默合并（别名/属性取并集），不返工；「同名不同类型」是工单
  定义的合法歧义，走 mention 歧义信号。关闭态继续按纯 `name` 比较。
- 跨块命中口径：⑤ 用文档级实体键集合（`matched_literals` 中 `n==0` 的键去掉
  `ambiguous_only`）判定「未找到出处」，不拿 chunk-weighted 的
  `entities_grounded/entities_total` 相加结果与文档级歧义集合比较。
  `matched_literals` 缺失或为空（旧调用方手写的精简字典）时退回计数口径
  `grounded + ambiguous_only < total`，不能把空字典当成「全部命中」。
  歧义未归属只作为质量信号（`BackfillReport.ambiguous_mentions`）保留，
  **不**进返工 feedback：这版不做模型补问。
- 置信度：模型自报语义不变；slim 模式不自报 = `None`/SQL NULL（未知），
  不能被 0.0 冒充。下游（审校/看板/API）消费 `BackfillReport.block_confidence`
  等确定性信号。
- **置信度默认值（刻意破坏性变更）**：内核 `Entity.confidence` /
  `Relation.confidence` 的默认值由 `0.0` 改为 `None`（未知）。full 动态
  schema 的模型契约仍是 0~1 数值（缺省 0.0），`to_core_result` 对 full 输出
  保持 0.0；只有直接构造内核对象且不传 confidence 的调用方会看到 `null`。
  迁移方式：把 `confidence` 当可空字段判空，不要 `float(confidence)` 或
  `confidence < x` 直接比较。
- 回填坐标系：`Document.source_text` 是 mention/span 的唯一坐标系。返工轮
  provider 会把审校反馈追加到 `text`（让模型看到要改什么），但必须同时传
  `source_text=原始正文`，否则反馈里的实体名会被回填成越界 span。
- 切句边界：`split_sentences(preserve_leading=False)` 是分块默认，逐字节保持
  262319b 的边界（以结束符/换行开头返回空）；关系证据回填显式
  `preserve_leading=True`，两种语义不得互相污染。

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
python -m pytest tests/ -q                 # 308 个离线确定性单测（CI 同款）
RUN_DB_TESTS=1 python -m pytest tests/ -q  # 全量（+DB/pgvector/MCP/API/作业集成；本轮未复跑）
```

## 从哪开始读代码

建议按课：`lessons/README.md` 是索引，每个讲义只调对应模块的公共接口，
是比模块内注释更友好的入口。生产装配见 `api/server.py` 与 `jobs/handlers.py`。
