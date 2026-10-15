"""
动态 Schema 工厂（L04）
=======================

把「领域配置 DomainSpec」翻译成三样运行时产物：
  1. 一个动态 Pydantic 模型（实体类型是 Literal 枚举）——交给模型做结构化输出，
     既保留强校验，又能按领域换类型；
  2. 一段系统提示词（类型说明 + 关系谓词 + 领域规则）；
  3. 一个转换器：把动态模型结果转回【稳定内核 Entity/Relation/ExtractionResult】，
     存储层永远只依赖稳定内核，动态类型和属性不污染下游。

这就是"动态枚举 + 稳定内核 + attributes 属性包"三者如何协同的核心。
"""

from __future__ import annotations

from typing import Literal, Type

from pydantic import BaseModel, Field, create_model

from .domains import DomainSpec
from .entities import (
    Entity,
    EntityMention,
    ExtractionResult,
    Relation,
    Span,
)


def build_system_prompt(domain: DomainSpec) -> str:
    """根据领域配置拼出抽取 Agent 的系统提示词。"""
    predicates = "、".join(f"「{p}」" for p in domain.relation_predicates) or "（按文本自行概括规范动词）"
    return (
        "你是严谨的实体与关系抽取专家。只抽取文本中明确出现、且属于下列类型的"
        "命名实体，并判断实体之间有明确文本证据的关系。\n"
        f"当前领域：{domain.name}（{domain.code}）\n"
        "实体类型只能使用以下枚举：\n"
        f"{domain.type_guide_text()}\n"
        f"关系谓词尽量使用规范动词：{predicates}。\n"
        "要求：标准名取最完整正式的叫法，其他叫法放进 aliases；"
        "mentions 记录每次原文出现及其字符偏移；专有字段放入 attributes。\n"
        f"{domain.extra_instruction}\n"
        "没有任何实体时返回空列表，禁止臆测。"
    )


def build_extraction_model(domain: DomainSpec) -> Type[BaseModel]:
    """为指定领域动态生成结构化输出模型。

    和写死 `type: Literal[...]` 的效果完全一样，只是枚举值来自配置，
    因此可以在不修改本文件的前提下支持新领域。
    """
    # ① 动态枚举：Literal['PER', 'ORG', ...]，值来自领域配置
    type_literal = Literal[tuple(domain.type_codes())]  # type: ignore[valid-type]

    # ② 动态实体模型：公共字段对齐内核，type 用动态枚举约束
    attr_guide = domain.attribute_guide_text()
    dyn_entity = create_model(
        f"{domain.code.title()}Entity",
        name=(str, Field(description="实体标准名称，取最正式完整的叫法")),
        type=(
            type_literal,
            Field(description="实体类型，只能取本领域枚举值之一"),
        ),
        aliases=(list[str], Field(default_factory=list, description="其他叫法/简称")),
        mentions=(
            list[EntityMention],
            Field(default_factory=list, description="每次原文出现及字符偏移"),
        ),
        confidence=(
            float,
            Field(default=0.0, ge=0.0, le=1.0, description="置信度0~1"),
        ),
        attributes=(
            dict,
            Field(
                default_factory=dict,
                description=(
                    "该类型专有的属性键值对；没有则空对象。\n" + attr_guide
                ).strip(),
            ),
        ),
        __base__=BaseModel,
    )

    # ③ 顶层结果模型：实体 + 关系
    dyn_result = create_model(
        f"{domain.code.title()}ExtractionResult",
        entities=(
            list[dyn_entity],
            Field(default_factory=list, description="抽取出的去重实体"),
        ),
        relations=(
            list[Relation],
            Field(default_factory=list, description="有明确证据的关系三元组"),
        ),
        __base__=BaseModel,
    )
    return dyn_result


def to_core_result(dynamic_result: BaseModel) -> ExtractionResult:
    """把动态模型实例转换成【稳定内核】ExtractionResult。

    动态类型在核心里只是 Entity.type 字符串；专有字段收进 attributes。
    存储/检索/API 因此与"当前用了哪个领域"彻底解耦。
    """
    data = dynamic_result.model_dump()

    entities = [
        Entity(
            name=e["name"],
            type=e["type"],          # 动态枚举值 -> 稳定字符串代码
            aliases=e.get("aliases", []),
            mentions=[EntityMention(**m) for m in e.get("mentions", [])],
            confidence=e.get("confidence", 0.0),
            attributes=e.get("attributes", {}),
        )
        for e in data.get("entities", [])
    ]

    relations = []
    for r in data.get("relations", []):
        ev = r.get("evidence_span")
        relations.append(
            Relation(
                head=r["head"],
                predicate=r["predicate"],
                tail=r["tail"],
                evidence_span=Span(**ev) if ev else None,
                confidence=r.get("confidence", 0.0),
            )
        )

    return ExtractionResult(entities=entities, relations=relations)


def validate_against_domain(result: ExtractionResult, domain: DomainSpec) -> list[str]:
    """领域级二次校验（核心 Schema 不管枚举，由这里补上）。

    返回错误信息列表；空列表表示通过。真实流水线会据此决定放行/打回重抽。
    """
    allowed = set(domain.type_codes())
    errors: list[str] = []
    for i, e in enumerate(result.entities):
        if e.type not in allowed:
            errors.append(
                f"entities[{i}] 类型 {e.type!r} 不在领域 {domain.code} "
                f"允许的 {sorted(allowed)} 内"
            )
    return errors
