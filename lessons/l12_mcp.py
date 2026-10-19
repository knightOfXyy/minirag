"""
================================================================================
L12 · MCP 扩展：把实体知识库变成"任何 Agent 都能用"的标准工具
================================================================================

MCP（Model Context Protocol）解决的是"工具复用"问题：
  以前我们的知识库查询只在自己的 Python 代码里可调；封装成 MCP Server 后，
  任何支持 MCP 的 Agent / 客户端（AgentScope、Claude Desktop、Cursor…）都能
  用同一套标准协议发现并调用这些工具，与语言、框架无关。

重要区分（对照 agentscope 2.0.10 源码）：
  · agentscope.mcp.MCPClient 是【客户端】：把外部 MCP server 的工具接进 Agent；
  · 要【对外提供】工具，需要 MCP【服务端】。我们用官方 mcp 包的 FastMCP：
        @mcp.tool() 装饰普通 async 函数 -> 自动生成 JSON Schema -> stdio/HTTP 暴露。

暴露的 4 个工具（src/moray/mcp_server/kb_server.py）：
  locate_entity    自然语言/简称/别名 -> 规范实体（精确优先，向量兜底）
  get_entity_card  实体卡片（别名 + 出入边关系）
  find_similar     纯向量近邻检索
  list_relations   结构化出/入边关系查询

两种运行：
  python lessons/l12_mcp.py             # 离线：进程内直接调工具（快、不起子进程）
  python lessons/l12_mcp.py --stdio     # 真·MCP：AgentScope MCPClient 拉起 server
  python lessons/l12_mcp.py --agent     # 让一个 Agent 自主决定调用哪个 MCP 工具
================================================================================
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

MODE_STDIO = "--stdio" in sys.argv
MODE_AGENT = "--agent" in sys.argv


def _decode(contents) -> object:
    """FastMCP 把 list 的每个元素作为独立文本块（{...}{...}），需多文档解码。"""
    text = "".join(getattr(b, "text", str(b)) for b in contents).strip()
    if not text:
        return None
    dec, vals, i, n = json.JSONDecoder(), [], 0, len(text)
    while i < n:
        while i < n and text[i].isspace():
            i += 1
        if i >= n:
            break
        obj, i = dec.raw_decode(text, i)
        vals.append(obj)
    return vals if len(vals) > 1 else vals[0]


async def inproc_demo() -> None:
    """离线：直接在进程内调用 FastMCP 工具（走真实知识库，但不起子进程）。"""
    from moray.kb import EntityStore, close_pool, init_pool
    from moray.mcp_server import build_server

    pool = await init_pool()
    store = EntityStore(pool)
    await store.init_schema()
    if await store.count() == 0:
        print("知识库为空，请先运行：python lessons/l11_batch_resume.py")
        await close_pool(pool)
        return
    mcp = build_server()  # 用生产 KB（惰性初始化，含本地嵌入模型）

    async def call(tool_name, **args):
        contents, _ = await mcp.call_tool(tool_name, args)
        return _decode(contents)

    print("=" * 78)
    print("工具 1：locate_entity —— 简称也能定位规范实体")
    print("=" * 78)
    print(json.dumps(await call("locate_entity", query="腾讯", k=2),
                     ensure_ascii=False, indent=2))

    print("\n" + "=" * 78)
    print("工具 2：get_entity_card —— 一张卡片看清实体的关系网")
    print("=" * 78)
    card = await call("get_entity_card", name_or_id="腾讯")
    print(json.dumps(card, ensure_ascii=False, indent=2))

    print("\n" + "=" * 78)
    print("工具 3：list_relations —— 精确结构化查询（出边/入边）")
    print("=" * 78)
    print("出边:", json.dumps(await call("list_relations", name="腾讯",
                                          direction="out"), ensure_ascii=False))
    print("入边:", json.dumps(await call("list_relations", name="腾讯",
                                          direction="in"), ensure_ascii=False))

    print("\n" + "=" * 78)
    print("工具 4：find_similar —— 向量近邻（模糊探索）")
    print("=" * 78)
    print(json.dumps(await call("find_similar", query="深圳的科技公司",
                                 entity_type="ORG", k=3, min_similarity=0.0),
                     ensure_ascii=False, indent=2))

    await close_pool(pool)


async def stdio_demo(with_agent: bool = False) -> None:
    """真·MCP：用 AgentScope MCPClient 通过 stdio 拉起我们的 server。"""
    from agentscope.mcp import MCPClient, StdioMCPConfig

    client = MCPClient(
        name="moray-kb",
        is_stateful=True,  # stdio 必须是有状态连接
        mcp_config=StdioMCPConfig(
            command=sys.executable,
            args=["-m", "moray.mcp_server.kb_server"],
            env={
                **os.environ,
                "PYTHONPATH": str(ROOT / "src"),
                "HF_ENDPOINT": "https://hf-mirror.com",
                "HF_HUB_DISABLE_XET": "1",
            },
        ),
    )
    await client.connect()
    try:
        tools = await client.list_tools()
        print("AgentScope 从 MCP server 发现的工具：")
        for t in tools:
            print(f"  - {t.name}")

        # get_tool 需要 raw 名（list_raw_tools 给的是不带 mcp__ 前缀的原名）
        raw_names = [t.name for t in await client.list_raw_tools()]
        raw = lambda leaf: next(n for n in raw_names if n.split("__")[-1] == leaf)
        loc = await client.get_tool(raw("locate_entity"))
        card_tool = await client.get_tool(raw("get_entity_card"))

        def text_of(tool_chunk) -> str:
            return "".join(
                getattr(b, "text", "") for b in getattr(tool_chunk, "content", []))

        if not with_agent:
            r = await loc(query="比亚迪", k=1)
            print("\n直接调用 locate_entity('比亚迪') ->\n", text_of(r))
            r2 = await card_tool(name_or_id="比亚迪")
            print("\n直接调用 get_entity_card('比亚迪') ->\n", text_of(r2))
            return

        # —— 让 Agent 自己决定用哪个工具 ——
        from agentscope.agent import Agent
        from agentscope.message import UserMsg
        from agentscope.tool import Toolkit
        from moray.config import build_chat_model

        toolkit = Toolkit()
        for t in tools:
            await toolkit.add_tool(t)  # add_tool 是协程，必须 await
        agent = Agent(
            name="KBExplorer",
            system_prompt=(
                "你只能依据知识库【工具返回的结果】回答，严禁使用你记忆中的"
                "外部常识或编造任何未在工具结果里出现的实体/关系。\n"
                "回答任何实体问题时，必须按顺序调用工具：\n"
                "1) 先调用 mcp__moray-kb__locate_entity 定位实体；\n"
                "2) 再调用 mcp__moray-kb__get_entity_card 取其关系；\n"
                "3) 只根据卡片里的 outgoing/incoming 作答；查不到就明确说"
                "「知识库中没有该实体或关系」。"
            ),
            model=build_chat_model(stream=False),
            toolkit=toolkit,
        )
        reply = await agent.reply(
            UserMsg(name="user", content="比亚迪和谁有关系？请查知识库后回答。"))
        from agentscope.message import TextBlock
        print("\nAgent 自主使用 MCP 工具后的回答：")
        for blk in reply.content:
            if isinstance(blk, TextBlock):
                print(blk.text)
    finally:
        await client.close()


async def main() -> None:
    if MODE_STDIO or MODE_AGENT:
        await stdio_demo(with_agent=MODE_AGENT)
    else:
        await inproc_demo()
    print("\n# 动手任务：")
    print("# 1. 起一个常驻 HTTP MCP：python -m moray.mcp_server.kb_server \\")
    print("#      --transport streamable-http --port 8765，再用 HttpMCPConfig 接入。")
    print("# 2. --agent 观察 Agent 是否自主先 locate 再取 card（ReAct + MCP）。")
    print("# 3. 给 kb_server 新增一个 search_relations(predicate) 工具，重启后")
    print("#    在客户端 list_tools 里确认它被自动发现——体会 MCP 的解耦。")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:  # noqa: BLE001
        print("出错（数据库是否在跑？docker start moray-pgvector）：")
        print("错误：", repr(exc)[:300])
