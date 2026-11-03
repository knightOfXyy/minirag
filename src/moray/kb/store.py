"""
实体向量存储（L07）
===================

用 asyncpg 直连 PostgreSQL + pgvector，提供：
  · init_schema()           建表/扩展
  · upsert_entity()         实体 + 向量 + 别名 + 出处的幂等写入
  · similar_entities()      按向量余弦相似度找候选（L09 实体消歧的核心原语）
  · count() / get()         基础查询

只依赖标准 SQL 与 pgvector 的 <=> 操作符，不用 ORM，方便你看清向量检索本质。
连接信息从环境变量读取（.env）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asyncpg

# schema.sql 与本文件同目录
_SCHEMA_SQL = Path(__file__).with_name("schema.sql")


def _dsn() -> str:
    return (
        f"postgresql://{os.environ.get('POSTGRES_USER','moray')}:"
        f"{os.environ.get('POSTGRES_PASSWORD','moray_dev_2026')}"
        f"@{os.environ.get('POSTGRES_HOST','localhost')}:"
        f"{os.environ.get('POSTGRES_PORT','5433')}/"
        f"{os.environ.get('POSTGRES_DB','moray_kb')}"
    )


async def _init_connection(conn: asyncpg.Connection) -> None:
    """让 asyncpg 用 Python 对象收发 json/jsonb（否则读出来是字符串）。"""
    import json

    for t in ("json", "jsonb"):
        await conn.set_type_codec(
            t,
            encoder=lambda v: json.dumps(v, ensure_ascii=False),
            decoder=json.loads,
            schema="pg_catalog",
        )


async def init_pool(min_size: int = 1, max_size: int = 5) -> asyncpg.Pool:
    return await asyncpg.create_pool(
        _dsn(), min_size=min_size, max_size=max_size, init=_init_connection)


async def close_pool(pool: asyncpg.Pool) -> None:
    await pool.close()


def _vec_literal(vec: list[float]) -> str:
    """把 Python 向量转成 pgvector 字面量 '[1.0,0.5,...]'。"""
    return "[" + ",".join(f"{x:.7f}" for x in vec) + "]"


@dataclass
class SimilarEntity:
    id: int
    name: str
    type: str
    domain_code: str
    distance: float          # 余弦距离（0=完全同向）；相似度 = 1 - distance
    aliases: list[str]

    @property
    def similarity(self) -> float:
        return 1.0 - self.distance


class EntityStore:
    """实体向量 CRUD。"""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def init_schema(self) -> None:
        await self.pool.execute(_SCHEMA_SQL.read_text(encoding="utf-8"))

    async def count(self, domain_code: str | None = None) -> int:
        if domain_code:
            return await self.pool.fetchval(
                "SELECT count(*) FROM entities WHERE domain_code=$1", domain_code
            )
        return await self.pool.fetchval("SELECT count(*) FROM entities")

    # ------------------------------------------------------------------
    async def upsert_entity(
        self,
        *,
        name: str,
        type: str,
        embedding: list[float],
        domain_code: str = "general",
        aliases: list[str] | None = None,
        attributes: dict[str, Any] | None = None,
        confidence: float | None = None,
        mentions: list[dict] | None = None,
    ) -> int:
        """幂等写入一个实体（含向量/别名/出处），返回实体 id。

        同一 (domain, type, name) 已存在则更新向量与属性，并合并别名/出处。
        confidence=None 表示「未知」（瘦身模式模型不自报），写 NULL；
        GREATEST 会忽略 NULL，因此未知不会把已有已知值冲成 0。
        mentions: [{'document_id','surface','start','end','chunk_id'}]
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # INSERT ... ON CONFLICT DO UPDATE
                # jsonb 经连接 codec 自动编解码（见 init_pool._init_connection）
                row = await conn.fetchrow(
                    """
                    INSERT INTO entities
                        (domain_code, name, type, attributes, confidence, embedding)
                    VALUES ($1,$2,$3,$4,$5,$6::vector)
                    ON CONFLICT (domain_code, type, name) DO UPDATE SET
                        attributes = EXCLUDED.attributes,
                        confidence = GREATEST(entities.confidence, EXCLUDED.confidence),
                        embedding  = EXCLUDED.embedding,
                        updated_at = now()
                    RETURNING id
                    """,
                    domain_code, name, type, (attributes or {}),
                    confidence, _vec_literal(embedding),
                )
                entity_id = row["id"]

                for alias in (aliases or []):
                    if alias and alias != name:
                        await conn.execute(
                            "INSERT INTO entity_aliases(entity_id, alias) "
                            "VALUES ($1,$2) ON CONFLICT DO NOTHING",
                            entity_id, alias,
                        )

                for m in (mentions or []):
                    # 用 NOT EXISTS 守卫做幂等：配合 schema.sql 里 COALESCE 的
                    # 表达式唯一索引，span/chunk 为 NULL 时也能正确去重。
                    await conn.execute(
                        """
                        INSERT INTO entity_mentions
                            (entity_id, document_id, surface, start_off, end_off, chunk_id)
                        SELECT $1,$2,$3,$4,$5,$6
                        WHERE NOT EXISTS (
                            SELECT 1 FROM entity_mentions
                            WHERE entity_id=$1 AND document_id=$2 AND surface=$3
                              AND COALESCE(start_off,-1)=COALESCE($4,-1)
                              AND COALESCE(end_off,-1)=COALESCE($5,-1)
                              AND COALESCE(chunk_id,'')=COALESCE($6,'')
                        )
                        """,
                        entity_id,
                        m.get("document_id"),
                        m.get("surface"),
                        m.get("start"),
                        m.get("end"),
                        m.get("chunk_id"),
                    )
        return entity_id

    # ------------------------------------------------------------------
    async def similar_entities(
        self,
        embedding: list[float],
        *,
        domain_code: str | None = None,
        type: str | None = None,
        k: int = 5,
        max_distance: float | None = None,
    ) -> list[SimilarEntity]:
        """按余弦距离找最相近的实体（向量检索）。

        max_distance: 只返回距离小于阈值的候选（如 0.25 ≈ 相似度>0.75）。
        """
        clauses, params = [], []
        idx = 1
        if domain_code:
            clauses.append(f"domain_code=${idx}"); params.append(domain_code); idx += 1
        if type:
            clauses.append(f"type=${idx}"); params.append(type); idx += 1
        if max_distance is not None:
            clauses.append(f"(embedding <=> ${idx}::vector) < ${idx+1}")
            params += [_vec_literal(embedding), max_distance]
        else:
            clauses.append("TRUE")
        where = " AND ".join(clauses) if clauses else "TRUE"
        # 无论是否用阈值过滤，都需要向量参数用于排序
        params.append(_vec_literal(embedding))
        order_param = f"${len(params)}"

        sql = f"""
            SELECT e.id, e.name, e.type, e.domain_code,
                   (e.embedding <=> {order_param}::vector) AS distance,
                   COALESCE(array_agg(a.alias) FILTER (WHERE a.alias IS NOT NULL),
                            '{{}}') AS aliases
            FROM entities e
            LEFT JOIN entity_aliases a ON a.entity_id = e.id
            WHERE e.embedding IS NOT NULL
              AND {where}
            GROUP BY e.id
            ORDER BY distance
            LIMIT {int(k)}
        """
        rows = await self.pool.fetch(sql, *params)
        return [
            SimilarEntity(
                id=r["id"], name=r["name"], type=r["type"],
                domain_code=r["domain_code"], distance=float(r["distance"]),
                aliases=list(r["aliases"]),
            )
            for r in rows
        ]

    async def find_by_name_or_alias(
        self, text: str, *, domain_code: str | None = None
    ) -> dict | None:
        """按规范名或别名【精确】匹配（向量召回之外的确定性通道）。

        实体消歧时先过精确匹配（100%确定、不耗算力），再走向量模糊匹配。
        """
        sql = """
            SELECT e.id, e.domain_code, e.name, e.type, e.attributes, e.confidence
            FROM entities e
            LEFT JOIN entity_aliases a ON a.entity_id = e.id
            WHERE (e.name = $1 OR a.alias = $1)
        """
        params: list[Any] = [text]
        if domain_code:
            sql += " AND e.domain_code = $2"
            params.append(domain_code)
        sql += " LIMIT 1"
        row = await self.pool.fetchrow(sql, *params)
        return dict(row) if row else None

    async def get(self, entity_id: int) -> dict | None:
        row = await self.pool.fetchrow(
            "SELECT id, domain_code, name, type, attributes, confidence "
            "FROM entities WHERE id=$1", entity_id
        )
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # 关系
    # ------------------------------------------------------------------
    async def get_entity_id(
        self, name: str, *, domain_code: str = "general", type: str | None = None
    ) -> int | None:
        """按规范名或别名解析实体 id；可限定类型。"""
        sql = (
            "SELECT e.id FROM entities e "
            "LEFT JOIN entity_aliases a ON a.entity_id=e.id "
            "WHERE e.domain_code=$1 AND (e.name=$2 OR a.alias=$2)"
        )
        params: list[Any] = [domain_code, name]
        if type:
            sql += " AND e.type=$3"
            params.append(type)
        sql += " ORDER BY (e.name=$2) DESC LIMIT 1"
        return await self.pool.fetchval(sql, *params)

    async def upsert_relation(
        self,
        *,
        source_id: int,
        predicate: str,
        target_id: int,
        domain_code: str = "general",
        confidence: float | None = None,
    ) -> int:
        row = await self.pool.fetchrow(
            """
            INSERT INTO relations
                (domain_code, source_entity_id, predicate, target_entity_id, confidence)
            VALUES ($1,$2,$3,$4,$5)
            ON CONFLICT (domain_code, source_entity_id, predicate, target_entity_id)
            DO UPDATE SET confidence = GREATEST(relations.confidence, EXCLUDED.confidence)
            RETURNING id
            """,
            domain_code, source_id, predicate, target_id, confidence,
        )
        return row["id"]

    async def add_relation_evidence(
        self, relation_id: int, *, document_id: str, ev: dict
    ) -> None:
        await self.pool.execute(
            """
            INSERT INTO relation_evidence
                (relation_id, document_id, surface, start_off, end_off, chunk_id)
            VALUES ($1,$2,$3,$4,$5,$6) ON CONFLICT DO NOTHING
            """,
            relation_id, document_id,
            ev.get("surface"), ev.get("start"), ev.get("end"), ev.get("chunk_id"),
        )

    async def relations_of(
        self,
        entity_id: int,
        *,
        direction: str = "both",
        predicate: str | None = None,
    ) -> list[dict]:
        """查某实体的关系（出边/入边/两者），关联返回对方实体名。"""
        conds, params = [], []
        if direction in ("out", "both"):
            conds.append("r.source_entity_id=$1")
        if direction in ("in", "both"):
            conds.append("r.target_entity_id=$1")
        where = "(" + " OR ".join(conds) + ")"
        params.append(entity_id)
        if predicate:
            where += f" AND r.predicate=${len(params)+1}"
            params.append(predicate)

        sql = f"""
            SELECT r.id, r.predicate, r.confidence,
                   r.source_entity_id AS src_id, se.name AS src_name, se.type AS src_type,
                   r.target_entity_id AS dst_id, te.name AS dst_name, te.type AS dst_type
            FROM relations r
            JOIN entities se ON se.id=r.source_entity_id
            JOIN entities te ON te.id=r.target_entity_id
            WHERE {where}
            ORDER BY r.confidence DESC NULLS LAST
        """
        return [dict(r) for r in await self.pool.fetch(sql, *params)]

    async def count_relations(self) -> int:
        return await self.pool.fetchval("SELECT count(*) FROM relations")

    # ------------------------------------------------------------------
    # 实体合并（L09 消歧）
    # ------------------------------------------------------------------
    async def merge_entities(self, survivor_id: int, duplicate_id: int) -> None:
        """把 duplicate 合并进 survivor（保留 survivor），在一个事务里完成：

        1. duplicate 的别名搬到 survivor（去重）；
        2. duplicate 的出处（mentions）搬到 survivor；
        3. 指向 duplicate 的关系外键改指 survivor；
           自指/重复关系删除，避免破坏 relations 唯一约束；
        4. duplicate 的标准名作为 survivor 的别名保留（不丢叫法）；
        5. 删除 duplicate。
        """
        if survivor_id == duplicate_id:
            return
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                dup = await conn.fetchrow(
                    "SELECT name FROM entities WHERE id=$1", duplicate_id)
                if dup is None:
                    return  # 已被合并/删除，幂等返回

                # 1) 别名迁移（含标准名本身）
                await conn.execute(
                    "INSERT INTO entity_aliases(entity_id, alias) "
                    "SELECT $1, alias FROM entity_aliases WHERE entity_id=$2 "
                    "ON CONFLICT DO NOTHING",
                    survivor_id, duplicate_id,
                )
                await conn.execute(
                    "INSERT INTO entity_aliases(entity_id, alias) VALUES ($1,$2) "
                    "ON CONFLICT DO NOTHING", survivor_id, dup["name"],
                )

                # 2) 出处迁移
                await conn.execute(
                    "UPDATE entity_mentions SET entity_id=$1 WHERE entity_id=$2",
                    survivor_id, duplicate_id,
                )

                # 3) 关系外键改指；先删会变成自指的关系
                await conn.execute(
                    "DELETE FROM relations WHERE source_entity_id=$2 AND target_entity_id=$1 "
                    "OR source_entity_id=$1 AND target_entity_id=$2",
                    survivor_id, duplicate_id,
                )
                # 与 survivor 既有关系重复的，删除旧重复行
                await conn.execute(
                    """
                    DELETE FROM relations rdup USING relations rkeep
                    WHERE rdup.target_entity_id=$2 AND rkeep.target_entity_id=$1
                      AND rdup.source_entity_id=rkeep.source_entity_id
                      AND rdup.predicate=rkeep.predicate AND rdup.domain_code=rkeep.domain_code
                    """,
                    survivor_id, duplicate_id,
                )
                await conn.execute(
                    """
                    DELETE FROM relations rdup USING relations rkeep
                    WHERE rdup.source_entity_id=$2 AND rkeep.source_entity_id=$1
                      AND rdup.target_entity_id=rkeep.target_entity_id
                      AND rdup.predicate=rkeep.predicate AND rdup.domain_code=rkeep.domain_code
                    """,
                    survivor_id, duplicate_id,
                )
                await conn.execute(
                    "UPDATE relations SET source_entity_id=$1 WHERE source_entity_id=$2",
                    survivor_id, duplicate_id,
                )
                await conn.execute(
                    "UPDATE relations SET target_entity_id=$1 WHERE target_entity_id=$2",
                    survivor_id, duplicate_id,
                )

                # 5) 删 duplicate（级联清理其残留别名等）
                await conn.execute("DELETE FROM entities WHERE id=$1", duplicate_id)

    # ------------------------------------------------------------------
    # 人工审核队列（L09 HITL）
    # ------------------------------------------------------------------
    async def enqueue_review(
        self, *, survivor_id: int, duplicate_id: int,
        survivor_name: str, duplicate_name: str,
        similarity: float, reason: str = "",
        domain_code: str = "general", document_id: str | None = None,
    ) -> int:
        """登记一条待人工裁决的合并建议；同一对实体只入队一次。"""
        row = await self.pool.fetchrow(
            """
            INSERT INTO merge_reviews
                (domain_code, document_id, survivor_id, duplicate_id,
                 survivor_name, duplicate_name, similarity, reason, status)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'pending')
            ON CONFLICT DO NOTHING
            RETURNING id
            """,
            domain_code, document_id, survivor_id, duplicate_id,
            survivor_name, duplicate_name, similarity, reason,
        )
        # 已存在待审记录时取其 id
        if row is None:
            return await self.pool.fetchval(
                "SELECT id FROM merge_reviews WHERE survivor_id=$1 AND duplicate_id=$2 "
                "AND status='pending' ORDER BY id DESC LIMIT 1",
                survivor_id, duplicate_id,
            )
        return row["id"]

    async def pending_reviews(self, limit: int = 50) -> list[dict]:
        rows = await self.pool.fetch(
            "SELECT * FROM merge_reviews WHERE status='pending' "
            "ORDER BY similarity DESC, id LIMIT $1", limit,
        )
        return [dict(r) for r in rows]

    async def decide_review(self, review_id: int, approved: bool) -> bool:
        """人工裁决：approved=True 则执行合并；无论结果都把队列项置终态。"""
        row = await self.pool.fetchrow(
            "SELECT * FROM merge_reviews WHERE id=$1 AND status='pending'", review_id)
        if row is None:
            return False
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                if approved:
                    await self.merge_entities(row["survivor_id"], row["duplicate_id"])
                await conn.execute(
                    "UPDATE merge_reviews SET status=$1, decided_at=now() WHERE id=$2",
                    "approved" if approved else "rejected", review_id,
                )
        return True
