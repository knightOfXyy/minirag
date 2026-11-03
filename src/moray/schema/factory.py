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


# 与领域、与具体文档都【无关】的任务协议：放进 system prompt 尾部，
# 让"同一领域的所有请求"共享一段逐字节稳定的长前缀（提示缓存可整段复用）。
# 变化内容（正文/纠错反馈）永远只出现在最后的 user 消息里，绝不动前面的前缀。
TASK_PROTOCOL = (
    "输出协议：只输出结构化结果，不输出任何解释性文字。"
    "字符偏移 start/end 一律相对于【本次给出的正文】，从 0 开始计数。"
    "只抽取有明确文本证据的内容；没有任何实体时返回空列表，禁止臆测。"
)


TASK_PROTOCOL_SLIM = (
    "输出协议：只输出结构化结果，不输出任何解释性文字。"
    "只输出逻辑实体（标准名、类型、别名、专有属性）与逻辑关系（头、谓词、尾），"
    "不要输出出现位置、字符偏移或置信度——出现位置由本地确定性回填完成。"
    "只抽取有明确文本证据的内容；没有任何实体时返回空列表，禁止臆测。"
)


def build_system_prompt(domain: DomainSpec, *, slim: bool = False) -> str:
    """根据领域配置拼出抽取 Agent 的系统提示词。

    前缀稳定性 = 提示缓存命中率：这里刻意【不含时间戳/随机 id/文档相关内容】，
    同领域内每次请求逐字节一致；领域配置变更（DB updated_at 变化）才会换前缀。

    slim=True（结构化输出瘦身）：不再要求模型输出 mentions/字符偏移/置信度，
    这些由本地确定性回填；提示词因此明显更短、解码更快、无偏移幻觉。
    """
    predicates = "、".join(f"「{p}」" for p in domain.relation_predicates) or "（按文本自行概括规范动词）"
    if slim:
        return (
            "你是严谨的实体与关系抽取专家。只抽取文本中明确出现、且属于下列类型的"
            "命名实体，并判断实体之间有明确文本证据的关系。\n"
            f"当前领域：{domain.name}（{domain.code}）\n"
            "实体类型只能使用以下枚举：\n"
            f"{domain.type_guide_text()}\n"
            f"关系谓词尽量使用规范动词：{predicates}。\n"
            "要求：标准名取最完整正式的叫法，其他叫法放进 aliases；专有字段放入 "
            "attributes。不要输出出现位置、字符偏移或置信度。\n"
            f"{domain.extra_instruction}\n"
            "没有任何实体时返回空列表，禁止臆测。\n"
            f"{TASK_PROTOCOL_SLIM}"
        )
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
        "没有任何实体时返回空列表，禁止臆测。\n"
        f"{TASK_PROTOCOL}"
    )


def domain_fingerprint(domain: DomainSpec) -> str:
    """影响抽取输出的【领域配置版本】指纹（16 位十六进制）。

    覆盖系统提示词（类型枚举/说明/关系谓词/额外规则）与结构化输出模型的
    JSON Schema（字段、枚举、属性引导）两类输入：任一字节变化都换指纹，
    用作分块结果缓存键的一部分——领域一改（如增删实体类型枚举），缓存即失效。
    刻意不依赖 DB updated_at：内置常量与 DB 配置可复用同一口径，且只反映
    真正影响输出的内容（改个备注名但 spec 不变不会无谓失效）。
    """
    import hashlib
    import json

    prompt = build_system_prompt(domain)
    model_schema = build_extraction_model(domain).model_json_schema()
    canonical = json.dumps(
        {"prompt": prompt, "schema": model_schema},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def build_extraction_model(domain: DomainSpec, *, slim: bool = False) -> Type[BaseModel]:
    """为指定领域动态生成结构化输出模型。

    和写死 `type: Literal[...]` 的效果完全一样，只是枚举值来自配置，
    因此可以在不修改本文件的前提下支持新领域。

    slim=True（结构化输出瘦身）：模型只输出逻辑实体（name/type/aliases/
    attributes）与逻辑关系（head/predicate/tail），不再输出 mentions、字符
    偏移、证据 span 与自报置信度——这些由本地确定性回填与确定性信号替代，
    显著缩短解码输出、消除偏移幻觉。
    """
    # ① 动态枚举：Literal['PER', 'ORG', ...]，值来自领域配置
    type_literal = Literal[tuple(domain.type_codes())]  # type: ignore[valid-type]

    # ② 动态实体模型：公共字段对齐内核，type 用动态枚举约束
    attr_guide = domain.attribute_guide_text()
    # 字段顺序必须与改造前（262319b）逐字节一致：full 是
    # name/type/aliases/mentions/confidence/attributes；slim 只是删掉
    # mentions/confidence，保留其余相对顺序。
    entity_fields = dict(
        name=(str, Field(description="实体标准名称，取最正式完整的叫法")),
        type=(
            type_literal,
            Field(description="实体类型，只能取本领域枚举值之一"),
        ),
        aliases=(list[str], Field(default_factory=list, description="其他叫法/简称")),
    )
    if not slim:
        entity_fields["mentions"] = (
            list[EntityMention],
            Field(default_factory=list, description="每次原文出现及字符偏移"),
        )
        entity_fields["confidence"] = (
            float,
            Field(default=0.0, ge=0.0, le=1.0, description="置信度0~1"),
        )
    entity_fields["attributes"] = (
        dict,
        Field(
            default_factory=dict,
            description=(
                "该类型专有的属性键值对；没有则空对象。\n" + attr_guide
            ).strip(),
        ),
    )
    dyn_entity = create_model(
        f"{domain.code.title()}Entity", __base__=BaseModel, **entity_fields
    )

    # ③ 关系模型：瘦身时只保留 头/谓词/尾，去掉证据偏移与置信度
    if slim:
        dyn_relation = create_model(
            f"{domain.code.title()}Relation",
            head=(str, Field(description="头实体的标准名称")),
            predicate=(str, Field(description="关系类型，规范动词短语")),
            tail=(str, Field(description="尾实体的标准名称")),
            __base__=BaseModel,
        )
    else:
        # 全量口径必须与改造前（262319b）逐字节一致：显式重建字段，而不是
        # 直接复用内核 Relation。内核的 confidence 已改为可空（未知语义，
        # slim 侧需要），直接复用会让 full 的结构化输出 schema 跟着漂移，
        # 模型被要求提供的字段也就变了。
        dyn_relation = create_model(
            "Relation",
            __doc__=(
                "一条 (头实体, 关系, 尾实体) 的三元组。\n\n"
                "head/tail 用实体的标准名称引用，和 Entity.name 对齐；\n"
                "第 8 课入库后会改成稳定的实体 ID。"
            ),
            head=(str, Field(description="头实体的标准名称（关系的发起方）")),
            predicate=(
                str,
                Field(description=(
                    "关系类型，尽量使用规范动词短语，如 "
                    "「创立」「任职于」「总部位于」「投资」「收购」「子公司」")),
            ),
            tail=(str, Field(description="尾实体的标准名称（关系的承受方）")),
            evidence_span=(
                Span | None,
                Field(default=None, description="支撑该关系的原文证据句位置，用于溯源"),
            ),
            confidence=(
                float,
                Field(default=0.0, ge=0.0, le=1.0, description="关系抽取置信度，0~1"),
            ),
            __base__=BaseModel,
        )

    # ④ 顶层结果模型：实体 + 关系
    suffix = "Slim" if slim else ""
    dyn_result = create_model(
        f"{domain.code.title()}ExtractionResult{suffix}",
        entities=(
            list[dyn_entity],
            Field(default_factory=list, description="抽取出的去重实体"),
        ),
        relations=(
            list[dyn_relation],
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
            # 缺省即「未知」：slim 模式模型不自报，不能伪造成 0.0 低置信
            confidence=e.get("confidence"),
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
                confidence=r.get("confidence"),
            )
        )

    return ExtractionResult(entities=entities, relations=relations)


_ATTR_TYPE_CHECKS = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
}


def validate_attributes(result: ExtractionResult, domain: DomainSpec) -> list[str]:
    """按领域配置校验每个实体的 attributes：必填 / 类型 / 枚举取值。

    返回错误信息列表；空列表表示通过。配置里没声明的属性键一律放行
    （保留开放发现能力），只对【已声明】的键做约束。
    """
    attr_map = domain.type_attribute_map()
    errors: list[str] = []
    for i, e in enumerate(result.entities):
        specs = attr_map.get(e.type)
        if not specs:
            continue
        for key, spec in specs.items():
            if key not in e.attributes or e.attributes[key] in (None, ""):
                if spec.required:
                    errors.append(
                        f"entities[{i}]({e.name}/{e.type}) 缺少必填属性 "
                        f"attributes.{key}")
                continue
            value = e.attributes[key]
            check = _ATTR_TYPE_CHECKS.get(spec.value_type)
            if check and not check(value):
                errors.append(
                    f"entities[{i}]({e.name}/{e.type}) 属性 {key} 应为 "
                    f"{spec.value_type}，实际 {type(value).__name__}={value!r}")
            if spec.enum and value not in spec.enum:
                errors.append(
                    f"entities[{i}]({e.name}/{e.type}) 属性 {key}={value!r} "
                    f"不在允许取值 {list(spec.enum)} 内")
    return errors


def validate_against_domain(result: ExtractionResult, domain: DomainSpec) -> list[str]:
    """领域级二次校验：实体类型枚举 + 专有属性约束。

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
    errors.extend(validate_attributes(result, domain))
    return errors
