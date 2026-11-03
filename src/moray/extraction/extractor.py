"""
抽取服务（L04 下）
==================

把前几课散落在脚本里的"建模型->建 Agent->结构化输出->校验"封装成一个
可复用、可测试的服务对象：

    extractor = Extractor(domain_code="finance")
    output = await extractor.extract(document)

企业级要点：
1. 无状态污染：每次抽取都新建 Agent（独立记忆），批量处理多篇文档时
   上一篇的内容不会泄漏到下一篇。
2. 失败可纠错：结构化输出校验（枚举/类型/领域规则）不通过时，把【精确的
   错误信息】回喂给模型，要求只修正错误后重交，最多 max_retries 次。
   这是应用层重试，区别于 AgentScope 内部对"模型没按 schema 输出"的重试。
3. 全程可计量：累计每次尝试的 token 用量，落到 ExtractionOutput.usage
   （含命中提示缓存的输入 token，用于算命中率与折后成本）。
4. 可测试：_build_agent / _call_structured 拆成独立方法，单测可注入假返回，
   无需真实联网即可验证重试逻辑。
5. 前缀友好：与文档无关的规则全进 system prompt，正文只出现在 user 消息末尾，
   返工反馈追加在对话尾部 —— 让同一领域的所有请求共享一段稳定长前缀，
   把托管网关的提示缓存（prompt cache）吃满；warm_prefix() 负责起飞前预热。
"""

from __future__ import annotations

import logging
import time
from typing import Type

from pydantic import BaseModel, ValidationError

from agentscope.agent import Agent
from agentscope.message import UserMsg

from ..config import build_chat_model, get_settings, local_backfill_enabled
from ..schema import (
    TASK_PROTOCOL,
    DomainSpec,
    ExtractionResult,
    build_extraction_model,
    build_system_prompt,
    get_domain,
    to_core_result,
    validate_against_domain,
)
from .chunk_metrics import (
    FINISH_UNKNOWN,
    ChunkCallMetrics,
    is_length_finish_reason,
)
from .document import Document, ExtractionOutput, Usage
from .local_backfill import (
    MAX_INTERNAL_RELATION_EVIDENCE_CANDIDATES,
    BackfillReport,
    cached_backfill_provenance,
)

logger = logging.getLogger(__name__)


def _sum_known(values: list[float | None]) -> float | None:
    """累加实测到的分段耗时；全部测不到（None）时保持 None 而不是 0。"""
    known = [v for v in values if v is not None]
    return round(sum(known), 6) if known else None


class ExtractionError(RuntimeError):
    """多次重试后仍无法得到合法抽取结果。

    即使失败也带回已经花掉的度量（含截断信号）：调用方不能因为抛异常
    就把这次的 token 与 finish_reason 全丢掉，看板会因此漏记静默截断。
    """

    def __init__(self, message: str, *, metrics: ChunkCallMetrics | None = None):
        super().__init__(message)
        self.metrics = metrics


# 预热用的中性短串：真实正文会替换它。放在末尾，保证「system + schema + 引导语」
# 这段可复用前缀与真实请求逐字节一致（旧后端 prefix_warmup.py 的同款做法）。
_WARMUP_FILLER = "（预热）"


def _finish_reason_from_msg(reply) -> str:
    """从模型返回中取结束原因（工单 01 截断护栏）。

    agentscope 2.0 的 Msg 只保留 interrupted/completed，供应商原生的
    finish_reason（OpenAI 兼容网关的 stop/length/...）被解析层丢弃。
    模型边界（config_instrumented 的 Instrumented*ChatModel）把原生值挂到
    ChatResponse.metadata；agent.reply 后仍可读 reply.metadata。离线假模型
    可直接挂 reply.finish_reason。两者都没有时显式记 'unknown'——绝不静默
    当成 stop。
    """
    metadata = getattr(reply, "metadata", None)
    if isinstance(metadata, dict) and metadata.get("finish_reason"):
        return str(metadata["finish_reason"]).strip().lower()
    reason = getattr(reply, "finish_reason", None)
    if reason is None:
        return FINISH_UNKNOWN
    return str(reason).strip().lower() or FINISH_UNKNOWN


def _finish_reasons_from_msg(reply) -> list[str]:
    """取本次 reply 内【所有】模型调用的结束原因（按调用顺序）。

    一次 agentscope agent.reply 可能包含多次模型调用（截断 -> 工具参数 JSON
    残缺 -> 自纠）。模型埋点把每次调用的 reason 累积在 metadata['finish_reasons']；
    这里全部带出，Extractor 才能逐次识别截断而不是只看最后一次。
    老假模型/单次调用路径退回单个 finish_reason。
    """
    metadata = getattr(reply, "metadata", None)
    if isinstance(metadata, dict):
        reasons = metadata.get("finish_reasons")
        if isinstance(reasons, list) and reasons:
            return [str(r).strip().lower() for r in reasons if r is not None]
    return [_finish_reason_from_msg(reply)]


def _phase_timing_from_msg(reply) -> tuple[float | None, float | None]:
    """取 (prefill_s, decode_s) 分段计时。

    流式路径在模型边界实测首 token 时间点后挂在 metadata['timing']；
    离线假模型可直接挂 reply.timing。非流式网关一次性返回时为
    (None, None)。缺失是事实，不用总延迟冒充。
    """
    timing = None
    metadata = getattr(reply, "metadata", None)
    if isinstance(metadata, dict):
        timing = metadata.get("timing")
    if timing is None:
        timing = getattr(reply, "timing", None)
    if timing is None:
        return None, None
    if isinstance(timing, dict):
        prefill, decode = timing.get("prefill_s"), timing.get("decode_s")
    else:
        prefill, decode = (
            getattr(timing, "prefill_s", None),
            getattr(timing, "decode_s", None))
    return (
        float(prefill) if prefill is not None else None,
        float(decode) if decode is not None else None,
    )


def _latency_from_msg(reply) -> float | None:
    """取一次完整模型调用的墙钟耗时（非流式也测得到）。

    生产默认非流式，prefill/decode 协议层不存在；latency_s 是"这一步总共
    花了多久"的诚实归因，绝不拿它冒充分段耗时。
    """
    metadata = getattr(reply, "metadata", None)
    if isinstance(metadata, dict):
        value = metadata.get("latency_s")
        if value is not None:
            return float(value)
    value = getattr(reply, "latency_s", None)
    return float(value) if value is not None else None


def _usage_from_msg(reply) -> Usage:
    """从返回 Msg.usage 中稳健地取 token 数（不同供应商字段可能略有差异）。"""
    raw = getattr(reply, "usage", None)
    if raw is None:
        return Usage()
    get = lambda *names: next(
        (getattr(raw, n) for n in names if getattr(raw, n, None) is not None), 0
    )
    return Usage(
        input_tokens=int(get("input_tokens", "prompt_tokens")),
        output_tokens=int(get("output_tokens", "completion_tokens")),
        # 提示缓存命中量：agentscope 各家模型层都归一到 cache_input_tokens
        cached_input_tokens=int(
            get("cache_input_tokens", "cached_input_tokens")),
    )


class Extractor:
    """按指定领域配置抽取文档中的实体与关系。"""

    def __init__(
        self,
        domain: DomainSpec | str = "general",
        *,
        max_retries: int = 2,
        middlewares: list | None = None,
        local_backfill: bool | None = None,
    ) -> None:
        self.domain: DomainSpec = (
            get_domain(domain) if isinstance(domain, str) else domain
        )
        self.max_retries = max_retries
        # 可选的可观测中间件（审计/成本/质量），由调用方传入
        self._middlewares = list(middlewares or [])
        # 结构化输出瘦身开关：None 时读全局配置（默认关）。打开后模型只出逻辑
        # 实体/关系，mention/span/关系证据由本地确定性回填，模型自报置信度移除。
        self.local_backfill: bool = (
            local_backfill_enabled() if local_backfill is None else local_backfill
        )
        # 该领域对应的动态结构化模型与系统提示词（构造一次即可复用）
        self._schema_model: Type[BaseModel] = build_extraction_model(
            self.domain, slim=self.local_backfill
        )
        self._system_prompt: str = build_system_prompt(
            self.domain, slim=self.local_backfill
        )
        settings = get_settings()
        self.model_name: str = settings["chat_model"]
        # 影响输出的采样参数摘要：由分块 pipeline 并入内容缓存键（r5 F7）。
        # 未配置的项显式记 '-'，保证 "None" 与 "0.0" 不会拼成同一个键。
        temperature = settings.get("temperature")
        thinking = settings.get("thinking")
        self.sampling_fingerprint: str = (
            f"temp={temperature if temperature is not None else '-'}"
            f"|effort={settings.get('reasoning_effort') or '-'}"
            f"|think={thinking if thinking is not None else '-'}"
        )
        # 前缀预热去重：一个实例（= 一个领域版本）只探一次，别每次都烧钱
        self._prefix_warmed = False

    # ---- 提示缓存（prefix cache）可观测/可利用 ------------------------------
    @property
    def prefix_version(self) -> str:
        """当前前缀的指纹（领域 code + 长度 + 协议）。

        指纹变了 = 缓存必然失效（领域配置热更/换模型）。日志里对比两个请求的
        指纹即可判断"没命中"是不是我们自己把前缀改动了。
        """
        return (
            f"{self.domain.code}:{len(self._system_prompt)}:"
            f"{len(TASK_PROTOCOL)}"
        )

    async def warm_prefix(self, *, force: bool = False) -> Usage:
        """发一次极小的探测请求，把本领域的稳定前缀提前烘进服务端提示缓存。

        为什么需要：一批块并发时，N 个请求同时 miss、各自付全价 prefill，
        甚至互相抢建同一份缓存；先串行发一发，后续请求一起飞就命中。

        为什么走 `_call_structured`（而不是裸调 model + max_tokens=1）：
        结构化工具的 schema 会被服务端 chat 模板渲染进提示词，只有走【完全相同】
        的调用路径，前缀才可能逐字节一致。代价是这一次会生成几十个 token，
        可忽略；换来的是"预热确实预热了真实前缀"。

        返回探测请求的 Usage（含 cached_input_tokens）：
        cached 为 0 表示网关没回传缓存统计、或前缀太短未命中——
        是否继续依赖预热以这个数字为准，别猜（托管网关的缓存策略由厂商决定）。
        失败返回空 Usage 并只记日志、不抛异常：预热是优化，不是功能。
        """
        if self._prefix_warmed and not force:
            return Usage()
        self._prefix_warmed = True     # 失败也不重试：一个实例只探一次，别刷屏
        try:
            agent = self._build_agent()
            reply = await self._call_structured(
                agent, self._first_prompt(_WARMUP_FILLER)
            )
            usage = _usage_from_msg(reply)
            logger.info(
                "前缀预热完成 domain=%s prefix_version=%s in=%d cached=%d",
                self.domain.code, self.prefix_version,
                usage.input_tokens, usage.cached_input_tokens,
            )
            return usage
        except Exception as exc:  # noqa: BLE001 优化失败不能拖垮抽取
            logger.warning(
                "前缀预热失败（忽略，继续正常抽取）domain=%s: %r",
                self.domain.code, exc,
            )
            return Usage()

    # ---- 可在测试中替换的两个接缝 ----------------------------------------
    def _build_agent(self) -> Agent:
        return Agent(
            name="Extractor",
            system_prompt=self._system_prompt,
            model=build_chat_model(stream=False),
            middlewares=self._middlewares,
        )

    async def _call_structured(self, agent: Agent, content: str) -> object:
        """发起一次结构化调用，返回模型原始 Msg（单测可覆盖此方法）。

        工单 01：agentscope 的最终 Msg 不携带供应商原生 finish_reason 与
        流式分段计时，Instrumented*ChatModel 在响应经过时把它们写进模型
        实例的 moray_last_signals 槽位；这里在 reply 完成后取出、挂到 Msg
        上（与离线假模型直接挂属性的接缝保持同构）。

        B1：信号槽按"模型调用"累积。reply 前 reset，reply 后把**所有**调用的
        finish_reason 挂到 metadata['finish_reasons']，避免同一次 reply 里的
        截断信号被后续自纠调用覆盖。
        """
        model = getattr(agent, "model", None)
        signals = getattr(model, "moray_last_signals", None)
        reset = getattr(signals, "reset", None)
        if callable(reset):
            reset()
        started_at = time.perf_counter()
        reply = await agent.reply(
            UserMsg(name="user", content=content),
            structured_schema=self._schema_model,
        )
        # S1：非流式也能测的墙钟归因（覆盖这次 reply 内的全部模型调用）。
        # 它不等于 prefill/decode，汇总字段名单独叫 latency_s。
        latency_s = round(time.perf_counter() - started_at, 6)
        if signals is not None:
            meta = dict(getattr(reply, "metadata", None) or {})
            meta.setdefault("latency_s", latency_s)
            reply.metadata = meta
        elif latency_s is not None:
            try:
                reply.metadata = dict(
                    getattr(reply, "metadata", None) or {},
                    latency_s=latency_s)
            except (AttributeError, TypeError):
                # 离线假 Msg 可能不允许挂 metadata；退回直接挂属性
                try:
                    reply.latency_s = latency_s
                except AttributeError:
                    pass
        if signals is not None:
            # Msg 没有 finish_reason/timing 字段，统一走 metadata（与
            # Instrumented*ChatModel 挂 ChatResponse.metadata 同一条通道）。
            meta = dict(getattr(reply, "metadata", None) or {})
            reasons = getattr(signals, "finish_reasons", None)
            if reasons:
                meta["finish_reasons"] = [
                    r for r in reasons if r is not None]
            if signals.finish_reason is not None:
                meta.setdefault("finish_reason", signals.finish_reason)
            calls = getattr(signals, "calls", None) or []
            if calls:
                # 同一 attempt 内多次模型调用的分段耗时累加（只加真实测到的）
                prefill = _sum_known([c.get("prefill_s") for c in calls])
                decode = _sum_known([c.get("decode_s") for c in calls])
                if prefill is not None or decode is not None:
                    meta.setdefault("timing", {
                        "prefill_s": prefill, "decode_s": decode})
            elif (signals.prefill_s is not None
                  or signals.decode_s is not None):
                # 兼容只实现单槽的旧假模型/旧埋点（无 calls 列表）
                meta.setdefault("timing", {
                    "prefill_s": signals.prefill_s,
                    "decode_s": signals.decode_s})
            reply.metadata = meta
        return reply

    # ---- 核心流程 ---------------------------------------------------------
    async def extract(self, document: Document) -> ExtractionOutput:
        agent = self._build_agent()
        # 回填/审计用的原文：返工轮 document.text 可能已追加反馈，不能拿它
        # 当 span 坐标系（否则反馈里的实体名会回填出越界 span）。
        source_text = document.audit_text
        # 供 ExtractionMetricsMiddleware 核对 span（中间件存在时才有用）
        setattr(agent, "_audit_source_text", source_text)
        # 同一次 reply 的回填缓存：中间件与抽取器显式传同一个字典（r11 N4），
        # 缓存函数本身不再往 owner 上写私有属性。
        audit_backfill_cache: dict = {}
        setattr(agent, "_audit_backfill_cache", audit_backfill_cache)
        # 中间件需要知道瘦身回填是否开启：用抽取器的单一开关，而不是它自己
        # 再猜一套「无 mention 且 confidence 全 None」的判据（r9 N5）。
        setattr(agent, "_audit_local_backfill", self.local_backfill)
        usage = Usage()
        last_error = ""

        # 第 1 次是正常抽取；之后每次都携带上一次的校验错误，要求修正。
        content = self._first_prompt(document.text)
        total_attempts = self.max_retries + 1

        # 工单 01：逐尝试的结束原因/分段计时，用于截断护栏与返工度量
        attempt_reasons: list[str] = []
        attempt_prefill: list[float | None] = []
        attempt_decode: list[float | None] = []
        attempt_latency: list[float | None] = []
        truncated_attempts = 0

        for attempt in range(1, total_attempts + 1):
            reply = await self._call_structured(agent, content)
            attempt_usage = _usage_from_msg(reply)
            usage.add(attempt_usage)
            # B1：一次 reply 内可能调了多次模型；逐次记录结束原因，
            # 最后一次作为本次 attempt 的最终 finish_reason。
            reasons = _finish_reasons_from_msg(reply)
            reason = reasons[-1]
            prefill_s, decode_s = _phase_timing_from_msg(reply)
            attempt_reasons.extend(reasons)
            attempt_prefill.append(prefill_s)
            attempt_decode.append(decode_s)
            attempt_latency.append(_latency_from_msg(reply))

            # 截断护栏：finish_reason=length 必须显式可见。截断的 JSON 往往
            # 同时让下面的 schema 校验失败并触发返工，这里先于校验识别，
            # 用带结构化字段的 WARNING 打出来——它绝不能伪装成"普通校验失败"。
            # 同一 attempt 内每一次 length 都要计数/告警（截断后自纠救回也一样）。
            for call_index, call_reason in enumerate(reasons, start=1):
                if not is_length_finish_reason(call_reason):
                    continue
                truncated_attempts += 1
                logger.warning(
                    "结构化输出被长度上限截断 event=output_truncated "
                    "document_id=%s domain=%s attempt=%d/%d model_call=%d/%d "
                    "in_tokens=%d out_tokens=%d finish_reason=%s truncated=True",
                    document.id, self.domain.code, attempt, total_attempts,
                    call_index, len(reasons),
                    attempt_usage.input_tokens, attempt_usage.output_tokens,
                    call_reason,
                    extra={"event": "output_truncated", "truncated": True,
                           "document_id": document.id,
                           "domain_code": self.domain.code,
                           "attempt": attempt,
                           "model_call": call_index,
                           "finish_reason": call_reason,
                           "input_tokens": attempt_usage.input_tokens,
                           "output_tokens": attempt_usage.output_tokens},
                )

            # ① 结构化层校验：枚举/类型/数值边界
            try:
                dyn = self._schema_model.model_validate(reply.structured_output)
            except ValidationError as exc:
                last_error = self._format_validation_error(exc)
                content = self._retry_prompt(last_error)
                continue

            # ② 转稳定内核
            core: ExtractionResult = to_core_result(dyn)

            # ③ 领域层校验：类型必须属于该领域等业务规则
            domain_errors = validate_against_domain(core, self.domain)
            if domain_errors:
                last_error = "; ".join(domain_errors)
                content = self._retry_prompt(last_error)
                continue

            # ④ 瘦身模式：模型只给逻辑实体/关系，出现位置由本地确定性回填。
            #    回填坐标系必须是原始正文（source_text），不能是带返工反馈的
            #    text；分块流水线随后用既有的 remap_spans 平移成全局偏移。
            backfill: BackfillReport | None = None
            if self.local_backfill:
                # 与质量中间件共用同一份回填产物（显式 cache 字典），
                # 块正文只扫描一次（r10 N5 / r11 N4）。
                core, backfill = cached_backfill_provenance(
                    audit_backfill_cache, core, source_text)

            # 全部通过
            return ExtractionOutput(
                document_id=document.id,
                domain_code=self.domain.code,
                result=core,
                model_name=self.model_name,
                usage=usage,
                attempts=attempt,
                # 三方合并：瘦身回填信号（03）与分块度量（01）都保留，互不覆盖。
                # 块间/进程内传递保留全量候选；进程内软上限兜住笛卡尔积
                # （r12 N2），真正出网时由 `serialize_backfill_for_output`
                # 按默认 20 条裁剪（r11 F1）。
                backfill=(
                    backfill.to_dict(max_relation_evidence_candidates=(
                        MAX_INTERNAL_RELATION_EVIDENCE_CANDIDATES))
                    if backfill is not None else None),
                call_metrics=ChunkCallMetrics(
                    chunk_id=document.id,
                    attempts=attempt,
                    finish_reason=reason,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cached_input_tokens=usage.cached_input_tokens,
                    wait_s=0.0,  # 排队耗时由分块流水线在更上层测量填入
                    prefill_s=_sum_known(attempt_prefill),
                    decode_s=_sum_known(attempt_decode),
                    latency_s=_sum_known(attempt_latency),
                    attempt_finish_reasons=attempt_reasons,
                    truncated_attempts=truncated_attempts,
                ),
            )

        final_reason = attempt_reasons[-1] if attempt_reasons else FINISH_UNKNOWN
        metrics = ChunkCallMetrics(
            chunk_id=document.id,
            attempts=total_attempts,
            finish_reason=final_reason,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_input_tokens=usage.cached_input_tokens,
            wait_s=0.0,
            prefill_s=_sum_known(attempt_prefill),
            decode_s=_sum_known(attempt_decode),
            latency_s=_sum_known(attempt_latency),
            attempt_finish_reasons=attempt_reasons,
            truncated_attempts=truncated_attempts,
            error=last_error,
        )
        raise ExtractionError(
            f"文档 {document.id} 经过 {total_attempts} 次尝试仍校验失败：{last_error}",
            metrics=metrics,
        )

    # ---- 提示词 ------------------------------------------------------------
    # 前缀缓存按"逐 token 前缀"命中：system prompt 与历史消息只要逐字节不变，
    # 整段 KV 就能复用。所以这个类的规矩是三条：
    #   ① 与文档无关的规则全部进 system prompt（schema.TASK_PROTOCOL）；
    #   ② user 消息 = 一句固定引导 + 正文，正文之后不再有任何固定文字，
    #      这样「system + schema + 引导语」在同领域的所有请求之间共享前缀；
    #   ③ 返工时把纠错反馈追加到对话末尾，不重复发送正文（正文已在历史里），
    #      既省 token 又不动前缀。
    _LEAD_IN = "请抽取下面正文中的实体与关系：\n\n"

    @classmethod
    def _first_prompt(cls, text: str) -> str:
        return f"{cls._LEAD_IN}{text}"

    @staticmethod
    def _retry_prompt(error: str) -> str:
        """纠错反馈。刻意不带正文：正文和上一次的输出都在对话历史里，
        重复发既白烧 token，也让本次消息与前一次不同形。"""
        return (
            "你上一次的结构化输出没有通过校验，错误如下：\n"
            f"{error}\n"
            "请针对这些错误修正后【重新输出完整结果】（不要输出解释文字）。"
        )

    @staticmethod
    def _format_validation_error(exc: ValidationError) -> str:
        lines = []
        for err in exc.errors():
            loc = ".".join(str(x) for x in err["loc"])
            lines.append(f"- 字段 {loc or '(根)'}: {err['msg']}")
        return "\n".join(lines)
