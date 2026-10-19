"""领域配置的持久化仓库（PostgreSQL 配置中心）。

domain_configs 表是"单一事实源"：管理端改它，所有抽取进程经
DbDomainResolver 的版本缓存自动看到新配置（热更，无需重启）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import asyncpg

from ..schema import DOMAIN_REGISTRY, DomainSpec, domain_from_dict, domain_to_dict

_SCHEMA_SQL = Path(__file__).with_name("schema.sql")


class DomainConfigStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def init_schema(self) -> None:
        await self.pool.execute(_SCHEMA_SQL.read_text(encoding="utf-8"))

    async def upsert(self, domain: DomainSpec) -> None:
        """新增或更新一个领域包（更新即热更信号：updated_at 会变）。"""
        spec = domain_to_dict(domain)
        await self.pool.execute(
            """
            INSERT INTO domain_configs (code, name, spec, enabled, updated_at)
            VALUES ($1, $2, $3, TRUE, now())
            ON CONFLICT (code) DO UPDATE
              SET name=EXCLUDED.name, spec=EXCLUDED.spec,
                  enabled=TRUE, updated_at=now()
            """,
            domain.code, domain.name, spec,
        )

    async def get_raw(self, code: str) -> dict[str, Any] | None:
        return await self.pool.fetchrow(
            "SELECT spec, updated_at, enabled FROM domain_configs "
            "WHERE code=$1", code)

    async def list_codes(self, *, enabled_only: bool = True) -> list[str]:
        rows = await self.pool.fetch(
            "SELECT code FROM domain_configs"
            + (" WHERE enabled=TRUE" if enabled_only else "")
            + " ORDER BY code")
        return [r["code"] for r in rows]

    async def set_enabled(self, code: str, enabled: bool) -> bool:
        val = await self.pool.fetchval(
            "UPDATE domain_configs SET enabled=$2, updated_at=now() "
            "WHERE code=$1 RETURNING code", code, enabled)
        return val is not None

    async def seed_builtin(self, *, overwrite: bool = False) -> int:
        """把内置 general/finance 种入表（仅当不存在，除非 overwrite）。

        让系统开箱即有可热更的配置行；之后管理端可直接改表而不必先发版。
        """
        n = 0
        for domain in DOMAIN_REGISTRY.values():
            if overwrite:
                await self.upsert(domain)
                n += 1
                continue
            ok = await self.pool.fetchval(
                "INSERT INTO domain_configs (code, name, spec) "
                "VALUES ($1,$2,$3) ON CONFLICT (code) DO NOTHING RETURNING code",
                domain.code, domain.name, domain_to_dict(domain))
            if ok:
                n += 1
        return n
