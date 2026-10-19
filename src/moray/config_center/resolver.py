"""热更领域解析器：把 DB 配置行变成 DomainSpec，带版本缓存。

为什么需要缓存而不是每次抽取都查表：一次批量可能有成百上千篇文档，若每个
实体/每次请求都打一次 SQL 取配置，纯属浪费。策略：

  · 进程内缓存每个 code 的 (updated_at 版本, DomainSpec)；
  · resolve() 只查一行的 updated_at（极便宜的主键查询）；
  · 版本没变就用缓存的 DomainSpec，变了才重新解析 spec JSONB。

管理端 UPDATE domain_configs 后 updated_at 变化，所有抽取进程下一次 resolve
即拿到新配置——这就是"热更"，进程和 worker 都不用重启。
"""

from __future__ import annotations

import asyncpg

from ..schema import DomainSpec, domain_from_dict, get_domain
from .domain_store import DomainConfigStore


class DbDomainResolver:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.store = DomainConfigStore(pool)
        # code -> (updated_at 浮点秒版本, DomainSpec)
        self._cache: dict[str, tuple[float, DomainSpec]] = {}

    async def init_schema(self) -> None:
        await self.store.init_schema()

    async def resolve(self, code: str) -> DomainSpec:
        """取该 code 的最新领域配置；DB 没有则回退内置常量。"""
        domain, _ = await self.resolve_with_version(code)
        return domain

    async def resolve_with_version(self, code: str) -> tuple[DomainSpec, float]:
        """同 resolve，额外返回配置版本（updated_at 浮点秒；内置常量为 0.0）。

        调用方用版本判断"配置是否变化"，决定是否需要重建派生对象（如抽取器）。
        """
        row = await self.store.get_raw(code)
        if row is None or not row["enabled"]:
            return get_domain(code), 0.0  # 表中没有/被禁用：回退内置

        version = row["updated_at"].timestamp()  # 浮点秒，作为版本指纹
        cached = self._cache.get(code)
        if cached is not None and cached[0] == version:
            return cached[1], version

        domain = domain_from_dict(row["spec"])
        self._cache[code] = (version, domain)
        return domain, version

    def invalidate(self, code: str | None = None) -> None:
        """主动清缓存（测试或同进程刚改完配置想立刻生效时用）。"""
        if code is None:
            self._cache.clear()
        else:
            self._cache.pop(code, None)

    async def list_codes(self) -> list[str]:
        return await self.store.list_codes()
