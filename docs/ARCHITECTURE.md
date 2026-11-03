# Moray 架构总览

企业级实体抽取（NER/RE）+ 实体知识库，基于 **AgentScope 2.0**，15 课增量建成。
约 5k 行库代码 + 15 个可运行讲义 + 全量测试（测试数量随各工单合并持续演进，
以最近一次运行为准，不在文档里写死）。设计主线：**每一层都可离线、
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
| `extraction/cache.py` | 分块结果内容级缓存（键=领域指纹（含模板 hash 与 slim/full 模式）+ 模型身份（provider/base_url/model/思考档位）+ 正文）；内存/PostgreSQL 两后端（表 `extraction_result_cache`）、进程内+跨进程单飞（占位行租约+owner 令牌）、损坏降级、TTL/LRU | 05 |
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
- 关系证据复杂度：每个 mention 的句下标只算一次（二分定位），尾 mention 按句
  分桶后头 mention 只在本句内配对；不再对「头 × 尾」每对线性扫句表
  （旧实现 O(H·T·S)，见 r8 F4）。
- 缓存键：`domain_fingerprint(domain, slim=...)` 把回填模式一起 hash 进
  prompt+schema 指纹；`sampling_fingerprint`（temperature / reasoning_effort /
  thinking）也并入 05 内容缓存键。slim/full 的产物形状与语义不同，05 内容缓存
  绝不能共用键（否则关闭态会拿到 slim 的实体与 `backfill`）；换采样档位同样
  不得复用旧产物。
- 整篇别名口径：`unmatched_aliases` 先取各块 `seen_literals` 并集；`pipeline`
  再把 `document.source_text if document.source_text is not None else document.text`
  传给 `merge_backfill_reports`（空字符串是合法的「空正文」，不回退；r9 N12），
  对候选别名补一次线性 AC 扫描。某块漏抽实体但正文里真实存在该别名时，不算
  幻觉；只在返工反馈里出现的别名仍算幻觉。
- 计数口径：`entities_total/matched_literals/...` 跨块按块相加，是
  **chunk-weighted** 计数（重叠块对同一原文各计一次），不是文档级去重值；
  需要文档级计数时由消费方按 `(entity_key, span)` 收敛。
- 跨块 span 歧义对账（r10 S2）：每块独立回填后，`naive_merge` 可能把**同一处
  全局 span** 同时挂到两个逻辑实体上（重叠块里模型给出不同类型）。
  **仅在开启本地回填且存在块信号时**（`pipeline.run` 的 `if backfill_reports:`
  分支；开关关闭不会跑），归并后调用 `reconcile_span_ambiguity`，只对
  `(surface, start, end)` 完全相同的冲突撤销归属（从两侧 mentions 删掉该
  span）并合并成一条全局 `AmbiguousMention`；不同位置上的同名出现不判歧义。
  歧义条目与 `merge_backfill_reports` 共用同一去重索引（`chunk_ids` 聚合全部
  来源块），同一 owner 在同一 `(start,end)` 的重复 mention 只留一条；
  被撤销的 mention 数记入 `reconciled_unattributed`（r11 N1/N2/N10），并作为
  `pipeline_runs.reconciled_unattributed` 进入看板（r12 N7）；块加权
  `block_confidence` 不反算，只额外暴露撤销条数。
- 回填线性（验收 10、r12 S2）：歧义去重用 `(surface, start)` 集合做 O(1)
  判重；旧实现对已产出歧义列表做 `any(...)`，全部命中都歧义时是 O(A²)
  （16000 次命中约 2s），已删除。
- 关系证据候选（r10 N1/N2/N4、r11 F1、r12 N1/N2）：候选带
  `evidence_start/evidence_end`（裁剪后，与 `Relation.evidence_span` 逐值
  对齐）与原始 `sent_start/sent_end`；重叠块的同一处共现按
  `(head_start, tail_start, evidence_start, evidence_end)` 去重，来源块聚合到
  `chunk_ids`；跨块合并后全局只有一条 `selected=True`。**配对阶段就按进程内
  软上限 `MAX_INTERNAL_RELATION_EVIDENCE_CANDIDATES=2000`（每条关系）限流**
  （r14 F1）：句内尾起点排序后逐头从最近位置向两侧扩张，用大顶堆只保留
  最近的 N 对，一旦候选比堆顶更远就停止该头——无标点单句块不再先把 `H×T`
  全物化再裁，避免 05 结果缓存与内存被笛卡尔积撑大。块间传递的是这份**有界**
  候选；只有 API 响应/作业结果落盘边界用 `serialize_backfill_for_output` 按
  `MAX_SERIALIZED_RELATION_EVIDENCE_CANDIDATES=20` 裁剪（selected 在首条）。
  `to_dict()` 默认上限仍可用，但传 `None` 表示全量、`0` 表示空；负数在
  序列化边界 clamp 到默认 20 并记 WARNING（r13 N6，坏配置不能把整次响应打成
  500；同一非法值只告警一次，后续降为 DEBUG，r14 F5；配置层应在启动时另行
  校验）。
  `serialize_backfill_for_output` 返回完全独立的深层副本（含
  `chunk_ids`），消费方改嵌套容器不会反向污染内部报告（r12 N8、r13 N1）。
  **验收 5 的口径收窄**：单条关系进程内最多保留**最近的 2000 条**候选、
  出网 20 条；极端单句（如 50×50 共现）下「保留全部候选」只到 2000 条为止，
  不再声称无条件成立（r13 N2、r14 F1）。
- 实体身份键（r10 N9、r11 N11、r12 S1、r13 N3/N4）：回填与 slim 归并共用
  `entity_identity_key`（默认去首尾空白）；关闭态 `naive_merge` 保持 262319b
  的精确键。回填的输出存值也规范化（实体标准名、关系 head/tail），
  短文档/直连 `Extractor.extract` 路径不经 `naive_merge` 也同口径；
  `entity_signal_key` 同样对 name 去空白，跨块不会把同一实体拆成两个信号键。
  SLIM 归并不仅用去空白后的键，**存值/落库值也同步规范化**
  （实体标准名与关系 head/tail），否则入库 `_resolve` 按精确名匹配不到真实
  实体、会建 MISC 占位。别名同样按此口径规范化：strip、去空、去重，并跳过
  与标准名相同的项（r14 F2）；LEGACY 归并保持原样以维持关闭态逐字段一致。
  `naive_merge` 合并关系时存 `model_copy`，不改写调用方
  传入的 partial（r10 N10）。四个归并口径由 `MergeMode.LEGACY|SLIM` 单一开关
  表达；四个关键字参数是 **deprecated 兼容 shim**，显式传值即覆盖模式默认，
  新代码不要与 `mode` 混用（r12 N9、r13 N5）。
- 回填只扫一次（r10 N5、r11 N4、r12 N5）：质量中间件与抽取器共用
  `cached_backfill_provenance(cache, result, text)`；`cache` 由抽取器在本次
  reply 的 agent 上持有（`Extractor.extract` 显式 `setattr`），并显式传给
  中间件/回填函数；缓存函数内部不再往 owner 写私有属性。键为
  `(原文, 实体(type,name,aliases,attributes,description,confidence),
  关系(head,predicate,tail))`，不做全量 `model_dump_json`。缓存值是整个
  filled 结果，所以 attributes/description/confidence 必须在键里：重试只改
  这些字段时不能命中上一轮产物（r13 S1）。中间件在没有可核对 span
  时把 `span_ratio` 记 `None`（未测），不再写空转的 1.0。
- 单块扫描口径（r11 N8、r12 N6）：仅当 `len(chunks)==1` **且该块
  `start==0`、`end==len(document_text)`（真正覆盖全文）** 时，
  `merge_backfill_reports(single_chunk_covers_document=True)` 才跳过对别名的
  第二次整篇 AC 扫描；前缀块/部分块仍补扫，保证别名幻觉判定以整篇正文为准。
- 审校口径：`rule_review` 的关系端点与回填共用 `literal_index`——标准名永远
  合法；「唯一归属」的别名只在瘦身模式（有 `backfill` 信号）下放宽。关闭态
  逐字面保持 262319b：只认实体标准名**原样**精确命中（不 strip、不看归属
  唯一性；带空白的端点也按原样比较）。同一字面归属多个实体时**不报 issue、
  不触发返工**（这版不做模型补问），只把该处保留为
  `BackfillReport.ambiguous_mentions` 质量信号；④ 也不再对歧义端点报「端点在
  正文中未命中」。
- 重复实体口径：回填链路先按 `(type, name.strip())` 合并同一逻辑实体的重复
  输出（身份键与 `literal_index` 的去空白口径一致，只差空白的标准名不再退化成
  歧义），审校面对的是合并后的结果，因此瘦身模式不再另做「未去重」判定——
  真正同型同名的重复会被静默合并（别名/属性取并集），不返工；「同名不同
  类型」是工单定义的合法歧义，走 mention 歧义信号。关闭态继续按纯 `name`
  比较。
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
python -m pytest tests/ -q                 # 离线确定性单测（CI 同款，无需 DB）
RUN_DB_TESTS=1 python -m pytest tests/ -q  # 全量：额外含 pgvector/MCP/API/作业/分块缓存集成
```

两类测试的具体条数随各工单合并而变（01 度量、02 压力案卷、03 回填、05 缓存
都会增删用例），**以最近一次运行为准**，本文件只描述"跑什么"，不描述"多少个"。

## 从哪开始读代码

建议按课：`lessons/README.md` 是索引，每个讲义只调对应模块的公共接口，
是比模块内注释更友好的入口。生产装配见 `api/server.py` 与 `jobs/handlers.py`。
