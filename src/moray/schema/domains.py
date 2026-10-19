"""
领域配置目录（L04）
===================

「动态 Schema」的真相：不是让整个结构乱动，而是把【会变的部分】——实体类型、
类型说明、关系谓词、专有属性提示——抽到一份纯数据配置里，稳定内核保持不动。

生产中这份配置通常来自：
  · YAML 文件（随代码发布、可评审）
  · 数据库 / 配置中心（多租户，运行时可改，无需发版）
这里先用 Python 常量承载，结构刻意设计成可直接序列化成 JSON/YAML。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AttrSpec:
    """单个专有属性的规格（用于引导模型 + 应用层校验）。"""

    key: str                        # 属性键，进 Entity.attributes
    label: str = ""                 # 给人/模型看的说明
    required: bool = False          # 是否必填
    value_type: str = "string"      # string / integer / number / boolean
    enum: tuple[str, ...] = ()      # 限定取值（可选）
    example: object = None

    def type_word(self) -> str:
        return {
            "string": "字符串", "integer": "整数", "number": "数字",
            "boolean": "布尔 true/false",
        }.get(self.value_type, self.value_type)


@dataclass(frozen=True)
class TypeSpec:
    """一个实体类型的配置。"""

    code: str                       # 类型代码，如 PER / CONTRACT
    label: str                      # 给人看的名字
    guide: str                      # 给模型看的归类说明（直接进 prompt/schema）
    # 该类型的结构化属性规格（热更领域包的核心）；为空则不引导/校验专有属性
    attributes: tuple[AttrSpec, ...] = ()

    # ---- 向后兼容的派生视图 ----
    @property
    def attribute_hints(self) -> tuple[str, ...]:
        return tuple(a.key for a in self.attributes)

    @property
    def attributes_example(self) -> dict:
        return {a.key: a.example for a in self.attributes if a.example is not None}


@dataclass(frozen=True)
class DomainSpec:
    """一个业务领域的完整抽取配置。"""

    code: str                       # 领域代码：general / finance / medical
    name: str
    types: tuple[TypeSpec, ...]
    relation_predicates: tuple[str, ...] = ()   # 建议的规范关系谓词
    extra_instruction: str = ""     # 追加到系统提示词的领域抽取规则

    # ---- 便捷派生 ----
    def type_codes(self) -> list[str]:
        return [t.code for t in self.types]

    def type_guide_text(self) -> str:
        """拼成给模型看的类型说明（进 system prompt）。"""
        lines = []
        for t in self.types:
            extra = f"；建议属性: {', '.join(t.attribute_hints)}" if t.attribute_hints else ""
            lines.append(f"  - {t.code}: {t.guide}{extra}")
        return "\n".join(lines)

    def attribute_guide_text(self) -> str:
        """拼成 attributes 字段的补充说明（进动态模型描述）。"""
        blocks = []
        for t in self.types:
            if not t.attributes:
                continue
            parts = []
            for a in t.attributes:
                req = "（必填）" if a.required else ""
                enum = f"，取值限定 {list(a.enum)}" if a.enum else ""
                parts.append(f"{a.key}: {a.type_word()}{req}{enum}")
            blocks.append(f"{t.code} 的 attributes 字段：" + "；".join(parts))
        return "\n".join(blocks)

    def type_attribute_map(self) -> dict[str, dict[str, AttrSpec]]:
        return {t.code: {a.key: a for a in t.attributes} for t in self.types}

    def get(self, code: str) -> TypeSpec | None:
        for t in self.types:
            if t.code == code:
                return t
        return None


# ---------------------------------------------------------------------------
# 通用领域（沿用 L03 的五类）
# ---------------------------------------------------------------------------
GENERAL_DOMAIN = DomainSpec(
    code="general",
    name="通用领域",
    types=(
        TypeSpec("PER", "人物", "真实或虚构的人名", attributes=(
            AttrSpec("birth_date", "出生日期"),
            AttrSpec("title", "职务/头衔"),
        )),
        TypeSpec("ORG", "机构", "公司、政府机构、学校、团队、协会等组织", attributes=(
            AttrSpec("founded_date", "成立日期"),
            AttrSpec("registered_capital", "注册资本（数字）", value_type="number"),
            AttrSpec("legal_representative", "法定代表人"),
            AttrSpec("industry", "所属行业"),
        )),
        TypeSpec("LOC", "地点", "国家、城市、区域、地址、山川等地名"),
        TypeSpec("EVENT", "事件", "有专名的会议、事故、项目、赛事、战役等", attributes=(
            AttrSpec("event_date", "发生日期"),
        )),
        TypeSpec(
            "MISC", "其他",
            "确有价值但不属于上述类别的命名实体；日期、年份、纯数字、普通名词不要抽取",
        ),
    ),
    relation_predicates=("创立", "任职于", "总部位于", "位于", "投资", "收购", "子公司"),
    extra_instruction="只抽取命名实体；日期、年份、纯数字、普通名词一律不要抽取。",
)


# ---------------------------------------------------------------------------
# 金融合同领域（演示热插拔：全新的类型集 + 专有属性）
# ---------------------------------------------------------------------------
FINANCE_DOMAIN = DomainSpec(
    code="finance",
    name="金融合同",
    types=(
        TypeSpec(
            "PARTY", "合同方", "合同的签约主体（甲方/乙方/担保人等公司或个人）",
            attributes=(
                AttrSpec("role", "签约角色", enum=("甲方", "乙方", "担保人")),
                AttrSpec("credit_code", "统一社会信用代码"),
            ),
        ),
        TypeSpec(
            "CONTRACT", "合同", "被签署的合同/协议本身",
            attributes=(
                AttrSpec("contract_name", "合同名称", required=True),
                AttrSpec("amount", "合同金额（单位：元，数字）", value_type="number"),
                AttrSpec("currency", "币种 ISO 代码", enum=("CNY", "USD", "EUR")),
                AttrSpec("term_years", "合同年限", value_type="integer"),
                AttrSpec("sign_date", "签署日期 YYYY-MM-DD"),
            ),
        ),
        TypeSpec(
            "PERSON", "自然人", "合同中出现的具体责任人、法人、经办人等自然人",
            attributes=(AttrSpec("role", "职务/角色"),),
        ),
        TypeSpec(
            "FIN_ORG", "金融机构", "银行、券商、基金等金融类机构",
            attributes=(AttrSpec("institution_type", "机构类型"),),
        ),
    ),
    relation_predicates=("签约", "担保", "受托支付", "放款", "持有股权", "关联交易"),
    extra_instruction=(
        "金额统一规范化为数字（单位：元）放入 attributes.amount，币种用 ISO 代码"
        "（CNY/USD）；日期规范化为 YYYY-MM 或 YYYY-MM-DD；"
        "只抽取与合同权利义务相关的实体，不要把纯时间词单独抽成实体。"
    ),
)


# ---------------------------------------------------------------------------
# 领域注册表：code -> DomainSpec（生产中可由配置文件/DB 加载填充）
# ---------------------------------------------------------------------------
DOMAIN_REGISTRY: dict[str, DomainSpec] = {
    GENERAL_DOMAIN.code: GENERAL_DOMAIN,
    FINANCE_DOMAIN.code: FINANCE_DOMAIN,
}


def get_domain(code: str) -> DomainSpec:
    """按 code 取领域配置；找不到给出清晰错误（防止配错领域）。"""
    try:
        return DOMAIN_REGISTRY[code]
    except KeyError:
        available = ", ".join(DOMAIN_REGISTRY)
        raise KeyError(f"未知领域 {code!r}，已注册领域: {available}") from None


# ---------------------------------------------------------------------------
# 领域包的纯数据序列化：DomainSpec <-> dict（JSONB）
# DB 配置中心（kb.domain_configs 表）就存这份 dict；不依赖任何框架/文件格式。
# ---------------------------------------------------------------------------
def domain_from_dict(data: dict) -> DomainSpec:
    """把一份纯数据（DB JSONB / JSON）构造成 DomainSpec。

    结构：
        code/name/extra_instruction/relation_predicates
        types: [{code,label,guide, attributes:[{key,label,required,type,enum,example}]}]
    """
    types = []
    for t in data.get("types", []):
        attrs = tuple(
            AttrSpec(
                key=a["key"],
                label=a.get("label", ""),
                required=bool(a.get("required", False)),
                value_type=a.get("type", "string"),
                enum=tuple(a.get("enum", ())),
                example=a.get("example"),
            )
            for a in t.get("attributes", [])
        )
        types.append(TypeSpec(
            code=t["code"], label=t.get("label", t["code"]),
            guide=t.get("guide", ""), attributes=attrs))
    return DomainSpec(
        code=data["code"], name=data.get("name", data["code"]),
        types=tuple(types),
        relation_predicates=tuple(data.get("relation_predicates", ())),
        extra_instruction=data.get("extra_instruction", ""),
    )


def domain_to_dict(domain: DomainSpec) -> dict:
    """DomainSpec -> 可存 JSONB 的纯 dict（domain_from_dict 的逆操作）。"""
    return {
        "code": domain.code,
        "name": domain.name,
        "extra_instruction": domain.extra_instruction,
        "relation_predicates": list(domain.relation_predicates),
        "types": [
            {
                "code": t.code,
                "label": t.label,
                "guide": t.guide,
                "attributes": [
                    {
                        "key": a.key, "label": a.label, "required": a.required,
                        "type": a.value_type, "enum": list(a.enum),
                        **({"example": a.example} if a.example is not None else {}),
                    }
                    for a in t.attributes
                ],
            }
            for t in domain.types
        ],
    }
