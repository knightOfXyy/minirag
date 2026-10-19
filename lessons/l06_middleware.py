"""
================================================================================
L06 · Middleware：让"审计 / 成本 / 质量"自动发生（2.0 取代 1.x Hook）
================================================================================

为什么需要中间件？
  L04/L05 里我们是【手工】打印 token、耗时、span 一致率。真实系统里这些逻辑
  不能散落在业务代码中，而应像"洋葱"一样包在 Agent 外面，对业务零侵入地统一
  采集——这就是 2.0 的 Middleware（1.x 的 Hook 已废弃）。

7 个挂载点（本项目用到其中 4 个）：
  on_reply            包住整次回复（最外层）           -> 总耗时、生命周期
    on_reasoning      包住推理阶段
      on_model_call   包住真正的模型 HTTP 调用         -> token、单次耗时、失败
    on_check_permission 包住权限判定
    on_acting         包住单个工具执行（权限已过）     -> 审计调了哪个工具
    on_compress_context 包住上下文压缩
  on_system_prompt    不是洋葱，是"转换器"：依次改写系统提示词

洋葱模型的两条铁律：
  1. 生成器型钩子必须 async for ev in next_handler(): yield ev —— 不 yield
     就会"吞掉"事件，调用方收不到；
  2. 框架用关键字 next_handler= 传参，形参名必须叫 next_handler。

本项目的三个中间件（src/moray/observability/middlewares.py）：
  CostAccountingMiddleware  一次 reply 的总耗时，开启/收尾共享指标记录
  AuditMiddleware           每次模型调用的 token/成败，每次工具调用审计
  ExtractionMetricsMiddleware 最终结构化结果 -> 实体数/低置信/span一致率/质量分

运行：
  OFFLINE=1 python lessons/l06_middleware.py   # 用假事件演示机制，零花费
  python lessons/l06_middleware.py             # 含一次真实抽取（约 10~30s）
================================================================================
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from agentscope.message import Msg, UserMsg

from moray.observability import (
    AuditMiddleware,
    CostAccountingMiddleware,
    ExtractionMetricsMiddleware,
    MetricsCollector,
    build_observability_stack,
)

OFFLINE = os.environ.get("OFFLINE") == "1"


class _FakeAgent:
    """离线演示用的最小 Agent（真实场景传 AgentScope 的 Agent）。"""
    name = "Extractor"

    def __init__(self, source_text: str | None = None):
        if source_text is not None:
            self._audit_source_text = source_text


# -----------------------------------------------------------------------------
# 第 1 部分：离线驱动三个中间件，看清洋葱的执行顺序与采集内容
# -----------------------------------------------------------------------------
def _structured_msg() -> Msg:
    final = Msg(name="Extractor", role="assistant", content=[])
    final.structured_output = {
        "entities": [
            {"name": "华为", "type": "ORG", "confidence": 0.95,
             "mentions": [{"surface": "华为",
                           "span": {"start": 0, "end": 2, "text": "华为"}}]},
            {"name": "深圳", "type": "LOC", "confidence": 0.55,  # 低置信
             "mentions": [{"surface": "深圳",
                           "span": {"start": 3, "end": 5, "text": "深圳"}}]},
        ],
        "relations": [
            {"head": "华为", "predicate": "总部位于", "tail": "深圳",
             "confidence": 0.9},
        ],
    }
    return final


async def offline_demo() -> None:
    print("=" * 78)
    print("第 1 部分：离线模拟一次 reply（假的 next_handler），观察洋葱链")
    print("=" * 78)

    collector, middlewares = build_observability_stack()
    cost_mw, audit_mw, quality_mw = middlewares
    agent = _FakeAgent(source_text="华为在深圳。")

    # 最内层：真正"业务"——模拟一次模型调用 + 一次工具执行 + 产出结构化 Msg
    async def core_reply():
        # ① 模型调用（被 AuditMiddleware.on_model_call 包住）
        async def fake_model_next():
            await asyncio.sleep(0.01)
            return SimpleNamespace(usage=SimpleNamespace(
                input_tokens=1200, output_tokens=430,
                cache_input_tokens=0, cache_creation_input_tokens=0))

        await audit_mw.on_model_call(
            agent,
            {"messages": ["..."], "tools": [{"name": "GenerateStructuredOutput"}]},
            fake_model_next,
        )

        # ② 工具执行（被 AuditMiddleware.on_acting 包住）
        async def fake_tool_next():
            yield "tool-chunk"

        async for _ in audit_mw.on_acting(
            agent,
            {"tool_call": SimpleNamespace(name="GenerateStructuredOutput")},
            fake_tool_next,
        ):
            pass

        # ③ 最终结构化 Msg
        yield _structured_msg()

    # 质量层在内、成本层在外，按洋葱顺序手工包两层 on_reply
    async def quality_wrapped():
        async for ev in quality_mw.on_reply(agent, {}, core_reply):
            yield ev

    events = [ev async for ev in cost_mw.on_reply(agent, {}, quality_wrapped)]
    print(f"透传事件数：{len(events)}（最终 Msg 也在其中，证明中间件没有吞事件）")

    rec = collector.records[-1]
    print("\n采集到的【抽取指标档案】：")
    print(" ", rec.summary())
    print(f"  · 模型调用明细: {[(c.input_tokens, c.output_tokens, round(c.latency_s, 3)) for c in rec.model_calls]}")
    print(f"  · 工具调用明细: {[(t.name, t.ok) for t in rec.tool_calls]}")
    print(f"  · span 一致率: {rec.span_ratio}")
    print("\n质量分构成：0.5×span一致率(1.0) + 0.3×高置信占比(0.5) + 0.2×非空(1.0)")
    print(f"             = {rec.quality_score}")
    print("👉 低置信实体(深圳 0.55) 被自动标记，生产中它就是要送人工复核的对象。")


# -----------------------------------------------------------------------------
# 第 2 部分：真实抽取——把中间件栈装进 Extractor，指标自动产出
# -----------------------------------------------------------------------------
async def real_demo() -> None:
    print("\n" + "=" * 78)
    print("第 2 部分：真实抽取 + 中间件栈（OFFLINE=1 可跳过）")
    print("=" * 78)
    if OFFLINE:
        print("OFFLINE=1，跳过真实模型调用。")
        return

    from moray.extraction import Document, Extractor

    collector, middlewares = build_observability_stack()
    extractor = Extractor("general", max_retries=1, middlewares=middlewares)

    text = "宁德时代新能源科技股份有限公司（简称宁德时代）总部位于宁德，董事长曾毓群。该公司在德国建有工厂。"
    doc = Document(id="l06_demo", text=text, source="news")

    output = await extractor.extract(doc)

    print("\n抽取结果：")
    for e in output.result.entities:
        print(f"  [{e.type:5}] {e.name}  conf={e.confidence:.2f}")
    print("\n中间件【自动】产出的指标（业务代码里没有任何手动统计）：")
    for rec in collector.records:
        print(" ", rec.summary())
    print("\n多次抽取后可聚合：", collector.totals())


async def main() -> None:
    await offline_demo()
    await real_demo()

    print("\n# 动手任务：")
    print("# 1. 写一个自己的中间件：on_system_prompt 给提示词统一加一句")
    print("#    「所有金额以人民币计价」，观察转换器模式与洋葱模式的区别。")
    print("# 2. 在 AuditMiddleware.on_model_call 里把 input_tokens 故意 *0，")
    print("#    看成本统计如何变化，理解'横切逻辑集中一处'的好处。")
    print("# 3. 把 LOW_CONF_THRESHOLD 调到 0.95，重跑真实抽取，观察低置信计数变化。")


if __name__ == "__main__":
    asyncio.run(main())
