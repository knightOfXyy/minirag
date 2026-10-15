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
class TypeSpec:
    """一个实体类型的配置。"""

    code: str                       # 类型代码，如 PER / CONTRACT
    label: str                      # 给人看的名字
    guide: str                      # 给模型看的归类说明（直接进 prompt/schema）
    # 该类型建议抽取的专有属性（写进描述，引导模型填进 Entity.attributes）
    attribute_hints: tuple[str, ...] = ()
    # 该类型专有属性的期望结构提示（仅用于引导与二次校验，不强制）
    attributes_example: dict = field(default_factory=dict)


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
            if t.attribute_hints:
                blocks.append(
                    f"{t.code} 的 attributes 建议包含: "
                    f"{', '.join(t.attribute_hints)}（示例 {t.attributes_example}）"
                )
        return "\n".join(blocks)

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
        TypeSpec("PER", "人物", "真实或虚构的人名"),
        TypeSpec("ORG", "机构", "公司、政府机构、学校、团队、协会等组织"),
        TypeSpec("LOC", "地点", "国家、城市、区域、地址、山川等地名"),
        TypeSpec("EVENT", "事件", "有专名的会议、事故、项目、赛事、战役等"),
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
            attribute_hints=("role", "credit_code"),
            attributes_example={"role": "甲方", "credit_code": "91310000XX..."},
        ),
        TypeSpec(
            "CONTRACT", "合同", "被签署的合同/协议本身",
            attribute_hints=("contract_name", "amount", "currency", "term_years", "sign_date"),
            attributes_example={
                "contract_name": "采购合同", "amount": 500000000,
                "currency": "CNY", "term_years": 3, "sign_date": "2024-03",
            },
        ),
        TypeSpec(
            "PERSON", "自然人", "合同中出现的具体责任人、法人、经办人等自然人",
            attribute_hints=("role",),
        ),
        TypeSpec(
            "FIN_ORG", "金融机构", "银行、券商、基金等金融类机构",
            attribute_hints=("institution_type",),
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
