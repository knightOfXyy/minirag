"""
实体知识库 MCP Server（L12）
============================

把前 11 课建好的知识库能力，通过 MCP（Model Context Protocol）暴露成标准工具，
于是【任何】支持 MCP 的 Agent / 客户端（不止我们自己的抽取流水线）都能复用：

    locate_entity     自然语言/简称/别名 -> 定位规范实体（精确优先，向量兜底）
    get_entity_card   取一个实体的知识卡片（别名 + 出入边关系）
    find_similar      纯向量近邻检索（找语义相近的实体）
    list_relations    结构化查询某实体的出边/入边关系

为什么用 FastMCP：
  AgentScope 2.0 自带的是 MCP【客户端】(agentscope.mcp.MCPClient)，用来接入
  外部工具；要把【我们的】知识库对外提供，需要 MCP【服务端】。官方 mcp 包的
  FastMCP 用 @mcp.tool() 装饰普通 Python 函数即可自动生成工具 schema，最简单。

运行（stdio 传输，被 AgentScope MCPClient 或其它 MCP host 拉起）：
    python -m moray.mcp_server.kb_server
HTTP 传输（常驻服务）：
    python -m moray.mcp_server.kb_server --transport streamable-http --port 8765

设计要点：
  · 惰性初始化：server 被 stdio 拉起时不立即连库/加载模型，首次工具调用才建
    连接池与 Embedder，启动快、也避免无调用时空占内存；
  · 工具函数返回 JSON 可序列化的 dict/list，不泄漏 ORM/连接对象；
  · 核心逻辑写成纯 async 函数（service 层），可不经 MCP 直接单测。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ..kb import EntityStore, KnowledgeBase, close_pool, init_pool

# 这些工具全部只读、无副作用：标注 readOnlyHint，让接入方（如 AgentScope）
# 自动放行，无需每次人工确认。
_READ_ONLY = ToolAnnotations(readOnlyHint=True)


class _KBContext:
    """惰性持有连接池 / Store / KnowledgeBase（进程级单例）。"""

    def __init__(self) -> None:
        self.pool = None
        self.store: EntityStore | None = None
        self.kb: KnowledgeBase | None = None

    async def ensure(self) -> KnowledgeBase:
        if self.kb is not None:
            return self.kb
        if self.pool is None:
            # server 进程的工作目录不一定是项目根，确保能读到 .env
            try:
                from dotenv import load_dotenv

                root = Path(__file__).resolve().parents[3]
                load_dotenv(root / ".env")
            except Exception:  # noqa: BLE001
                pass
            self.pool = await init_pool()
            self.store = EntityStore(self.pool)
            await self.store.init_schema()
            self.kb = KnowledgeBase(self.store)
        return self.kb

    async def aclose(self) -> None:
        if self.pool is not None:
            await close_pool(self.pool)
            self.pool = None


def build_server(kb: KnowledgeBase | None = None) -> FastMCP:
    """构造 MCP server。

    kb: 可注入现成的 KnowledgeBase（主要给测试用，避免加载嵌入模型）；
        不传则在首次工具调用时用生产配置惰性初始化。
    """
    mcp = FastMCP("moray-entity-kb")
    ctx = _KBContext()
    if kb is not None:
        ctx.kb = kb
        ctx.store = kb.store
        ctx.pool = kb.store.pool  # 标记已初始化（关闭仍由调用方管理）

    @mcp.tool(annotations=_READ_ONLY)
    async def locate_entity(query: str, domain_code: str = "general",
                            k: int = 5) -> list[dict]:
        """用自然语言、简称或别名定位知识库中的规范实体。

        先走规范名/别名精确匹配，命不中再用向量语义召回。返回候选实体及其
        类型与相似度，按相似度从高到低排列。

        Args:
            query: 实体指称，如「华为」「华为公司」或一句描述。
            domain_code: 领域代码，默认 general。
            k: 最多返回候选数。
        """
        kb = await ctx.ensure()
        cands = await kb.locate(query, domain_code=domain_code, k=k)
        return [
            {
                "id": c.id,
                "name": c.name,
                "type": c.type,
                "similarity": round(c.similarity, 4),
                "aliases": c.aliases,
            }
            for c in cands
        ]

    @mcp.tool(annotations=_READ_ONLY)
    async def get_entity_card(name_or_id: str,
                              domain_code: str = "general") -> dict | None:
        """获取实体的「知识卡片」：别名 + 全部出边/入边关系。

        先用名称/别名定位（也可直接传实体 id 数字），再聚合它的关系图，
        适合回答「某公司的子公司/创始人/总部」这类问题。

        Args:
            name_or_id: 实体规范名、别名，或数字 id。
            domain_code: 领域代码，默认 general。
        """
        kb = await ctx.ensure()
        entity_id = None
        if name_or_id.isdigit():
            entity_id = int(name_or_id)
        else:
            cands = await kb.locate(name_or_id, domain_code=domain_code, k=1)
            if cands:
                entity_id = cands[0].id
        if entity_id is None:
            return None
        card = await kb.card(entity_id)
        return None if card is None else {
            "id": card.id,
            "name": card.name,
            "type": card.type,
            "aliases": card.aliases,
            "attributes": card.attributes,
            "confidence": card.confidence,
            "outgoing": card.outgoing,
            "incoming": card.incoming,
        }

    @mcp.tool(annotations=_READ_ONLY)
    async def find_similar(query: str, entity_type: str | None = None,
                           domain_code: str = "general", k: int = 5,
                           min_similarity: float = 0.5) -> list[dict]:
        """纯向量语义近邻检索：找与 query 语义最相近的实体。

        不做精确匹配短路，适合探索性查找（近义词、错别字、模糊描述）。

        Args:
            query: 文本指称。
            entity_type: 可选类型过滤（PER/ORG/LOC...）。
            domain_code: 领域代码。
            k: 返回数量。
            min_similarity: 相似度下限（0~1），默认 0.5。
        """
        kb = await ctx.ensure()
        embedder = kb.embedder
        vec = embedder.embed_one(query)
        hits = await kb.store.similar_entities(
            vec, domain_code=domain_code, type=entity_type, k=k,
            max_distance=1 - min_similarity)
        return [
            {"id": h.id, "name": h.name, "type": h.type,
             "similarity": round(h.similarity, 4), "aliases": h.aliases}
            for h in hits
        ]

    @mcp.tool(annotations=_READ_ONLY)
    async def list_relations(name: str, direction: str = "both",
                             predicate: str | None = None,
                             domain_code: str = "general") -> list[dict]:
        """结构化查询某实体的关系（出边/入边）。

        精确回答「X 投资了谁（出边 out）」「谁投资了 X（入边 in）」。

        Args:
            name: 实体规范名或别名。
            direction: out=我指向谁，in=谁指向我，both=两者。
            predicate: 可选，只看某种关系（如「子公司」）。
            domain_code: 领域代码。
        """
        kb = await ctx.ensure()
        # 先精确（规范名/别名），命不中再语义定位，简称也能查到关系
        entity_id = await kb.store.get_entity_id(
            name, domain_code=domain_code)
        if entity_id is None:
            cands = await kb.locate(name, domain_code=domain_code, k=1)
            if cands:
                entity_id = cands[0].id
        if entity_id is None:
            return []
        rels = await kb.store.relations_of(
            entity_id, direction=direction, predicate=predicate)
        return [
            {
                "source": r["src_name"], "source_type": r["src_type"],
                "predicate": r["predicate"],
                "target": r["dst_name"], "target_type": r["dst_type"],
                "confidence": (None if r["confidence"] is None
                               else round(float(r["confidence"]), 3)),
            }
            for r in rels
        ]

    # 暴露给进程退出钩子做清理（FastMCP 会在 shutdown 时触发）
    mcp._moray_ctx = ctx  # type: ignore[attr-defined]
    return mcp


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Moray 实体知识库 MCP Server")
    parser.add_argument("--transport", default="stdio",
                        choices=["stdio", "sse", "streamable-http"])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)

    mcp = build_server()
    if args.transport in ("sse", "streamable-http"):
        mcp.settings.host = args.host
        mcp.settings.port = args.port
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main(sys.argv[1:])
