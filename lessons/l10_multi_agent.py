"""
================================================================================
L10 · 多智能体编排：从"一个大模型包打天下"到"抽取团队 + 质量闭环"
================================================================================

为什么要多智能体？
  单个 Agent 又抽实体、又自检、又消歧，容易"自己查自己"——盲区一致、错误照过。
  拆成角色后形成【制衡】：
      抽取员(Executor)  专注产出，可能犯错
           │  ExtractionResult
           ▼
      审校员(Verifier)  专职挑错：错 span / 臆测关系 / 漏抽端点 / 重复实体
           │
      pass ───────────────> 采用、入库
      needs_fix ─反馈意见──> 抽取员返工（有迭代预算，超限转人工）

AgentScope 2.0 的两个编排原语（已对照 2.0.10 源码确认）：
  · GoalPipeline(executor, verifier)：执行-验证反思循环，pass/fail/impossible，
    但它的结构化 schema 固定（report/verification），适合通用任务达成；
  · TeamPipeline(leader, [TeamMember...])：leader 靠一个 TeamAssign 工具派活，
    成员【并发执行、上下文互相隔离、彼此不能私聊】，结果只回给 leader。
    —— 这才是"多个专精 Agent 团队"的标准实现。

本项目的做法（src/moray/orchestration/specialists.py）：
  把角色定义成【可注入的 async 函数】，编排循环本身不绑框架：
    · 离线/测试：注入确定性假角色，秒跑、零花费、可复现（本脚本默认）；
    · 线上：把角色函数内部换成真实 Agent，或直接套 TeamPipeline（见 --live）。

运行：
  python lessons/l10_multi_agent.py            # 离线：看质量闭环如何拦下错误
  python lessons/l10_multi_agent.py --live     # 真实 TeamPipeline（联网，花 token）
================================================================================
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from moray.orchestration import run_extraction_team
from moray.schema import (
    Entity,
    EntityMention,
    ExtractionResult,
    Relation,
    Span,
)

LIVE = "--live" in sys.argv

TEXT = (
    "华为技术有限公司的子公司华为云计算技术有限公司总部位于贵阳。"
    "公司创始人任正非在深圳创立了华为。"
)


# ---------------------------------------------------------------------------
# 离线：一个"会犯错、但听得进反馈"的抽取员。第 1 轮故意产出 3 类典型错误，
# 第 2 轮根据审校意见修正。用来确定性地演示质量闭环（不花一分 token）。
# ---------------------------------------------------------------------------
async def teachable_extractor(text: str, feedback: str | None) -> ExtractionResult:
    if feedback is None:
        # —— 第 1 轮：三类典型错误 ——
        return ExtractionResult(
            entities=[
                Entity(
                    name="华为技术有限公司", type="ORG", confidence=0.9,
                    mentions=[EntityMention(
                        surface="华为",
                        span=Span(start=0, end=2, text="写错了"))],  # 错①span对不上
                ),
            ],
            relations=[
                # 错②尾实体根本不在实体列表（臆测/漏抽）；错③高置信却无证据
                Relation(head="华为技术有限公司", predicate="子公司",
                         tail="华为云计算技术有限公司", confidence=0.95),
            ],
        )

    # —— 第 2 轮：按反馈整改。用 .find() 在原文里程序化定位 span，
    #    而不是手数偏移（手数中文偏移非常容易错——这正是审校要抓的问题）。
    def span_of(surface: str) -> Span:
        i = text.find(surface)
        return Span(start=i, end=i + len(surface), text=surface)

    return ExtractionResult(
        entities=[
            Entity(name="华为技术有限公司", type="ORG", confidence=0.95,
                   aliases=["华为"],
                   mentions=[EntityMention(surface="华为技术有限公司",
                                            span=span_of("华为技术有限公司"))]),
            Entity(name="华为云计算技术有限公司", type="ORG", confidence=0.93,
                   aliases=["华为云"],
                   mentions=[EntityMention(surface="华为云计算技术有限公司",
                                            span=span_of("华为云计算技术有限公司"))]),
            Entity(name="任正非", type="PER", confidence=0.9,
                   mentions=[EntityMention(surface="任正非", span=span_of("任正非"))]),
            Entity(name="贵阳", type="LOC", confidence=0.9,
                   mentions=[EntityMention(surface="贵阳", span=span_of("贵阳"))]),
            Entity(name="深圳", type="LOC", confidence=0.9,
                   mentions=[EntityMention(surface="深圳", span=span_of("深圳"))]),
        ],
        relations=[
            Relation(head="华为技术有限公司", predicate="子公司",
                     tail="华为云计算技术有限公司", confidence=0.95,
                     evidence_span=span_of("子公司华为云计算技术有限公司")),
            Relation(head="任正非", predicate="创立",
                     tail="华为技术有限公司", confidence=0.85,
                     evidence_span=span_of("任正非在深圳创立了华为")),
            Relation(head="华为云计算技术有限公司", predicate="总部位于",
                     tail="贵阳", confidence=0.6,
                     evidence_span=span_of("华为云计算技术有限公司总部位于贵阳")),
        ],
    )


async def offline_demo() -> None:
    print("原文：", TEXT)
    print("=" * 78)
    out = await run_extraction_team(TEXT, extractor=teachable_extractor, max_iters=3)

    for i, review in enumerate(out.history, 1):
        print(f"\n— 第 {i} 轮审校 —  结论：{review.verdict}"
              f"（span 一致率 {review.span_ratio:.0%}）")
        if review.issues:
            print("  审校拦下的问题：")
            for iss in review.issues:
                print("   ✗", iss)
        else:
            print("   ✓ 无问题，通过质量门禁")

    print("\n" + "=" * 78)
    print(f"最终：accepted={out.accepted}，共迭代 {out.review.iterations} 轮")
    print("实体：", [(e.name, e.type) for e in out.result.entities])
    print("关系：", [(r.head, r.predicate, r.tail) for r in out.result.relations])

    print("\n核心认知：")
    print(" 1) 审校员是一道独立制衡，错误在【入库前】被挡下并驱动返工；")
    print(" 2) 迭代有预算（max_iters），超预算 accepted=False -> 转 L09 的人工队列；")
    print(" 3) 角色是可注入函数：把 teachable_extractor 换成真实 Extractor Agent、")
    print("    规则审校换成 LLM 审校 Agent，编排与门禁一行都不用改。")


# ---------------------------------------------------------------------------
# 线上：用真实 TeamPipeline 组一个 leader + 两个专精成员的团队。
# 仅在 --live 时执行（联网、花 token、较慢）。
# ---------------------------------------------------------------------------
async def live_demo() -> None:
    from agentscope.agent import Agent
    from agentscope.message import UserMsg, Msg, TextBlock
    from agentscope.event import EventType
    from agentscope.pipeline import TeamPipeline, TeamMember

    from moray.config import build_chat_model

    model = build_chat_model(stream=False)

    # 成员 1：实体/关系抽取专员（产出 JSON 文本即可，leader 负责汇总）
    extractor_agent = Agent(
        name="Extractor",
        system_prompt=(
            "你是实体关系抽取专员。只根据给定原文，抽取 PER/ORG/LOC 实体与"
            "有明确证据的关系。用简洁中文列出，实体标注类型，关系写成"
            "「头 -[谓词]-> 尾」。不要臆测，不要输出与原文无关内容。"
        ),
        model=model,
    )
    # 成员 2：审校专员
    reviewer_agent = Agent(
        name="Reviewer",
        system_prompt=(
            "你是严格的抽取质量审校。检查别人给你的抽取结果：是否有原文支撑、"
            "关系端点是否都存在、有没有重复或漏抽。给出 PASS 或 NEEDS_FIX，"
            "并列出具体问题。"
        ),
        model=model,
    )

    # leader：自己不抽，只负责按需要把活派给合适成员，并给最终结论
    leader = Agent(
        name="Coordinator",
        system_prompt=(
            "你是抽取团队协调者。需要抽取时派给 Extractor，需要质量核查时派给"
            "Reviewer。根据 Reviewer 的意见决定是否要求 Extractor 修正，"
            "最多两轮。最后输出定稿结果。"
        ),
        model=model,
    )

    team = TeamPipeline(
        leader=leader,
        members=[
            TeamMember(agent=extractor_agent,
                       description="从中文原文抽取实体与关系三元组"),
            TeamMember(agent=reviewer_agent,
                       description="审校抽取结果是否有臆测、漏抽、重复"),
        ],
    )

    print("用 TeamPipeline 真实运行…")
    print("注意：TeamAssign 派活事件被 TeamPipeline 在内部消费（不向外抛），")
    print("所以我们从 leader 的文字叙述观察它何时派给 Extractor / Reviewer。\n")
    final_parts: list[str] = []
    async for ev in team.reply_stream(
        UserMsg(name="user", content=f"请抽取并审校下面这段文本：\n{TEXT}")
    ):
        # 只收集最终文本；成员的中间步骤彼此隔离，不外泄给调用方
        if ev.type == EventType.TEXT_BLOCK_DELTA:
            final_parts.append(str(getattr(ev, "delta", "") or ""))
        elif isinstance(ev, Msg) and ev.finished_reason is not None:
            print(f"  [团队轮次结束 finished={ev.finished_reason}]")

    print("\n最终输出：\n", "".join(final_parts).strip() or "(见上方事件)")


async def main() -> None:
    if LIVE:
        await live_demo()
    else:
        await offline_demo()
    print("\n# 动手任务：")
    print("# 1. 改 teachable_extractor 让它第 2 轮仍犯错，观察 accepted=False")
    print("#    （预算耗尽）——思考此时应如何接到 L09 人工审核队列。")
    print("# 2. 给 rule_review 增加一条规则（如实体必须至少有一个 mention），")
    print("#    重跑看审校意见变化。")
    print("# 3. --live 观察 TeamPipeline：leader 是否真的先派 Extractor 再派 Reviewer，")
    print("#    成员之间是否被隔离（它们看不到彼此的中间过程）。")


if __name__ == "__main__":
    asyncio.run(main())
