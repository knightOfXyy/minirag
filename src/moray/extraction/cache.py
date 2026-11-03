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
    个结果；支持占位的持久后端（PostgreSQL）还做跨进程单飞——先写占位行的
    一方是 leader，其余 worker 等它回写后直接命中，避免缓存击穿式重复调用；
  · 后端可换且必须容错：默认进程内后端零外部依赖；后端损坏/不可读时自动降级为
    正常抽取，不抛给用户、不丢结果。

跨进程单飞是 best-effort：占位行带租约，leader 抽取期间定期续期；只有租约
过期（leader 崩溃/卡死）其它 worker 才接管，健康但慢的 leader 不会被抢。
等待上限由 ``remote_wait_seconds`` 控制（默认 300s，可用
``MORAY_RESULT_CACHE_REMOTE_WAIT_SECONDS`` 调整）：正常情况下要么命中，
要么在一个租约（默认 30s）内接管，不会让等待方付满整个窗口。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections import OrderedDict
from typing import Awaitable, Callable, Protocol

from .document import ExtractionOutput

logger = logging.getLogger(__name__)

# 载荷格式版本：一旦序列化形态不兼容可整体换命名空间，避免读到旧格式脏数据。
_PAYLOAD_VERSION = 1

# 提示模板/生成参数版本：domain_fingerprint 覆盖 system prompt + schema，但不含
# user 消息前缀（Extractor._LEAD_IN）与采样参数。改这些影响输出的模板要素时必须
# 递增此版本，让旧缓存整体失效（N11）。
_PROMPT_TEMPLATE_VERSION = 1

# 内存后端默认上限：长跑 worker 的进程内存不能只增不减（简单 LRU）。
_DEFAULT_MEMORY_ENTRIES = 2048

# 跨进程单飞默认等待窗口（秒）。必须明显大于 p99 单块真实抽取耗时：
# 推理模型 + 校验重试的单块可能远超 1 分钟，窗口太短会让健康 leader 也
# 被等待方“超时自抽”，退化成两次真实调用（S2）。可用
# MORAY_RESULT_CACHE_REMOTE_WAIT_SECONDS 调整。
DEFAULT_REMOTE_WAIT_SECONDS = 300.0

# 占位行租约（秒）：leader 抽取期间定期续期；只有租约过期（leader 崩溃/
# 卡死）才允许其它 worker 抢占。健康但慢的 leader 不会被抢，死掉的 leader
# 也能在一个租约内被接管，而不是让每个等待方付满整个等待窗口（S2）。
DEFAULT_CLAIM_LEASE_SECONDS = 30.0


# 缓存后端要实现的最小契约。value 一律是字符串（JSON），后端不理解业务结构，
# 这样文件/内存/PostgreSQL 后端可以平替。
class CacheBackend(Protocol):
    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str) -> None: ...


# 可选能力：支持跨进程单飞的后端用空载荷行占位（leader 令牌）。只有实现
# 这个 Protocol 的后端才会启用跨进程单飞；内存后端不实现，契约自洽（N6）。
class ClaimableBackend(Protocol):
    async def try_claim(self, key: str, lease_seconds: float) -> bool: ...

    async def renew_claim(self, key: str, lease_seconds: float) -> bool: ...

    async def release(self, key: str) -> None: ...


def build_cache_key(domain_fingerprint: str, model_name: str, chunk_text: str) -> str:
    """以全部影响输出的输入要素拼键：领域指纹 + 模型 + 正文 + 模板版本。

    用真正的长度前缀（``len:value|``）拼装，避免字段边界被内容含分隔符时
    拼成同一字符串；正文取 SHA-256，键长固定且与正文长度无关。
    """
    body = hashlib.sha256(chunk_text.encode("utf-8")).hexdigest()
    parts = (
        f"v{_PAYLOAD_VERSION}",
        f"t{_PROMPT_TEMPLATE_VERSION}",
        f"fp={domain_fingerprint}",
        f"m={model_name}",
        f"sha={body}",
    )
    raw = "|".join(f"{len(p)}:{p}" for p in parts)
    return "chunk:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


class InMemoryCacheBackend:
    """零外部依赖的进程内后端：默认值，离线测试与单进程 worker 都可用。

    ``max_entries`` 是简单 LRU 上限：命中/写入都会把键提到最近使用，超过上限
    淘汰最久未用的条目，避免长跑进程内存无限增长。
    """

    def __init__(self, max_entries: int = _DEFAULT_MEMORY_ENTRIES) -> None:
        self.max_entries = max(1, int(max_entries))
        self._store: OrderedDict[str, str] = OrderedDict()

    async def get(self, key: str) -> str | None:
        value = self._store.get(key)
        if value is not None:
            self._store.move_to_end(key)
        return value

    async def set(self, key: str, value: str) -> None:
        self._store[key] = value
        self._store.move_to_end(key)
        while len(self._store) > self.max_entries:
            self._store.popitem(last=False)


class ContentResultCache:
    """以内容为键的分块抽取结果缓存 + 单飞去重。"""

    def __init__(
        self,
        backend: CacheBackend | None = None,
        *,
        remote_wait_seconds: float = DEFAULT_REMOTE_WAIT_SECONDS,
        poll_seconds: float = 0.05,
        claim_lease_seconds: float = DEFAULT_CLAIM_LEASE_SECONDS,
        max_poll_seconds: float = 0.5,
    ) -> None:
        self.backend: CacheBackend = backend or InMemoryCacheBackend()
        # 键 -> 进行中的抽取。后到者直接 await 先到者的 future（单飞）。
        self._inflight: dict[str, asyncio.Future] = {}
        # 键 -> 本地等待者数量：只有确实有人在等时才给 future set_exception。
        self._waiters: dict[str, int] = {}
        self._inflight_lock = asyncio.Lock()
        # 跨进程单飞的最长等待 / 轮询间隔（leader 崩溃后超时自抽，不永久卡死）。
        self.remote_wait_seconds = remote_wait_seconds
        self.poll_seconds = poll_seconds
        # 指数退避上限：慢 leader 期间不能每 50ms 打一条 SELECT（N9）。
        self.max_poll_seconds = max(poll_seconds, max_poll_seconds)
        self.claim_lease_seconds = claim_lease_seconds

    async def init_schema(self) -> None:
        """持久后端建表（DDL 归后端自己，config 只负责选后端）。"""
        init = getattr(self.backend, "init_schema", None)
        if init is not None:
            await init()

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

        # ① 锁外先读后端：postgres 的 SELECT 会真正让出事件循环，绝不能塞进
        # 全局锁，否则 chunk_concurrency 会被串行化（S5）。
        cached = await self._safe_get(key)
        if cached is not None:
            return cached, True

        # ② 锁只保护进程内 _inflight 字典的“查/登记”：同键并发只登记一个 leader。
        async with self._inflight_lock:
            existing = self._inflight.get(key)
            if existing is not None:
                self._waiters[key] = self._waiters.get(key, 0) + 1
                future = existing
                is_leader = False
            else:
                future = asyncio.get_running_loop().create_future()
                self._inflight[key] = future
                self._waiters[key] = 0
                is_leader = True

        if not is_leader:
            # 命中返回独立副本，与后端命中路径语义一致（N1）：等待方/调用方
            # 改写自己的结果不会污染 leader 或缓存。
            return (await future).model_copy(deep=True), True

        # ③ leader 锁外二次确认：覆盖“首读与登记之间别的协程刚写完”这一窗口。
        cached = await self._safe_get(key)
        if cached is not None:
            self._finish_inflight(key, cached)
            return cached, True

        # ④ 支持跨进程占位的后端：抢到占位才是 leader，没抢到就等对方的载荷。
        claimable = getattr(self.backend, "try_claim", None) is not None
        if claimable:
            if not await self._safe_claim(key):
                cached = await self._wait_remote(key)
                if cached is not None:
                    self._finish_inflight(key, cached)
                    return cached, True
                # 等待超时/对方失败：_wait_remote 已尽量重新占位，自己抽。

        # 本协程是这一键的“抽取者”：真正调用模型，结束后唤醒等待者并回写。
        renewer = asyncio.create_task(self._renew_loop(key)) if claimable else None
        try:
            output = await extract()
        except BaseException as exc:
            self._fail_inflight(key, exc)
            if claimable:
                await self._safe_release(key)
            raise
        finally:
            if renewer is not None:
                renewer.cancel()
                try:
                    await renewer
                except asyncio.CancelledError:
                    pass
        await self._safe_set(key, output)
        self._finish_inflight(key, output)
        return output, False

    async def has_cached(
        self,
        domain_fingerprint: str,
        model_name: str,
        chunk_text: str,
    ) -> bool:
        """该分块是否已有可用的缓存结果（只读探测，供预热前判断全命中）。"""
        return await self._safe_get(
            build_cache_key(domain_fingerprint, model_name, chunk_text)) is not None

    # -------------------------------------------------- 后端容错读写
    async def _safe_get(self, key: str) -> ExtractionOutput | None:
        try:
            raw = await self.backend.get(key)
        except Exception as exc:  # noqa: BLE001 后端不可读 → 降级
            logger.warning("抽取缓存读取失败，降级为正常抽取 key=%s: %r", key[:16], exc)
            return None
        return self._decode(key, raw)

    def _decode(self, key: str, raw) -> ExtractionOutput | None:
        # 空字符串是跨进程单飞的“占位行”（leader 正在抽），不是缓存值。
        if raw is None or raw == "":
            return None
        try:
            # 后端返回不透明 JSON 文本（内存/PostgreSQL TEXT 列都是字符串）。
            # 保留对 dict 的兼容，便于将来直接挂返回已解码对象的后端。
            payload = json.loads(raw) if isinstance(raw, str) else raw
            return ExtractionOutput.model_validate(payload["output"])
        except Exception as exc:  # noqa: BLE001 脏数据/旧格式 → 当未命中
            logger.warning("抽取缓存载荷损坏，按未命中处理 key=%s: %r", key[:16], exc)
            return None

    async def _safe_claim(self, key: str) -> bool:
        """尝试抢占跨进程 leader 占位；后端不可用时退回本进程直接抽取。

        backend 只在占位行【不存在或租约已过期】时才会把 leader 让给我们，
        健康但慢的 leader 只要还在续租就不会被抢（S2）。
        """
        try:
            return bool(await self.backend.try_claim(key, self.claim_lease_seconds))
        except Exception as exc:  # noqa: BLE001 占位失败按“我来抽”处理
            logger.warning("抽取缓存占位失败，本进程直接抽取 key=%s: %r", key[:16], exc)
            return True

    async def _safe_renew(self, key: str) -> bool:
        try:
            return bool(await self.backend.renew_claim(key, self.claim_lease_seconds))
        except Exception as exc:  # noqa: BLE001 续租失败只影响被接管时机
            logger.warning("抽取缓存占位续租失败（忽略）key=%s: %r", key[:16], exc)
            return False

    async def _renew_loop(self, key: str) -> None:
        """leader 抽取期间按租约 1/3 续期，避免慢但健康的抽取被等待方接管。"""
        interval = max(0.05, self.claim_lease_seconds / 3.0)
        while True:
            await asyncio.sleep(interval)
            if not await self._safe_renew(key):
                # 占位已丢（被接管/已回写）：停止续租，避免干扰新 leader。
                return

    async def _safe_release(self, key: str) -> None:
        try:
            await self.backend.release(key)
        except Exception as exc:  # noqa: BLE001 释放失败只会多占一行，租约到期后可覆盖
            logger.warning("抽取缓存占位释放失败（忽略）key=%s: %r", key[:16], exc)

    async def _wait_remote(self, key: str) -> ExtractionOutput | None:
        """等另一个 worker 回写载荷；租约过期或超时后重新抢占地盘。

        返回 None 表示“轮到我抽”（已尽量抢到新的占位），由调用方继续 extract。
        轮询按指数退避（N9），不会在慢 leader 期间打出上千条 SELECT。
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.remote_wait_seconds
        delay = self.poll_seconds
        last_takeover = loop.time()
        while loop.time() < deadline:
            try:
                raw = await self.backend.get(key)
            except Exception as exc:  # noqa: BLE001 后端不可读 → 自己抽
                logger.warning("等待跨进程缓存时读取失败，改为自行抽取 key=%s: %r",
                               key[:16], exc)
                return None
            if raw is None:
                # 占位行消失 = leader 失败并释放；立刻重新抢，早于超时恢复。
                if await self._safe_claim(key):
                    return None
            elif raw != "":
                cached = self._decode(key, raw)
                if cached is not None:
                    return cached
                # 载荷损坏：立刻自己重抽并由 set 覆盖（占位行已存在，抢不到）。
                return None
            else:
                # 占位行还在：只有经过一个完整租约后才尝试接管。健康 leader 会
                # 续租，原子抢占会失败；崩溃 leader 的租约过期后等待方立刻接手，
                # 不必付满整个 remote_wait_seconds（S2）。
                if loop.time() - last_takeover >= self.claim_lease_seconds:
                    last_takeover = loop.time()
                    if await self._safe_claim(key):
                        return None
            await asyncio.sleep(delay)
            delay = min(delay * 2, self.max_poll_seconds)
        # 等太久（leader 卡死且租约异常）：尽量重新抢占地盘后自己抽，绝不永久卡住。
        await self._safe_claim(key)
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
        self._waiters.pop(key, None)
        if future is not None and not future.done():
            future.set_result(output)

    def _fail_inflight(self, key: str, exc: BaseException) -> None:
        future = self._inflight.pop(key, None)
        waiters = self._waiters.pop(key, 0)
        if future is None or future.done():
            return
        if waiters > 0:
            # 有等待者会 await 并取走异常，不会触发 unhandled future 告警。
            future.set_exception(exc)
        else:
            # leader 自己直接 raise，没人会取这个 future 的异常；cancel 掉，
            # 避免 GC 时打 "Future exception was never retrieved"（S6）。
            future.cancel()


# 复用 PostgreSQL 作业库时的表结构：与 extraction_jobs 同库，不引入新服务。
# payload 为空字符串表示“跨进程单飞占位行”：leader 正在抽，内容尚未回写。
# expires_at 是占位行租约：leader 抽取期间定期续期，过期才允许其它 worker 抢占。
_POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS extraction_result_cache (
    cache_key   TEXT PRIMARY KEY,
    payload     TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ
);
ALTER TABLE extraction_result_cache
    ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_result_cache_created
    ON extraction_result_cache(created_at);
"""

# 占位行（leader 已抢到、payload 待回写）用空字符串表示。
_CLAIM_PAYLOAD = ""

# 过期清理的最小间隔（秒）：每块写入都 DELETE 一次是浪费（N7）。
_DEFAULT_PURGE_INTERVAL_SECONDS = 60.0


class PostgresCacheBackend:
    """跨 worker 进程共享的持久后端，复用作业/知识库同一个 asyncpg 连接池。

    纯 best-effort：表不存在/读写异常一律向上抛出由 ContentResultCache 当作
    未命中降级（本类不吞业务抽取异常，只做存取）。

    跨进程单飞：``try_claim`` 用 ``INSERT ... ON CONFLICT DO UPDATE ... WHERE``
    原子抢占位，只有在占位行不存在或租约过期时才成功；leader 抽取期间
    ``renew_claim`` 定期续期。没抢到的 worker 轮询 payload，等 leader 回写后
    直接命中。leader 失败/崩溃时 ``release`` 删占位（或租约自然过期），等待方
    会重新抢占位，至多多一次真实调用，绝不脏数据。``set`` 是后写覆盖：同一键
    的两次抽取语义等价，覆盖无害，且能顺手修掉损坏载荷。
    """

    def __init__(self, pool, *, ttl_hours: int = 0,
                 purge_interval_seconds: float = _DEFAULT_PURGE_INTERVAL_SECONDS) -> None:
        self.pool = pool
        # 0 = 不清理；>0 时写入顺带删除过期行（N6），但两次清理至少间隔
        # purge_interval_seconds（N7），避免一篇 30 块文档打出 30 条 DELETE。
        self.ttl_hours = max(0, int(ttl_hours))
        self.purge_interval_seconds = max(0.0, float(purge_interval_seconds))
        self._last_purge: float | None = None

    async def init_schema(self) -> None:
        await self.pool.execute(_POSTGRES_SCHEMA)

    async def get(self, key: str) -> str | None:
        return await self.pool.fetchval(
            "SELECT payload FROM extraction_result_cache WHERE cache_key=$1",
            key)

    async def set(self, key: str, value: str) -> None:
        # 载荷是不透明 JSON 文本：PG 不解析结构，读写都不做二次转义；
        # 合法性在 ContentResultCache 读取时校验，损坏按未命中降级。
        # 回写真实载荷即清掉租约（不再是占位行）。
        await self.pool.execute(
            """
            INSERT INTO extraction_result_cache (cache_key, payload, expires_at)
            VALUES ($1, $2, NULL)
            ON CONFLICT (cache_key) DO UPDATE
              SET payload = EXCLUDED.payload,
                  expires_at = NULL
            """,
            key, value)
        await self.purge_expired()

    async def try_claim(self, key: str, lease_seconds: float) -> bool:
        """原子抢占位；返回 True 表示本进程成为该键的 leader。

        只有占位行不存在、或它的租约已过期（leader 崩溃/卡死）时才抢占成功。
        健康但慢的 leader 会续租，其它 worker 不会把它顶掉（S2）。
        """
        row = await self.pool.fetchval(
            """
            INSERT INTO extraction_result_cache (cache_key, payload, expires_at)
            VALUES ($1, $2, now() + make_interval(secs => $3::double precision))
            ON CONFLICT (cache_key) DO UPDATE
              SET expires_at = EXCLUDED.expires_at
              WHERE extraction_result_cache.payload = $2
                AND (extraction_result_cache.expires_at IS NULL
                     OR extraction_result_cache.expires_at < now())
            RETURNING cache_key
            """,
            key, _CLAIM_PAYLOAD, float(lease_seconds))
        return row is not None

    async def renew_claim(self, key: str, lease_seconds: float) -> bool:
        """续期占位行租约；占位已被回写或已被接管时返回 False。"""
        row = await self.pool.fetchval(
            """
            UPDATE extraction_result_cache
               SET expires_at = now() + make_interval(secs => $2::double precision)
             WHERE cache_key = $1 AND payload = $3
            RETURNING cache_key
            """,
            key, float(lease_seconds), _CLAIM_PAYLOAD)
        return row is not None

    async def release(self, key: str) -> None:
        """leader 失败时释放占位；只删空载荷行，绝不误删已回写的真实结果。"""
        await self.pool.execute(
            "DELETE FROM extraction_result_cache "
            "WHERE cache_key=$1 AND payload=$2",
            key, _CLAIM_PAYLOAD)

    async def purge_expired(self) -> None:
        if self.ttl_hours <= 0:
            return
        loop = asyncio.get_running_loop()
        now = loop.time()
        if (self._last_purge is not None
                and now - self._last_purge < self.purge_interval_seconds):
            return
        self._last_purge = now
        await self.pool.execute(
            "DELETE FROM extraction_result_cache "
            "WHERE created_at < now() - ($1::text || ' hours')::interval",
            str(self.ttl_hours))
