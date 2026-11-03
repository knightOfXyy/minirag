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
from typing import Type

from pydantic import BaseModel, ValidationError

from agentscope.agent import Agent
from agentscope.message import UserMsg

from ..config import build_chat_model, get_settings
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
from .document import Document, ExtractionOutput, Usage

logger = logging.getLogger(__name__)


class ExtractionError(RuntimeError):
    """多次重试后仍无法得到合法抽取结果。"""


# 预热用的中性短串：真实正文会替换它。放在末尾，保证「system + schema + 引导语」
# 这段可复用前缀与真实请求逐字节一致（旧后端 prefix_warmup.py 的同款做法）。
_WARMUP_FILLER = "（预热）"


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
    ) -> None:
        self.domain: DomainSpec = (
            get_domain(domain) if isinstance(domain, str) else domain
        )
        self.max_retries = max_retries
        # 可选的可观测中间件（审计/成本/质量），由调用方传入
        self._middlewares = list(middlewares or [])
        # 该领域对应的动态结构化模型与系统提示词（构造一次即可复用）
        self._schema_model: Type[BaseModel] = build_extraction_model(self.domain)
        self._system_prompt: str = build_system_prompt(self.domain)
        settings = get_settings()
        self.model_name: str = settings["chat_model"]
        # 缓存键用的完整模型身份：同一个 chat_model 挂在不同 provider/base_url
        # 网关下可能是不同权重/版本，不能共用缓存（nit-1）。model_name 仍保持
        # 裸模型名，API/结果序列化口径不变。
        self.model_identity: str = (
            f"{settings.get('provider', '')}|{settings.get('base_url', '')}|"
            f"{self.model_name}")
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
        """发起一次结构化调用，返回模型原始 Msg（单测可覆盖此方法）。"""
        return await agent.reply(
            UserMsg(name="user", content=content),
            structured_schema=self._schema_model,
        )

    # ---- 核心流程 ---------------------------------------------------------
    async def extract(self, document: Document) -> ExtractionOutput:
        agent = self._build_agent()
        # 供 ExtractionMetricsMiddleware 核对 span（中间件存在时才有用）
        setattr(agent, "_audit_source_text", document.text)
        usage = Usage()
        last_error = ""

        # 第 1 次是正常抽取；之后每次都携带上一次的校验错误，要求修正。
        content = self._first_prompt(document.text)
        total_attempts = self.max_retries + 1

        for attempt in range(1, total_attempts + 1):
            reply = await self._call_structured(agent, content)
            usage.add(_usage_from_msg(reply))

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

            # 全部通过
            return ExtractionOutput(
                document_id=document.id,
                domain_code=self.domain.code,
                result=core,
                model_name=self.model_name,
                usage=usage,
                attempts=attempt,
            )

        raise ExtractionError(
            f"文档 {document.id} 经过 {total_attempts} 次尝试仍校验失败：{last_error}"
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
