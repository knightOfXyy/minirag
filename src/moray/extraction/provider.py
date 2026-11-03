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
from .document import Document, Usage
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

    async def extract(
        self,
        domain_code: str,
        usage_sink: Usage | None = None,
        metrics_sink: list | None = None,
        backfill_sink: dict | None = None,
    ):
        """返回一个绑定该领域的 async (text, feedback)->ExtractionResult。

        usage_sink: 可选的 Usage 累加器。抽取函数契约只返回 ExtractionResult，
        token 用量（含提示缓存命中）会在这一层被丢掉；给上层一个"顺手收集"的
        出口，/extract 与 worker 才能把成本和缓存命中率写进看板。

        backfill_sink: 可选字典。瘦身回填的确定性信号同样不在
        ExtractionResult 里；调用方传一个空 dict，本层把最后一次抽取的
        ``out.backfill`` 写进 ``backfill_sink["backfill"]``，避免主链丢信号。

        metrics_sink: 可选的 ChunkCallMetrics 列表。短文档（不分块）路径需要
        把每次调用的 finish_reason/截断信号带出去，否则 length 只留一条日志。
        """
        extractor, _domain = await self.get_extractor(domain_code)

        async def _fn(text: str, feedback: str | None = None):
            body = text
            if feedback:
                # 反馈追加在正文之后：前缀（system + 引导语 + 正文）保持不变，
                # 返工轮因此能继续命中上一轮的提示缓存。
                body = text + "\n\n【上次问题，请务必修正】\n" + feedback
            out = await extractor.extract(
                Document(id=f"{domain_code}-dyn", text=body,
                         source="dynamic", domain_code=domain_code))
            if usage_sink is not None:
                usage_sink.add(out.usage)
            if backfill_sink is not None:
                backfill_sink["backfill"] = out.backfill
            if metrics_sink is not None and out.call_metrics is not None:
                metrics_sink.append(out.call_metrics)
            return out.result

        return _fn
