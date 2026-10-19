"""
结构化 + 语义联合检索（L08）
============================

两种检索各有所长，组合起来才是"知识库问答"的基础：
  · 语义检索（向量）：用户用自然语言/简称也能定位实体；
  · 结构化检索（关系表/外键）：精确回答"X 投资了谁""谁是 X 子公司"这类问题。

KnowledgeBase 把两者拼成实体的"知识卡片"：
    定位实体（精确优先、向量兜底） -> 拉出它的关系与出处。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..embedding import Embedder, get_embedder
from .store import EntityStore, SimilarEntity


@dataclass
class EntityCard:
    id: int
    name: str
    type: str
    attributes: dict
    confidence: float
    aliases: list[str] = field(default_factory=list)
    outgoing: list[dict] = field(default_factory=list)   # 我 -> 别人
    incoming: list[dict] = field(default_factory=list)   # 别人 -> 我

    def summary(self) -> str:
        lines = [f"[{self.type}] {self.name}"]
        if self.aliases:
            lines.append(f"  别名: {', '.join(self.aliases)}")
        for r in self.outgoing:
            lines.append(f"  --[{r['predicate']}]--> {r['dst_name']}")
        for r in self.incoming:
            lines.append(f"  <--[{r['predicate']}]-- {r['src_name']}")
        return "\n".join(lines)


class KnowledgeBase:
    """对 EntityStore 的面向问答的高层封装。"""

    def __init__(self, store: EntityStore, embedder: Embedder | None = None) -> None:
        self.store = store
        self.embedder = embedder or get_embedder()

    async def locate(
        self, text: str, *, domain_code: str = "general", k: int = 5
    ) -> list[SimilarEntity]:
        """先精确匹配，命不中再走向量召回。"""
        exact = await self.store.find_by_name_or_alias(text, domain_code=domain_code)
        if exact:
            # 精确命中直接返回（距离 0）
            return [SimilarEntity(
                id=exact["id"], name=exact["name"], type=exact["type"],
                domain_code=exact["domain_code"], distance=0.0,
                aliases=[],
            )]
        vec = self.embedder.embed_one(text)
        return await self.store.similar_entities(
            vec, domain_code=domain_code, k=k, max_distance=0.55
        )

    async def card(self, entity_id: int) -> EntityCard | None:
        row = await self.store.get(entity_id)
        if row is None:
            return None
        out = await self.store.relations_of(entity_id, direction="out")
        inc = await self.store.relations_of(entity_id, direction="in")
        # 取别名
        aliases = []
        async with self.store.pool.acquire() as conn:
            aliases = [r["alias"] for r in await conn.fetch(
                "SELECT alias FROM entity_aliases WHERE entity_id=$1", entity_id
            )]
        return EntityCard(
            id=row["id"], name=row["name"], type=row["type"],
            attributes=row["attributes"], confidence=float(row["confidence"]),
            aliases=aliases,
            outgoing=[
                {"predicate": r["predicate"], "dst_name": r["dst_name"],
                 "dst_type": r["dst_type"], "confidence": float(r["confidence"])}
                for r in out
            ],
            incoming=[
                {"predicate": r["predicate"], "src_name": r["src_name"],
                 "src_type": r["src_type"], "confidence": float(r["confidence"])}
                for r in inc
            ],
        )

    async def ask(self, text: str, *, domain_code: str = "general") -> EntityCard | None:
        """一句话问答的最小闭环：自然语言定位实体 -> 返回知识卡片。"""
        candidates = await self.locate(text, domain_code=domain_code, k=1)
        if not candidates:
            return None
        return await self.card(candidates[0].id)
