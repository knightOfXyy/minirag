"""
================================================================================
L02 · 让 Agent 长出"手"：Tool / Toolkit 与 ReAct 循环
================================================================================

上一课的 Agent 只会"想"，这节课让它能"做"。你要建立 5 个认知：

  认知 1：工具就是「一个带类型标注 + docstring 的普通 Python 函数」
           FunctionTool 会自动从签名和 docstring 生成 JSON Schema 给模型。
           · 类型标注      -> 参数 schema（必填/类型）
           · Args 段落      -> 每个参数的中文说明（务必写，模型靠它理解）
           · 返回 dict/list -> 自动 json.dumps 后回灌给模型；返回 str 则直接用

  认知 2：Toolkit 是工具的"容器/调度台"
           Toolkit(tools=[...]) 把工具注册进去，负责把 schema 暴露给模型，
           模型决定调用时，再把调用分派给对应的 FunctionTool。
           （2.0 里 MCP、Skill、ToolGroup 也都放进这同一个 Toolkit。）

  认知 3：ReAct 循环到底怎么转（观察事件流就能看见）
           思考(THINKING) → 决定调用工具(TOOL_CALL) → 执行(TOOL_RESULT)
                          → 拿到结果再思考(THINKING) → 最终回答(TEXT)
           没有工具时，一轮模型调用就结束；有工具时可能转好几圈（受 max_iters 限制）。

  认知 4（企业级关键）：工具默认带「权限」
           · is_read_only=True  的只读工具 -> 默认自动放行
           · 有副作用的写工具           -> 默认抛出 REQUIRE_USER_CONFIRM，Agent 挂起
           这就是 HITL（Human-In-The-Loop，人在回路）：
           reply_stream 在需要确认时结束（不占协程、不留锁），
           你把"确认/拒绝"结果喂回去，它再继续。第 9 课做实体入库审核就靠它。

  认知 5：Agent 是"有记忆的状态机"
           它停在等待确认时，不能当作没事一样再 reply() —— 会抛
           "Agent is waiting for tool calls ..."。要么确认恢复，要么中断它。

运行： .venv/bin/python lessons/l02_tool_toolkit.py
================================================================================
"""

from __future__ import annotations

import asyncio
import os

from dotenv import load_dotenv

from agentscope.agent import Agent
from agentscope.credential import VolcengineCredential
from agentscope.event import (
    EventType,
    UserConfirmResultEvent,
    ConfirmResult,
)
from agentscope.message import UserMsg
from agentscope.model import VolcengineChatModel
from agentscope.tool import Toolkit, FunctionTool

# 注意：脚本方式运行时 load_dotenv() 通常能自动找到 .env；
# 但用 stdin/某些 IDE 运行时找不到调用栈文件，稳妥起见显式指定路径。
# 这里给出项目根目录 .env 的绝对/相对健壮写法：
from pathlib import Path

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


# -----------------------------------------------------------------------------
# 一组"假知识库"工具 —— 第 8 课会把它们换成真正的 PostgreSQL 实现
# -----------------------------------------------------------------------------
_ENTITY_DB: dict[str, dict] = {
    "阿里巴巴": {"type": "ORG", "aliases": ["阿里", "Alibaba"], "hq": "杭州"},
    "字节跳动": {"type": "ORG", "aliases": ["字节", "ByteDance"], "hq": "北京"},
    "马云": {"type": "PER", "role": "阿里巴巴主要创始人"},
}

_WRITE_LOG: list[dict] = []


def lookup_entity(name: str, source:str) -> dict:
    """按名称查询实体知识库，返回实体的类型、别名、总部等信息。

    Args:
        name: 实体的标准名称（公司名、人名、地名等），例如「阿里巴巴」。
    """
    print('source:' + source)
    hit = _ENTITY_DB.get(name)
    if hit:
        return {"found": True, "name": name, **hit}
    # 也允许用别名命中（第 9 课会做成向量相似度匹配）
    for std, info in _ENTITY_DB.items():
        if name in info.get("aliases", []):
            return {"found": True, "name": std, "matched_by_alias": name, **info}
    return {"found": False, "name": name, "hint": "知识库中暂无该实体"}


def write_entity(name: str, entity_type: str) -> dict:
    """向实体知识库写入一条新实体（有副作用，执行前需要人工审核）。

    Args:
        name: 要录入的实体名称。
        entity_type: 实体类型，取值 PER(人物)/ORG(机构)/LOC(地点)/EVENT(事件)。
    """
    record = {"id": f"E-{len(_WRITE_LOG) + 1:04d}", "name": name, "type": entity_type}
    _WRITE_LOG.append(record)
    return {"saved": True, **record}


def build_agent(*, tools: list) -> Agent:
    credential = VolcengineCredential(
        api_key=os.environ["ARK_API_KEY"],
        base_url=os.environ.get("ARK_BASE_URL"),
    )
    model = VolcengineChatModel(
        credential=credential,
        model=os.environ.get("ARK_CHAT_MODEL", "deepseek-v4-flash-ga"),
        stream=False,
    )
    return Agent(
        name="Moray",
        system_prompt=(
            "你是企业实体知识库助手。查询信息时优先调用 lookup_entity；"
            "需要录入新实体时调用 write_entity。回答简洁、专业。"
        ),
        model=model,
        toolkit=Toolkit(tools=tools),
    )


# -----------------------------------------------------------------------------
# Demo A：只读工具 —— 看一次完整的 ReAct 循环（自动放行，无需确认）
# -----------------------------------------------------------------------------
async def demo_readonly_tool() -> None:
    print("\n" + "=" * 72)
    print("Demo A：只读工具 lookup_entity（is_read_only=True，自动放行）")
    print("=" * 72)

    agent = build_agent(tools=[FunctionTool(lookup_entity, is_read_only=True)])

    msg = UserMsg(name="student", content="我想了解一下「阿里」这家公司，总部在哪里？")

    # ★ 怎么拿到模型返回的信息？
    #   *_END / *_START 大多只是"边界标记"，真正的内容挂在 *_DELTA 上：
    #   · TEXT_BLOCK_DELTA        .delta -> 模型最终回答文本（流式可多片，需拼接）
    #   · TOOL_CALL_START         .tool_call_name -> 调了哪个工具
    #   · TOOL_CALL_DELTA         .delta -> 工具入参 JSON（可多片）
    #   · TOOL_RESULT_TEXT_DELTA  .delta -> 工具返回 JSON（可多片）
    #   · THINKING_BLOCK_DELTA    .delta -> 模型的思考
    answer_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_calls: list[dict] = []  # 本轮每次工具调用：{name, args, result}
    current_tool: dict | None = None

    event_counter: dict[str, int] = {}
    async for event in agent.reply_stream(msg):
        event_counter[event.type.name] = event_counter.get(event.type.name, 0) + 1

        if event.type == EventType.TOOL_CALL_START:
            current_tool = {"name": event.tool_call_name, "args": "", "result": ""}
            tool_calls.append(current_tool)
            print(f"\n🔧 [模型决定调用工具] {event.tool_call_name}")
        elif event.type == EventType.TOOL_CALL_DELTA:
            current_tool["args"] += str(event.delta)
        elif event.type == EventType.TOOL_RESULT_TEXT_DELTA:
            current_tool["result"] += str(event.delta)
        elif event.type == EventType.TOOL_RESULT_END:
            print("📦 [工具执行完毕，结果已回灌模型]")
        elif event.type == EventType.THINKING_BLOCK_DELTA:
            thinking_parts.append(str(getattr(event, "delta", "") or ""))
        elif event.type == EventType.TEXT_BLOCK_DELTA:
            answer_parts.append(str(getattr(event, "delta", "") or ""))
        elif event.type == EventType.TEXT_BLOCK_END:
            print("💬 [模型生成最终回答]")
        elif event.type == EventType.REPLY_END:
            # finished_reason: completed / tool_calls_interrupted(被人工中断) ...
            print(f"[本轮结束] finished_reason={event.finished_reason}")

    # ---------- 像写业务代码一样，把三类内容取出来用 ----------
    import json

    print("\n" + "-" * 72)
    print("✅ 方式一：从事件流里取（做服务/前端最常用，能拿到全过程）")
    print("-" * 72)
    print("【模型思考】", "".join(thinking_parts).strip()[:100], "...")

    for i, tc in enumerate(tool_calls, 1):
        print(f"\n【第 {i} 次工具调用】工具名: {tc['name']}")
        print("  原始入参:", tc["args"])
        # 入参/结果是 JSON 字符串，解析成 dict 就能在程序里直接用（比如落库）
        try:
            print("  入参(dict):", json.loads(tc["args"]))
        except json.JSONDecodeError:
            pass
        try:
            print("  返回(dict):", json.loads(tc["result"]))
        except json.JSONDecodeError:
            print("  返回(文本):", tc["result"])

    final_answer = "".join(answer_parts).strip()
    print("\n【模型最终回答】", final_answer)

    # 方式二：只想要最终答案、不关心过程 —— 用 reply()，从 Msg.content 的块里取
    print("\n" + "-" * 72)
    print("✅ 方式二：reply() 只要最终 Msg，再从 content 的 TextBlock 取文本")
    print("-" * 72)
    reply_msg = await agent.reply(
        UserMsg(name="student", content="字节跳动总部在哪？一句话。")
    )
    text_blocks = [
        b.text for b in reply_msg.content if type(b).__name__ == "TextBlock"
    ]
    print("【TextBlock 拼接】", "".join(text_blocks))
    # token 用量也挂在 Msg 上（企业做成本核算就靠它，第 6 课 Middleware 细讲）
    print("【token 用量】", reply_msg.usage)

    print("\n--- 本轮事件序列（按类型计数）---")
    for k, v in event_counter.items():
        print(f"  {k:28s} x {v}")
    print("\n👉 你应该能在序列里看到：思考 → TOOL_CALL → TOOL_RESULT → 思考 → TEXT")


# -----------------------------------------------------------------------------
# Demo B：写工具触发人工确认 —— 观察 Agent 如何"挂起"
# -----------------------------------------------------------------------------
async def demo_write_needs_confirmation() -> None:
    print("\n" + "=" * 72)
    print("Demo B：写工具 write_entity（默认需要人工确认）—— Agent 挂起")
    print("=" * 72)

    # 注意这里没有 is_read_only=True
    agent = build_agent(tools=[FunctionTool(write_entity)])

    parked_event = None
    async for event in agent.reply_stream(
        UserMsg(name="student", content="帮我录入实体：腾讯，类型是 ORG。")
    ):
        if event.type == EventType.REQUIRE_USER_CONFIRM:
            parked_event = event
            break  # 现实系统里：此刻应把待确认调用推给前端/审核台

    assert parked_event is not None, "写工具竟然没有触发确认？"
    print("⛔  Agent 挂起，等待人工审核。待执行的工具调用：")
    for tc in parked_event.tool_calls:
        print(f"    · 工具: {tc.name}")
        print(f"      入参: {tc.input}")
    print(f"    (reply_id={parked_event.reply_id[:8]}...，恢复时要用)")

    # 演示：此时若错误地直接再发新消息，会怎样？（认知 5）
    try:
        await agent.reply(UserMsg(name="student", content="再帮我查个东西"))
    except ValueError as e:
        print("\n⚠️  挂起期间直接 reply() 会报错（这是正确的保护机制）：")
        print(f"    {str(e)[:90]}")

    return agent, parked_event


# -----------------------------------------------------------------------------
# Demo C：人工"放行"后恢复执行 —— 完整 HITL 闭环
# -----------------------------------------------------------------------------
async def demo_resume_after_confirm(
    agent: Agent, parked_event
) -> None:
    print("\n" + "=" * 72)
    print("Demo C：人工审核通过 → 恢复 Agent 执行")
    print("=" * 72)

    # 现实中这里来自审核人员点击"通过/拒绝"。我们对每个待确认调用给出裁决：
    #   confirmed=True  -> 放行执行
    #   confirmed=False -> 拒绝，模型会看到"工具被拒绝"并改走别的回应
    confirm_results = [
        ConfirmResult(confirmed=True, tool_call=tc) for tc in parked_event.tool_calls
    ]
    resume_event = UserConfirmResultEvent(
        reply_id=parked_event.reply_id,
        confirm_results=confirm_results,
    )

    answer_parts: list[str] = []
    async for event in agent.reply_stream(resume_event):
        if event.type == EventType.TEXT_BLOCK_DELTA:
            answer_parts.append(str(getattr(event, "delta", "") or ""))

    print("✅ 恢复后 Agent 的回答：", "".join(answer_parts).strip())
    print("📝 写入日志（真实系统这里就是数据库事务提交）：", _WRITE_LOG)


async def main() -> None:
    # await demo_readonly_tool()
    agent, parked = await demo_write_needs_confirmation()
    # await demo_resume_after_confirm(agent, parked)

    print("\n" + "#" * 72)
    print("# 动手任务：")
    print("# 1. 给 lookup_entity 增加一个参数「source」（如 baike/internal），")
    print("#    观察模型是否会根据用户问题自动填这个参数——体会 docstring 的引导作用。")
    print("# 2. 在 Demo C 里把 confirmed 改成 False，观察模型被拒绝后如何回应。")
    print("# 3. 再注册一个 list_relations 只读工具，问一个需要「先查实体、再查关系」")
    print('#    的复合问题，观察事件计数里 MODEL_CALL 是否变成 3 次（多转一圈）。')
    print("#" * 72)


if __name__ == "__main__":
    asyncio.run(main())
