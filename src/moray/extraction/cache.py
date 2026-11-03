"""分块抽取结果的内容级缓存（纯加速层）
========================================

作业至少一次投递、可断点续跑：文档改版重抽、失败重试、同一文档重复提交时，
相同的分块会被反复送进 LLM。本模块在【分块抽取】这一步之前加一层以内容为键
的缓存：

    缓存键 = 领域配置指纹 + 模型名 + 分块正文

三者任一变化（改了系统提示/schema、切了模型、改正文一个字）都会换键 → 缓存
自然失效，不需要显式作废。命中时直接还原上一次的 ExtractionOutput，不发起任何
模型调用。

设计约束（对应工单 05 验收）：
  · 纯加速层：缓存的是【块内】抽取产物，偏移换算（remap_spans）与归并
    （naive_merge）仍在缓存之外照常进行，结果语义逐字节不变；
  · 命中不是真实 LLM 调用：不产生 token，调用方据 hit 标记把用量记成
    「缓存来源」，绝不计入真实 token 成本；
  · 单飞（single-flight）：同进程内同键并发只允许一次真实抽取，其余协程等同一
    个结果，避免缓存击穿式重复调用；
  · 后端可换且必须容错：默认进程内后端零外部依赖；后端损坏/不可读时自动降级为
    正常抽取，不抛给用户、不丢结果。

跨进程共享（多个 worker 副本）由后端实现负责：进程内后端只在单进程内单飞，
持久后端（如复用 PostgreSQL）可在 upsert 层做去重。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Awaitable, Callable, Protocol

from .document import ExtractionOutput

logger = logging.getLogger(__name__)

# 载荷格式版本：一旦序列化形态不兼容可整体换命名空间，避免读到旧格式脏数据。
_PAYLOAD_VERSION = 1

# 缓存后端要实现的最小契约。value 一律是字符串（JSON），后端不理解业务结构，
# 这样文件/内存/PostgreSQL 后端可以平替。
class CacheBackend(Protocol):
    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str) -> None: ...


def build_cache_key(domain_fingerprint: str, model_name: str, chunk_text: str) -> str:
    """以全部影响输出的输入要素拼键：领域配置指纹 + 模型 + 正文。

    用带长度前缀的分隔拼法，避免两段拼成同一字符串造成的歧义碰撞；正文取
    SHA-256，键长固定且与正文长度无关。
    """
    body = hashlib.sha256(chunk_text.encode("utf-8")).hexdigest()
    raw = f"v{_PAYLOAD_VERSION}\x1dfp={domain_fingerprint}\x1dm={model_name}\x1dsha={body}"
    return "chunk:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


class InMemoryCacheBackend:
    """零外部依赖的进程内后端：默认值，离线测试与单进程 worker 都可用。"""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self._store.get(key)

    async def set(self, key: str, value: str) -> None:
        self._store[key] = value


class ContentResultCache:
    """以内容为键的分块抽取结果缓存 + 单飞去重。"""

    def __init__(self, backend: CacheBackend | None = None) -> None:
        self.backend: CacheBackend = backend or InMemoryCacheBackend()
        # 键 -> 进行中的抽取。后到者直接 await 先到者的 future（单飞）。
        self._inflight: dict[str, asyncio.Future] = {}
        self._inflight_lock = asyncio.Lock()

    async def get_or_extract(
        self,
        domain_fingerprint: str,
        model_name: str,
        chunk_text: str,
        extract: Callable[[], Awaitable[ExtractionOutput]],
    ) -> tuple[ExtractionOutput, bool]:
        """命中返回 (缓存结果, True)；未命中跑一次 extract 并回写，返回 (结果, False)。

        后端读失败/载荷损坏一律按未命中处理；写失败只记日志：缓存是优化，
        绝不能因为它影响真实抽取。
        """
        key = build_cache_key(domain_fingerprint, model_name, chunk_text)

        # 单飞关键区：在【同一把锁】内决定“命中 / 加入在途 / 成为抽取者”。
        # 必须把后端读也放进锁内——内存后端的 get 不会让出事件循环，若先读后登记，
        # 一批并发协程会在任何一个真正发起抽取前各自登记成抽取者，各抽一次。
        async with self._inflight_lock:
            cached = await self._safe_get(key)
            if cached is not None:
                return cached, True
            existing = self._inflight.get(key)
            if existing is not None:
                future = existing
                is_leader = False
            else:
                future = asyncio.get_running_loop().create_future()
                self._inflight[key] = future
                is_leader = True

        if not is_leader:
            return await future, True

        # 本协程是这一键的“抽取者”：真正调用模型，结束后唤醒等待者并回写。
        try:
            output = await extract()
        except BaseException as exc:
            self._fail_inflight(key, exc)
            raise
        await self._safe_set(key, output)
        self._finish_inflight(key, output)
        return output, False

    # -------------------------------------------------- 后端容错读写
    async def _safe_get(self, key: str) -> ExtractionOutput | None:
        try:
            raw = await self.backend.get(key)
        except Exception as exc:  # noqa: BLE001 后端不可读 → 降级
            logger.warning("抽取缓存读取失败，降级为正常抽取 key=%s: %r", key[:16], exc)
            return None
        if raw is None:
            return None
        try:
            # 后端返回不透明 JSON 文本（内存/PostgreSQL TEXT 列都是字符串）。
            # 保留对 dict 的兼容，便于将来直接挂返回已解码对象的后端。
            payload = json.loads(raw) if isinstance(raw, str) else raw
            return ExtractionOutput.model_validate(payload["output"])
        except Exception as exc:  # noqa: BLE001 脏数据/旧格式 → 当未命中
            logger.warning("抽取缓存载荷损坏，按未命中处理 key=%s: %r", key[:16], exc)
            return None

    async def _safe_set(self, key: str, output: ExtractionOutput) -> None:
        try:
            raw = json.dumps(
                {"v": _PAYLOAD_VERSION,
                 "output": output.model_dump(mode="json")},
                ensure_ascii=False)
            await self.backend.set(key, raw)
        except Exception as exc:  # noqa: BLE001 写失败不影响本次结果
            logger.warning("抽取缓存写入失败（忽略）key=%s: %r", key[:16], exc)

    # -------------------------------------------------- 单飞原语
    def _finish_inflight(self, key: str, output: ExtractionOutput) -> None:
        future = self._inflight.pop(key, None)
        if future is not None and not future.done():
            future.set_result(output)

    def _fail_inflight(self, key: str, exc: BaseException) -> None:
        future = self._inflight.pop(key, None)
        if future is not None and not future.done():
            future.set_exception(exc)


# 复用 PostgreSQL 作业库时的表结构：与 extraction_jobs 同库，不引入新服务。
_POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS extraction_result_cache (
    cache_key   TEXT PRIMARY KEY,
    payload     TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


class PostgresCacheBackend:
    """跨 worker 进程共享的持久后端，复用作业/知识库同一个 asyncpg 连接池。

    纯 best-effort：表不存在/读写异常一律向上抛出由 ContentResultCache 当作
    未命中降级（本类不吞业务抽取异常，只做存取）。跨进程的强一致单飞不在此
    保证（单进程内由 ContentResultCache 保证）；INSERT ON CONFLICT 保证并发
    写同键不报错、以先到者为准，至多多一次真实调用，绝不脏数据。
    """

    def __init__(self, pool) -> None:
        self.pool = pool

    async def init_schema(self) -> None:
        await self.pool.execute(_POSTGRES_SCHEMA)

    async def get(self, key: str) -> str | None:
        return await self.pool.fetchval(
            "SELECT payload FROM extraction_result_cache WHERE cache_key=$1",
            key)

    async def set(self, key: str, value: str) -> None:
        # 载荷是不透明 JSON 文本：PG 不解析结构，读写都不做二次转义；
        # 合法性在 ContentResultCache 读取时校验，损坏按未命中降级。
        await self.pool.execute(
            """
            INSERT INTO extraction_result_cache (cache_key, payload)
            VALUES ($1, $2)
            ON CONFLICT (cache_key) DO UPDATE
              SET payload = EXCLUDED.payload
            """,
            key, value)
