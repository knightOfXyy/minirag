"""
================================================================================
L01 · 你的第一个 AgentScope 2.0 Agent
================================================================================

这节课你要建立 4 个认知（它们是整个项目的地基）：

  认知 1：Agent = 一个「推理-行动(ReAct)循环」内核
           2.0 把 1.x 的 ReActAgent 重构成了统一的 Agent 类。
           哪怕不给它任何工具，它也是一个完整 Agent，只是这一轮"没有行动可做"。

  认知 2：Agent 有两个入口（2.0 最重要的 API 变化）
           · await agent.reply(msg)        -> 只要最终一条消息（好用）
           · async for ev in agent.reply_stream(msg)  -> 订阅全过程事件（强大）
           1.x 时代的 agent(msg) / agent.print 已废弃：
           Agent 变成"纯生产者"，怎么展示由调用方决定（这对做服务至关重要）。

  认知 3：一切中间过程都是「事件 Event」
           模型开始/结束、文本流、思考流(thinking)、工具调用、工具结果、
           甚至"请求人工确认"，都是 reply_stream 吐出的事件。
           前端打字机效果、日志、计费、审计……全部挂在事件流上。

  认知 4：模型与密钥是独立积木（Credential + Model 分离）
           Credential 管"你是谁/连哪"，Model 管"怎么调"。
           本课手工搭这对积木；换供应商（火山方舟 / DeepSeek / 任意 OpenAI
           兼容网关）只是换这两个类，由 .env 的 LLM_PROVIDER 决定。

运行： .venv/bin/python lessons/l01_hello_agent.py
================================================================================
"""

from __future__ import annotations

import asyncio

from dotenv import load_dotenv

# ---- AgentScope 2.0 的积木（注意导入路径，和 1.x 已大不相同）----------------
from agentscope.agent import Agent
from agentscope.event import EventType
from agentscope.message import UserMsg  # 等价于 Msg(role="user", ...)

# 项目自己的配置工厂：读 .env 决定连哪一家（见 src/moray/config.py）
from moray.config import get_settings, provider_classes


# 把项目根目录 .env 载入环境变量（LLM_PROVIDER + 对应那家的 KEY/BASE_URL/MODEL）
load_dotenv()


def build_agent() -> Agent:
    """组装一个最小可用的 Agent：身份(system prompt) + 大脑(model)。"""

    # 本课故意把"接线"手工展开，让你看清接入一家 API 到底需要哪两样东西；
    # 生产代码里这两步收在 moray.config.build_chat_model()，
    # 换供应商只改 .env 的 LLM_PROVIDER（volcengine / deepseek / openai）。
    s = get_settings()
    cred_cls, model_cls = provider_classes()

    # ① 凭证：Key + 网关地址。三家都是 OpenAI 兼容协议，区别只在 base_url 与模型名。
    credential = cred_cls(api_key=s["api_key"], base_url=s["base_url"])

    # ② 模型：网关上的 deepseek 同时支持普通对话与 function calling（结构化输出靠它）。
    #    stream=False 时 reply_stream 依然会产出"块"事件，只是来源是非流式接口。
    model = model_cls(
        credential=credential,
        model=s["chat_model"],
        stream=True,  # 先关掉底层流式，方便你看清"完整块"事件；L02 再开
    )

    # ③ Agent：2.0 统一内核。此时没有 toolkit，所以它只会"推理"，不会"行动"。
    agent = Agent(
        name="Moray",
        system_prompt=(
            "你是 Moray，一名金融行业的实体抽取专家。回答简洁、专业，"
            "当前你还没有任何工具，只能凭自身知识回答。"
        ),
        model=model,
    )
    return agent


# -----------------------------------------------------------------------------
# 入口 A：reply() —— 我只想要最终答案
# -----------------------------------------------------------------------------
async def demo_reply(agent: Agent) -> None:
    print("\n" + "=" * 70)
    print("入口 A：await agent.reply(msg) —— 只拿最终消息")
    print("=" * 70)

    # 2.0 的消息统一为 Msg；UserMsg 是预设 role="user" 的构造快捷方式。
    # content 传入字符串后，内部会规范化成 block 列表（文本/思考/工具调用块）。
    msg = UserMsg(name="student", content="用一句话解释什么是「命名实体识别(NER)」？")

    reply = await agent.reply(msg)

    # reply 也是 Msg（role="assistant"）。
    # 关键变化：content 不再是字符串，而是 block 列表。
    print(f"\n[返回类型] {type(reply).__name__}, role={reply.role}")
    print(f"[content 类型] {type(reply.content)}")
    for i, block in enumerate(reply.content):
        # 不同 block 类型带不同字段：TextBlock / ToolCallBlock / ThinkingBlock ...
        btype = type(block).__name__
        preview = str(getattr(block, "text", getattr(block, "content", block)))[:80]
        print(f"  block[{i}] {btype}: {preview}")


# -----------------------------------------------------------------------------
# 入口 B：reply_stream() —— 我要看见 Agent 的"每一次呼吸"
# -----------------------------------------------------------------------------
async def demo_stream(agent: Agent) -> None:
    print("\n" + "=" * 70)
    print("入口 B：reply_stream —— 订阅全部事件")
    print("=" * 70)

    msg = UserMsg(
        name="student",
        content="帮我心算：17 * 23 等于多少？把推理过程说出来。",
    )

    # 计数器：让你直观看到一次对话到底产生了哪些事件、各有多少个
    event_counter: dict[str, int] = {}
    thinking_parts: list[str] = []
    answer_parts: list[str] = []

    async for event in agent.reply_stream(msg):
        event_counter[event.type.name] = event_counter.get(event.type.name, 0) + 1

        # —— 思考块增量：很多模型(如 deepseek-r 系)会把"内心戏"单独输出 ——
        if event.type == EventType.THINKING_BLOCK_DELTA:
            piece = getattr(event, "delta", None) or getattr(event, "text", "")
            thinking_parts.append(str(piece))

        # —— 正文文本块增量：这就是前端"打字机"要消费的东西 ——
        elif event.type == EventType.TEXT_BLOCK_DELTA:
            piece = getattr(event, "delta", None) or getattr(event, "text", "")
            answer_parts.append(str(piece))

        # —— 一轮 reply 的生命周期边界（做服务时用来收尾、落库、计费）——
        elif event.type in (EventType.REPLY_START, EventType.REPLY_END):
            print(f"\n>>> 事件: {event.type.name}")

    print("\n----- 模型的思考(thinking) -----")
    print("".join(thinking_parts).strip() or "（该模型本轮未单独输出思考块）")
    print("\n----- 模型的最终回答(text) -----")
    print("".join(answer_parts).strip())

    print("\n----- 本轮事件统计（理解事件流的最好方式）-----")
    for name, count in sorted(event_counter.items()):
        print(f"  {name:28s} x {count}")


async def main() -> None:
    agent = build_agent()
    await demo_reply(agent)
    await demo_stream(agent)

    print("\n" + "#" * 70)
    print("# 动手任务（必做，改成你自己的实验）：")
    print("# 1. 把 system_prompt 改成「你是一名金融行业的实体抽取专家」，再问问题，")
    print("#    体会 system prompt 如何决定 Agent 的行为边界。")
    print("# 2. 把 model 的 stream 改成 True，观察事件数量/粒度有什么变化。")
    print("# 3. 故意把当前 provider 的 KEY 改错（默认 ARK_API_KEY，"
          "LLM_PROVIDER=deepseek 时是 DEEPSEEK_API_KEY），观察异常长什么样——"
          "这是以后做重试的入口。")
    print("#" * 70)


if __name__ == "__main__":
    asyncio.run(main())
