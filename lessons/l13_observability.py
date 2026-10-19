"""
================================================================================
L13 · 可观测性：调用链追踪（OTEL）+ 抽取质量看板
================================================================================

两层可观测，回答两类不同问题：

1) 技术调用链（OpenTelemetry / agentscope.TracingMiddleware）
   一次抽取到底经过了哪些环节？Agent -> 模型调用 -> 工具执行，各自多久、
   父子关系如何？这是【通用技术追踪】，可导出到 Jaeger/Tempo/Phoenix。
   关键：TracingMiddleware 只有在进程里初始化了真正的 TracerProvider 才工作，
   否则零开销短路。本脚本用内存导出器离线演示，也可换 console/otlp。

2) 业务质量看板（pipeline_runs 表 + Dashboard）
   抽取系统好不好用？盯的是 OTEL 不知道的领域指标：
     span 一致率 / 审校通过率 / 返工轮数 / 遗留问题
     消歧自动合并率（自动化程度）/ 转人工率
     每篇 token 成本与耗时

运行：
  python lessons/l13_observability.py            # 离线：假模型触发真实 span + 看板
  python lessons/l13_observability.py --console  # span 打印到控制台（OTEL 原样 JSON）
  前置：docker start moray-pgvector
================================================================================
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

CONSOLE = "--console" in sys.argv


# ---------------------------------------------------------------------------
# 离线假模型：实现 ChatModelBase.__call__，确定性返回一段文本，
# 这样无需联网也能让 Agent 产生真实的 model/agent span。
# ---------------------------------------------------------------------------
def build_fake_model():
    from agentscope.model import ChatModelBase
    from agentscope.credential import CredentialBase
    from agentscope.model._model_response import ChatResponse
    from agentscope.message import TextBlock
    from agentscope.formatter import OpenAIChatFormatter
    from pydantic import BaseModel

    class _Params(BaseModel):
        pass

    class _Cred(CredentialBase):
        def __init__(self):
            pass

        async def __call__(self):  # 某些路径会取凭证
            return {}

    class FakeChatModel(ChatModelBase):
        def __init__(self):
            super().__init__(
                credential=_Cred(), model="fake-local",
                parameters=_Params(), stream=False)
            # Agent 需要 formatter 来整理输入消息
            self.formatter = OpenAIChatFormatter()
            self.calls = 0

        async def __call__(self, messages, tools=None, tool_choice=None, **kw):
            self.calls += 1
            return ChatResponse(
                content=[TextBlock(type="text",
                                   text="这是离线假模型的确定性回复（无网络）。")],
                is_last=True)

    return FakeChatModel()


async def tracing_demo() -> None:
    from agentscope.agent import Agent
    from agentscope.message import UserMsg, Msg
    from agentscope.tool import FunctionTool, Toolkit
    from agentscope.event import EventType

    from moray.observability import (
        build_tracing_middleware,
        setup_tracing,
    )

    print("=" * 78)
    print("1) 调用链追踪：初始化 OTEL（内存导出）+ TracingMiddleware")
    print("=" * 78)
    mem = setup_tracing("console" if CONSOLE else "memory",
                        service_name="moray-ner-l13")

    def kb_lookup(name: str) -> str:
        """查询实体知识库（只读演示工具）。"""
        return f"{name} -> 类型 ORG，别名若干（假数据）"

    agent = Agent(
        name="TracedExtractor",
        system_prompt="你是可观测性演示 Agent。",
        model=build_fake_model(),
        toolkit=Toolkit(tools=[FunctionTool(kb_lookup, is_read_only=True)]),
        middlewares=[build_tracing_middleware()],
    )

    async for _ev in agent.reply_stream(
        UserMsg(name="user", content="查一下腾讯")
    ):
        pass  # 跑完一次完整 reply，中间件会自动开 agent/model span

    if CONSOLE:
        print("（span 已由 ConsoleSpanExporter 打印在上方 JSON）")
        return

    names = mem.names()
    print("本次 reply 产生的 span（按结束顺序）：")
    for n in names:
        print("  •", n)
    kinds = {n.split(".", 1)[0] for n in names}
    print("覆盖层级：", sorted(kinds))
    assert any("agent" in n for n in names), "应至少有 agent span"
    print("✓ TracingMiddleware 在真实 TracerProvider 下自动记录了调用链")


# ---------------------------------------------------------------------------
# 业务质量看板：用 RunRecorder 造一批"文档运行"指标，落库后聚合。
# 真实系统里这些数字来自 L10 审校结果 + L08/09 入库报告（from_pipeline）。
# ---------------------------------------------------------------------------
async def dashboard_demo() -> None:
    from moray.kb import close_pool, init_pool
    from moray.observability import Dashboard, RunRecorder

    print("\n" + "=" * 78)
    print("2) 业务质量看板：pipeline_runs 落库 + SQL 聚合")
    print("=" * 78)
    pool = await init_pool()
    dash = Dashboard(pool)
    await dash.init_schema()
    await pool.execute("TRUNCATE pipeline_runs RESTART IDENTITY")

    runs = [
        # 高质量、全自动的文档
        RunRecorder(document_id="doc_a", n_entities=9, n_relations=6,
                    span_ratio=1.0, review_passed=True, review_iters=1,
                    n_auto_merged=2, n_queued=0, n_created=7,
                    input_tokens=1200, output_tokens=900, latency_ms=8200),
        # 返工 2 轮才过
        RunRecorder(document_id="doc_b", n_entities=7, n_relations=4,
                    span_ratio=0.86, review_passed=True, review_iters=2,
                    n_auto_merged=1, n_queued=1, n_created=5,
                    input_tokens=2600, output_tokens=1900, latency_ms=15300),
        # 没通过审校、留了问题，多个实体转人工
        RunRecorder(document_id="doc_c", n_entities=12, n_relations=5,
                    span_ratio=0.62, review_passed=False, review_iters=3,
                    open_issues=4, n_auto_merged=1, n_queued=3, n_created=8,
                    input_tokens=4100, output_tokens=3300, latency_ms=24100),
    ]
    for r in runs:
        await dash.record(r)

    s = await dash.summary()
    print(f"运行文档数        : {s['runs']}")
    print(f"审校通过率        : {s['pass_rate']:.0%}（{s['passed']}/{s['runs']}）")
    print(f"平均 span 一致率  : {s['avg_span_ratio']:.2f}")
    print(f"平均返工轮数      : {s['avg_review_iters']:.2f}")
    print(f"遗留问题总数      : {s['total_open_issues']}")
    print(f"实体/关系总量     : {s['total_entities']}/{s['total_relations']}")
    print(f"消歧自动合并/人工 : {s['auto_merged']}/{s['queued']}"
          f"  → 自动化率 {None if s['auto_merge_rate'] is None else format(s['auto_merge_rate'], '.0%')}")
    print(f"token 总量        : in {s['input_tokens']} / out {s['output_tokens']}")
    print(f"平均耗时          : {s['avg_latency_ms']:.0f} ms")

    print("\n最近运行（看板明细）：")
    for row in await dash.recent(limit=3):
        flag = "✓过" if row["review_passed"] else "✗挂"
        print(f"  {row['document_id']} {flag} 轮{row['review_iters']} "
              f"span={row['span_ratio']:.2f} 实体{row['n_entities']} "
              f"人工{row['n_queued']} {row['latency_ms']}ms")

    await close_pool(pool)
    print("\n解读：")
    print(" · 通过率/span 一致率低 → 抽取 prompt 或模型有问题；")
    print(" · 返工轮数高 → 成本涨（看 token），该优化 schema 或上规则前置；")
    print(" · 自动化率低/人工率高 → 消歧阈值或嵌入模型需调（回看 L09）。")


async def main() -> None:
    await tracing_demo()
    await dashboard_demo()
    print("\n# 动手任务：")
    print("# 1. --console 看 OTEL 原生 span JSON 的父子关系（parent_id）。")
    print("# 2. 起一个 OTLP collector，export='otlp' 后在 Jaeger/Tempo 里看链路。")
    print("# 3. 把 L10/L11 真实运行用 RunRecorder.from_pipeline() 写进看板，")
    print("#    对比真实文档的通过率/自动化率，而不是这里的造数。")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:  # noqa: BLE001
        print("出错（数据库是否在跑？docker start moray-pgvector）：")
        import traceback
        traceback.print_exc()
