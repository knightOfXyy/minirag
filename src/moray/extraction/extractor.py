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
3. 全程可计量：累计每次尝试的 token 用量，落到 ExtractionOutput.usage。
4. 可测试：_build_agent / _call_structured 拆成独立方法，单测可注入假返回，
   无需真实联网即可验证重试逻辑。
"""

from __future__ import annotations

from typing import Type

from pydantic import BaseModel, ValidationError

from agentscope.agent import Agent
from agentscope.message import UserMsg

from ..config import build_chat_model, get_settings
from ..schema import (
    DomainSpec,
    ExtractionResult,
    build_extraction_model,
    build_system_prompt,
    get_domain,
    to_core_result,
    validate_against_domain,
)
from .document import Document, ExtractionOutput, Usage


class ExtractionError(RuntimeError):
    """多次重试后仍无法得到合法抽取结果。"""


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
    )


class Extractor:
    """按指定领域配置抽取文档中的实体与关系。"""

    def __init__(
        self,
        domain: DomainSpec | str = "general",
        *,
        max_retries: int = 2,
    ) -> None:
        self.domain: DomainSpec = (
            get_domain(domain) if isinstance(domain, str) else domain
        )
        self.max_retries = max_retries
        # 该领域对应的动态结构化模型与系统提示词（构造一次即可复用）
        self._schema_model: Type[BaseModel] = build_extraction_model(self.domain)
        self._system_prompt: str = build_system_prompt(self.domain)

    # ---- 可在测试中替换的两个接缝 ----------------------------------------
    def _build_agent(self) -> Agent:
        return Agent(
            name="Extractor",
            system_prompt=self._system_prompt,
            model=build_chat_model(stream=False),
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
                content = self._retry_prompt(document.text, last_error)
                continue

            # ② 转稳定内核
            core: ExtractionResult = to_core_result(dyn)

            # ③ 领域层校验：类型必须属于该领域等业务规则
            domain_errors = validate_against_domain(core, self.domain)
            if domain_errors:
                last_error = "; ".join(domain_errors)
                content = self._retry_prompt(document.text, last_error)
                continue

            # 全部通过
            return ExtractionOutput(
                document_id=document.id,
                domain_code=self.domain.code,
                result=core,
                model_name=get_settings()["chat_model"],
                usage=usage,
                attempts=attempt,
            )

        raise ExtractionError(
            f"文档 {document.id} 经过 {total_attempts} 次尝试仍校验失败：{last_error}"
        )

    # ---- 提示词 -----------------------------------------------------------
    @staticmethod
    def _first_prompt(text: str) -> str:
        return (
            "请从下面文本中抽取实体与关系。注意：字符偏移 span 一律基于"
            "本次给出的文本、从 0 开始计数；只抽取有明确证据的内容。\n\n"
            f"{text}"
        )

    @staticmethod
    def _retry_prompt(text: str, error: str) -> str:
        return (
            "你上一次的结构化输出没有通过校验，错误如下：\n"
            f"{error}\n\n"
            "请针对这些错误修正后【重新输出完整结果】（不要输出解释文字）。\n"
            f"原始文本：\n{text}"
        )

    @staticmethod
    def _format_validation_error(exc: ValidationError) -> str:
        lines = []
        for err in exc.errors():
            loc = ".".join(str(x) for x in err["loc"])
            lines.append(f"- 字段 {loc or '(根)'}: {err['msg']}")
        return "\n".join(lines)
