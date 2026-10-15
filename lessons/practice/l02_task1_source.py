"""
L02 动手任务 1 · 练习脚本
=========================
任务：给 lookup_entity 增加 source 入参，验证模型会根据问题语境自动填值。

怎么算成功？
  跑两个不同语境的问题，观察打印出的「工具入参」：
    · 问公开/百科性质的问题 -> source 应被填成 "baike"
    · 问公司内部资料的问题  -> source 应被填成 "internal"

运行： .venv/bin/python lessons/practice/l02_task1_source.py
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

from agentscope.agent import Agent
from agentscope.credential import VolcengineCredential
from agentscope.event import EventType
from agentscope.message import UserMsg
from agentscope.model import VolcengineChatModel
from agentscope.tool import Toolkit, FunctionTool


# ★ 改动点 1：新增 source 参数（Literal -> 模型只能二选一）
def lookup_entity(
    name: str,
    source: Literal["baike", "internal"],
) -> dict:
    """按名称查询实体信息。

    Args:
        name: 实体的标准名称，例如公司名、人名。
        source: 资料来源，按问题性质二选一：
            - "baike"：公开百科资料，用于回答对外的、常识性的、公众可见的信息
              （如公司总部、成立时间、创始人、公开新闻）。
            - "internal"：公司内部资料，用于回答企业内部视角的信息
              （如内部系统里的客户编号、对接人、合同、员工、内部备注）。
    """
    # ★ 改动点 2：让两个来源真的返回不同内容，这样从结果也能看出选了哪个
    table = {
        "baike": {
            "阿里巴巴": {"type": "ORG", "hq": "杭州", "founded": 1999},
        },
        "internal": {
            "阿里巴巴": {"customer_id": "CUST-88231", "owner_sales": "张磊",
                        "contract_level": "KA"},
        },
    }
    hit = table.get(source, {}).get(name)
    return {"found": bool(hit), "name": name, "source": source, "data": hit}


def build_agent() -> Agent:
    cred = VolcengineCredential(
        api_key=os.environ["ARK_API_KEY"], base_url=os.environ["ARK_BASE_URL"]
    )
    model = VolcengineChatModel(
        credential=cred, model=os.environ["ARK_CHAT_MODEL"], stream=False
    )
    return Agent(
        name="Moray",
        system_prompt="你是实体知识库助手，需要查询时调用 lookup_entity。",
        model=model,
        toolkit=Toolkit(tools=[FunctionTool(lookup_entity, is_read_only=True)]),
    )


async def ask(agent: Agent, question: str) -> None:
    print("\n" + "=" * 72)
    print("🙋 用户问题：", question)

    cur: dict | None = None
    answer: list[str] = []

    async for ev in agent.reply_stream(UserMsg(name="student", content=question)):
        if ev.type == EventType.TOOL_CALL_START:
            cur = {"name": ev.tool_call_name, "args": ""}
        elif ev.type == EventType.TOOL_CALL_DELTA:
            cur["args"] += str(ev.delta)
        elif ev.type == EventType.TEXT_BLOCK_DELTA:
            answer.append(str(getattr(ev, "delta", "") or ""))

    # ★ 验证点：这里打印的入参里应该能看到模型自己填的 source
    if cur:
        print("🔧 工具调用：", cur["name"])
        print("   入参(dict)：", json.loads(cur["args"]))
        chosen = json.loads(cur["args"]).get("source")
        print(f"   ✅ 模型自动选择的 source = {chosen!r}")
    else:
        print("⚠️ 模型本轮没有调用工具（可换个问法再试）")
    print("💬 回答：", "".join(answer).strip())


async def main() -> None:
    agent = build_agent()

    # 问题 1：明显是公开常识 -> 期望 source="baike"
    await ask(agent, "阿里巴巴总部在哪？这家公司哪年成立的？")

    # 问题 2：明显是企业内部视角 -> 期望 source="internal"
    await ask(agent, "帮我查一下内部 CRM 里阿里巴巴这个客户的编号和负责销售是谁？")

    print("\n" + "#" * 72)
    print("# 对照两次打印的 source：如果一个是 baike、一个是 internal，")
    print("# 就说明模型确实在读 docstring 并自动选参——任务通过。")
    print("# 进阶：故意把 docstring 里 baike/internal 的说明删掉再跑，")
    print("# 你会发现模型开始乱猜或瞎填——这就是 docstring 的威力。")
    print("#" * 72)


if __name__ == "__main__":
    asyncio.run(main())
