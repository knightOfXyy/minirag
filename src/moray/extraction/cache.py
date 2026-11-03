"""分块抽取结果的内容级缓存（纯加速层）
========================================

作业至少一次投递、可断点续跑：文档改版重抽、失败重试、同一文档重复提交时，
相同的分块会被反复送进 LLM。本模块在【分块抽取】这一步之前加一层以内容为键
的缓存：

    缓存键 = 领域配置指纹 + 模型身份 + 分块正文

模型身份 = provider + base_url + 模型名（见 ``Extractor.model_identity``）。
三者任一变化（改了系统提示/schema、换 provider/网关/模型、改正文一个字）都会
换键 → 缓存自然失效，不需要显式作废。命中时直接还原上一次的
ExtractionOutput，不发起任何模型调用。

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
import uuid
from collections import OrderedDict
from typing import Awaitable, Callable, NamedTuple, Protocol, runtime_checkable

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

# 同键 leader 连续被取消时，等待方最多重试几次；超过说明该键的抽取一直在被
# 打断，继续重试只会叠加深层调用栈（blocker-1）。
_MAX_LEADER_CANCEL_RETRIES = 3


class _LeaderCancelled(Exception):
    """leader 自己被取消（不是本等待方被取消）的内部信号。

    共享 future 不能塞 ``CancelledError``：那会把同键所有健康等待方一起取消。
    等待方收到本信号后重新参与（可能由自己接手抽取）（blocker-1）。
    """


class _ClaimResult(NamedTuple):
    """抢占位的结果：owner 令牌 + 是否只是后端故障。

    后端故障时既没有令牌、也不该继续等别人（那不是“没抢到”，而是“问不到”），
    调用方应退化为“直接抽取但不持有占位”，失败时不 release 别人的占位。
    """

    owner: str | None = None
    backend_error: bool = False


# 缓存后端要实现的最小契约。value 一律是字符串（JSON），后端不理解业务结构，
# 这样文件/内存/PostgreSQL 后端可以平替。
class CacheBackend(Protocol):
    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str) -> None: ...


# 可选能力：支持跨进程单飞的后端用空载荷行占位（leader 令牌）。只有实现
# 这个 Protocol 的后端才会启用跨进程单飞；内存后端不实现，契约自洽（N6）。
# runtime_checkable 让运行期判定与导出的契约一致，而不是靠 getattr 猜方法名（nit-10）。
#
# ``owner`` 是每次抢到的 leader 令牌（uuid）。续租/释放都必须带上它，后端只在
# 令牌匹配时才生效：否则旧 leader 租约过期、新 leader 接管后，旧 leader 仍能
# 给新 leader 续租、或删掉新 leader 的占位，导致同一块第二次真实调用（should-1）。
@runtime_checkable
class ClaimableBackend(Protocol):
    async def try_claim(
        self, key: str, lease_seconds: float, owner: str) -> bool: ...

    async def renew_claim(
        self, key: str, lease_seconds: float, owner: str) -> bool: ...

    async def release(self, key: str, owner: str) -> None: ...


# 可选能力：按“读到的坏值”精确删除损坏载荷，让并发遇到同一坏行的 worker 只
# 重抽一次（nit-3）。claimable 后端应同时实现；缺失时 ContentResultCache 会
# 记 warning 并退化为“尽力占位”——两个 worker 可能各抽一次（nit-5）。
@runtime_checkable
class CorruptibleBackend(Protocol):
    async def discard(self, key: str, expected_value: str) -> None: ...


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
        return await self._get_or_extract(
            domain_fingerprint, model_name, chunk_text, extract, retries=0)

    async def _get_or_extract(
        self,
        domain_fingerprint: str,
        model_name: str,
        chunk_text: str,
        extract: Callable[[], Awaitable[ExtractionOutput]],
        *,
        retries: int,
    ) -> tuple[ExtractionOutput, bool]:
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
            #
            # shield：多个等待方共用同一个 future，不能直接 await——本协程被
            # 取消时 CPython 会连带取消它 await 的 future，导致同键其它等待方
            # 与后续到达者一起失败（blocker-1）。shield 只断开“本等待方取消”
            # 与共享 future 的联系。
            try:
                try:
                    return (await asyncio.shield(future)).model_copy(deep=True), True
                finally:
                    # 等待方被取消/异常退出时回退计数（nit-7）：否则 leader 失败会
                    # 给一个没人 await 的 future set_exception（日志告警）。
                    remaining = self._waiters.get(key, 0) - 1
                    if remaining > 0:
                        self._waiters[key] = remaining
                    else:
                        self._waiters.pop(key, None)
            except _LeaderCancelled:
                # leader 被取消（不是本等待方被取消）：重新参与该键，自己可能
                # 成为新 leader；绝不能把取消传播给调用方（blocker-1）。
                if retries >= _MAX_LEADER_CANCEL_RETRIES:
                    raise RuntimeError(
                        f"同键 leader 连续 {retries + 1} 次被取消，放弃等待 "
                        f"key={key[:16]}") from None
                return await self._get_or_extract(
                    domain_fingerprint, model_name, chunk_text, extract,
                    retries=retries + 1)

        # ③ 从这里开始本协程“拥有”该键的 _inflight。二次读、跨进程占位、等待、
        # 抽取、回写全部放进 try：这些 await 点任意一个被取消（uvicorn 关停/
        # 请求取消/外层 wait_for）都必须清理 future 与占位，否则同键后续请求
        # 会永久挂在 pending future 上（blocker-1）。
        claimable = getattr(self.backend, "try_claim", None) is not None
        # 只有 _safe_claim / _wait_remote 明确返回“我抢到了”才有 owner 令牌：
        # 等待方超时自抽失败时绝不能 release 别人的健康占位（should-1）。
        # 令牌同时防止旧 leader 给新 leader 续租/删占位（should-1 的另一半）。
        claim_owner: str | None = None
        renewer: asyncio.Task | None = None
        try:
            cached = await self._safe_get(key)
            if cached is not None:
                self._finish_inflight(key, cached)
                return cached, True

            # ④ 支持跨进程占位的后端：抢到占位才是 leader，没抢到就等对方的载荷。
            if claimable:
                claim = await self._safe_claim(key)
                claim_owner = claim.owner
                if claim_owner is None and not claim.backend_error:
                    cached, claim_owner = await self._wait_remote(key)
                    if cached is not None:
                        self._finish_inflight(key, cached)
                        return cached, True
                    # 等待超时/对方失败：_wait_remote 会回传是否抢到了新占位。

            # 只有确实持有占位才续租：无条件起续租循环会替别人的占位续命。
            if claimable and claim_owner is not None:
                renewer = asyncio.create_task(self._renew_loop(key, claim_owner))

            # 本协程是这一键的“抽取者”：真正调用模型，结束后唤醒等待者并回写。
            output = await extract()
            await self._safe_set(key, output)
            self._finish_inflight(key, output)
            return output, False
        except BaseException as exc:
            self._fail_inflight(key, exc)
            if claim_owner is not None:
                await self._safe_release(key, claim_owner)
            raise
        finally:
            if renewer is not None:
                renewer.cancel()
                try:
                    await renewer
                except asyncio.CancelledError:
                    pass

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

    async def _safe_claim(self, key: str) -> _ClaimResult:
        """尝试抢占跨进程 leader 占位；抢到返回本协程的 owner 令牌。

        返回 ``owner=None`` 表示没抢到，调用方会去等对方的载荷；``backend_error``
        表示后端调用异常，调用方应直接抽取但不持有占位（失败时绝不 release
        别人的占位）。生成新令牌而不是复用旧的，才能让过期 leader 的续租/
        释放不再命中新 leader 的占位（should-1）。

        backend 只在占位行【不存在或租约已过期】时才会把 leader 让给我们，
        健康但慢的 leader 只要还在续租就不会被抢（S2）。
        """
        owner = uuid.uuid4().hex
        try:
            claimed = bool(await self.backend.try_claim(
                key, self.claim_lease_seconds, owner))
        except Exception as exc:  # noqa: BLE001 占位失败按“我来抽但不持有”处理
            logger.warning("抽取缓存占位失败，本进程直接抽取 key=%s: %r", key[:16], exc)
            return _ClaimResult(backend_error=True)
        return _ClaimResult(owner=owner if claimed else None)

    async def _safe_renew(self, key: str, owner: str) -> bool | None:
        """续租占位。

        True=已续期；False=明确丢失（被接管/已回写）；None=调用异常（瞬时抖动）。
        调用方不能把 None 当成“占位已丢”，否则一次 DB 抖动就会永久停止续租，
        健康 leader 随后被等待方接管 → 同一块第二次真调（nit-3）。
        """
        try:
            return bool(await self.backend.renew_claim(
                key, self.claim_lease_seconds, owner))
        except Exception as exc:  # noqa: BLE001 续租失败只影响被接管时机
            logger.warning("抽取缓存占位续租失败（稍后重试）key=%s: %r",
                           key[:16], exc)
            return None

    async def _renew_loop(self, key: str, owner: str) -> None:
        """leader 抽取期间按租约 1/3 续期，避免慢但健康的抽取被等待方接管。"""
        interval = max(0.05, self.claim_lease_seconds / 3.0)
        backoff = interval
        while True:
            await asyncio.sleep(backoff)
            renewed = await self._safe_renew(key, owner)
            if renewed is False:
                # 占位明确丢失（被接管/已回写）：停止续租，避免干扰新 leader。
                return
            if renewed is None:
                # 瞬时失败：指数退避重试，但不超过半个租约，别真拖到过期（nit-3）。
                backoff = min(
                    backoff * 2, max(0.05, self.claim_lease_seconds / 2.0))
            else:
                backoff = interval

    async def _safe_release(self, key: str, owner: str) -> None:
        try:
            await self.backend.release(key, owner)
        except Exception as exc:  # noqa: BLE001 释放失败只会多占一行，租约到期后可覆盖
            logger.warning("抽取缓存占位释放失败（忽略）key=%s: %r", key[:16], exc)

    async def _discard_corrupt(self, key: str, raw) -> None:
        """删除损坏载荷行（仅在值仍是读到的那个坏值时），让本协程能抢占重抽。

        后端不实现 discard 时只能尽力而为（ClaimableBackend 不强制它，
        CorruptibleBackend 才是完整契约）：两个 worker 同时遇到坏行可能各抽
        一次，所以要留一条 warning，不能静默当成功（nit-5）。
        """
        discard = getattr(self.backend, "discard", None)
        if discard is None:
            logger.warning(
                "缓存后端未实现 discard，坏载荷可能导致同键重复抽取 key=%s",
                key[:16])
            return
        try:
            await discard(key, raw)
        except Exception as exc:  # noqa: BLE001 删坏行失败 → 继续尝试占位/重抽
            logger.warning("抽取缓存删除损坏载荷失败（忽略）key=%s: %r",
                           key[:16], exc)

    async def _wait_remote(
            self, key: str) -> tuple[ExtractionOutput | None, str | None]:
        """等另一个 worker 回写载荷；租约过期或超时后重新抢占地盘。

        返回 ``(cached, claim_owner)``：
          · ``cached`` 非 None 表示等到了载荷（claim_owner 恒 None）；
          · ``cached is None`` 表示“轮到我抽”，``claim_owner`` 非 None 说明本
            协程确实抢到了占位（决定失败时能不能 release、能不能起续租循环）。
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
                return None, None
            if raw is None:
                # 占位行消失 = leader 失败并释放；立刻重新抢，早于超时恢复。
                claim = await self._safe_claim(key)
                if claim.owner is not None or claim.backend_error:
                    return None, claim.owner
            elif raw != "":
                cached = self._decode(key, raw)
                if cached is not None:
                    return cached, None
                # 载荷损坏：删掉（仅当值仍是我们读到的坏值）再走占位，抢到的
                # 一方重抽并覆盖；没抢到就继续轮询，避免两个 worker 同时重抽（nit-3）。
                await self._discard_corrupt(key, raw)
                claim = await self._safe_claim(key)
                if claim.owner is not None or claim.backend_error:
                    return None, claim.owner
            else:
                # 占位行还在：只有经过一个完整租约后才尝试接管。健康 leader 会
                # 续租，原子抢占会失败；崩溃 leader 的租约过期后等待方立刻接手，
                # 不必付满整个 remote_wait_seconds（S2）。
                if loop.time() - last_takeover >= self.claim_lease_seconds:
                    last_takeover = loop.time()
                    claim = await self._safe_claim(key)
                    if claim.owner is not None or claim.backend_error:
                        return None, claim.owner
            await asyncio.sleep(delay)
            delay = min(delay * 2, self.max_poll_seconds)
        # 循环因 deadline 退出前会先 sleep 一个轮询间隔：leader 可能刚好在这个
        # 窗口里回写。退出前再读一次，否则会白自抽一次（nit-1）。
        cached = await self._safe_get(key)
        if cached is not None:
            return cached, None
        # 等太久（leader 卡死且租约异常）：尽量重新抢占地盘后自己抽，绝不永久卡住。
        return None, (await self._safe_claim(key)).owner

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
            # leader 被取消时不能把 CancelledError 塞给共享 future：那会连带
            # 取消所有健康等待方（blocker-1）。改用可重试的内部信号。
            future.set_exception(
                _LeaderCancelled() if isinstance(exc, asyncio.CancelledError) else exc)
        else:
            # leader 自己直接 raise，没人会取这个 future 的异常；cancel 掉，
            # 避免 GC 时打 "Future exception was never retrieved"（S6）。
            future.cancel()


# 复用 PostgreSQL 作业库时的表结构：与 extraction_jobs 同库，不引入新服务。
# payload 为空字符串表示“跨进程单飞占位行”：leader 正在抽，内容尚未回写。
# expires_at 是占位行租约：leader 抽取期间定期续期，过期才允许其它 worker 抢占。
# owner 是占位行的 leader 令牌：续租/释放都要匹配，旧 leader 不能干扰新 leader。
_POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS extraction_result_cache (
    cache_key   TEXT PRIMARY KEY,
    payload     TEXT NOT NULL,
    owner       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ
);
ALTER TABLE extraction_result_cache
    ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
ALTER TABLE extraction_result_cache
    ADD COLUMN IF NOT EXISTS owner TEXT;
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
    原子抢占位，只有在占位行不存在或租约过期时才成功，并把本次的 owner 令牌
    写进占位行；leader 抽取期间 ``renew_claim`` 用同一令牌定期续期。没抢到的
    worker 轮询 payload，等 leader 回写后直接命中。leader 失败/崩溃时
    ``release`` 删占位（或租约自然过期），等待方会重新抢占位，至多多一次真实
    调用，绝不脏数据。``set`` 是后写覆盖：同一键的两次抽取语义等价，覆盖无害，
    且能顺手修掉损坏载荷。续租/释放都要求 owner 匹配：过期旧 leader 既不能给
    新 leader 续租，也不能删掉新 leader 的占位（should-1）。
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
        # 回写真实载荷即清掉租约与 owner（不再是占位行）。
        await self.pool.execute(
            """
            INSERT INTO extraction_result_cache (cache_key, payload, owner, expires_at)
            VALUES ($1, $2, NULL, NULL)
            ON CONFLICT (cache_key) DO UPDATE
              SET payload = EXCLUDED.payload,
                  owner = NULL,
                  expires_at = NULL
            """,
            key, value)
        await self.purge_expired()

    async def try_claim(
            self, key: str, lease_seconds: float, owner: str) -> bool:
        """原子抢占位；返回 True 表示本进程成为该键的 leader。

        只有占位行不存在、或它的租约已过期（leader 崩溃/卡死）时才抢占成功，
        同时记录本次 owner 令牌。健康但慢的 leader 会续租，其它 worker 不会把
        它顶掉（S2）。
        """
        row = await self.pool.fetchval(
            """
            INSERT INTO extraction_result_cache (cache_key, payload, owner, expires_at)
            VALUES ($1, $2, $3, now() + make_interval(secs => $4::double precision))
            ON CONFLICT (cache_key) DO UPDATE
              SET expires_at = EXCLUDED.expires_at,
                  owner = EXCLUDED.owner
              WHERE extraction_result_cache.payload = $2
                AND (extraction_result_cache.expires_at IS NULL
                     OR extraction_result_cache.expires_at < now())
            RETURNING cache_key
            """,
            key, _CLAIM_PAYLOAD, owner, float(lease_seconds))
        return row is not None

    async def renew_claim(
            self, key: str, lease_seconds: float, owner: str) -> bool:
        """续期占位行租约；占位已被回写/接管或 owner 不匹配时返回 False。"""
        row = await self.pool.fetchval(
            """
            UPDATE extraction_result_cache
               SET expires_at = now() + make_interval(secs => $2::double precision)
             WHERE cache_key = $1 AND payload = $3 AND owner = $4
            RETURNING cache_key
            """,
            key, float(lease_seconds), _CLAIM_PAYLOAD, owner)
        return row is not None

    async def release(self, key: str, owner: str) -> None:
        """leader 失败时释放占位；只删自己持有的空载荷行，绝不误删已回写的
        真实结果或新 leader 的占位。"""
        await self.pool.execute(
            "DELETE FROM extraction_result_cache "
            "WHERE cache_key=$1 AND payload=$2 AND owner=$3",
            key, _CLAIM_PAYLOAD, owner)

    async def discard(self, key: str, expected_value: str) -> None:
        """删除损坏载荷行：只在值仍等于读到的坏值时删，避免误删别人刚回写的
        有效结果（nit-3）。"""
        if not isinstance(expected_value, str) or expected_value == _CLAIM_PAYLOAD:
            return
        await self.pool.execute(
            "DELETE FROM extraction_result_cache "
            "WHERE cache_key=$1 AND payload=$2",
            key, expected_value)

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
