"""按领域 code 提供抽取函数（接 DB 热更配置）。

DynamicExtractorProvider 每次接到一个 domain_code：
  1) 经 resolver 拿到【最新】DomainSpec（配置表改了立即反映）；
  2) 用 (code, 配置版本) 作键缓存 Extractor——只有领域配置真的变化时才重建
     动态结构化模型与系统提示词，平时零额外开销；
  3) 返回符合 run_extraction_team 约定的 async (text, feedback)->ExtractionResult。

这样 /extract 与 worker 都能在【不重启】的情况下响应运营对领域属性的增改。
"""

from __future__ import annotations

from ..config_center.resolver import DbDomainResolver
from .document import Document
from .extractor import Extractor


class DynamicExtractorProvider:
    def __init__(
        self,
        resolver: DbDomainResolver,
        *,
        max_retries: int = 2,
        middlewares: list | None = None,
    ) -> None:
        self.resolver = resolver
        self.max_retries = max_retries
        self.middlewares = middlewares
        # (code, 版本) -> (Extractor, 领域)
        self._cache: dict[tuple[str, int], Extractor] = {}

    async def get_extractor(self, domain_code: str) -> tuple[Extractor, object]:
        domain, version = await self.resolver.resolve_with_version(domain_code)
        key = (domain.code, version)
        extractor = self._cache.get(key)
        if extractor is None:
            extractor = Extractor(
                domain, max_retries=self.max_retries,
                middlewares=self.middlewares)
            self._cache[key] = extractor
            # 淘汰该 code 的旧版本，避免配置频繁变更时缓存无限增长
            for old_key in [k for k in self._cache
                            if k[0] == domain.code and k != key]:
                self._cache.pop(old_key, None)
        return extractor, domain

    async def extract(self, domain_code: str):
        """返回一个绑定该领域的 async (text, feedback)->ExtractionResult。"""
        extractor, _domain = await self.get_extractor(domain_code)

        async def _fn(text: str, feedback: str | None = None):
            body = text
            if feedback:
                body = text + "\n\n【上次问题，请务必修正】\n" + feedback
            out = await extractor.extract(
                Document(id=f"{domain_code}-dyn", text=body,
                         source="dynamic", domain_code=domain_code))
            return out.result

        return _fn
