"""
实体 / 关系 / 出处 的核心 Schema（L03 地基）
=============================================

设计原则：
1. 这些 Pydantic 模型有「两副面孔」：
   · 给程序看：强类型校验、自动补默认值、.model_validate() 还原；
   · 给大模型看：Field(description=...) 会进 JSON Schema，等于抽取指令。
   所以每句 description 都要像写给模型看的提示词，字斟句酌。
2. 出处（provenance）从第一天就内建——企业知识库必须能回答
   "这条实体是从哪个文档的哪句话抽出来的？"，否则无法纠错、无法审计。
3. 实体类型用枚举受控，避免模型自由发挥造出"公司/企业/集团"三种同义类型。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# 注意（L04 演进）：
# 核心 Entity 不再把类型写死成 Literal 枚举。允许哪些类型，由「领域配置目录」
# （domains.py）在运行时决定，再由 factory.py 动态生成带枚举约束的模型。
# 这样核心存储层永远稳定（type 只是一个字符串代码），而金融的 PARTY/CONTRACT、
# 医疗的 DRUG/DISEASE 都能热插拔，无需改动内核与数据库表结构。
# 通用领域的类型 PER/ORG/LOC/EVENT/MISC 见 domains.py 的 GENERAL_DOMAIN。


# ---------------------------------------------------------------------------
# ② 出处：实体/关系在原文中的精确位置（provenance 的最小单元）
# ---------------------------------------------------------------------------
class Span(BaseModel):
    """一段原文出现位置。字符偏移让我们能在原文里高亮、复核。"""

    start: int = Field(ge=0, description="起始字符偏移（含），从 0 开始")
    end: int = Field(ge=0, description="结束字符偏移（不含）")
    text: str = Field(description="该位置对应的原文片段，便于人工核对")


class EntityMention(BaseModel):
    """实体在文本中的一次「具体提及」。

    同一个实体可能在文中出现多次、用不同叫法（阿里巴巴/阿里/BABA），
    每次出现都是一个 mention，归并到同一个实体上。
    """

    surface: str = Field(description="文中实际出现的字面写法，如「阿里」")
    span: Span | None = Field(
        default=None,
        description="该提及在原文中的位置；无法定位时可为空",
    )


# ---------------------------------------------------------------------------
# ③ 实体：抽取的第一公民
# ---------------------------------------------------------------------------
class Entity(BaseModel):
    """一个规范化后的命名实体。"""

    name: str = Field(
        description=(
            "实体的标准名称（canonical name），取该实体最正式/最完整的叫法，"
            "例如文中出现「阿里」时标准名应写「阿里巴巴」"
        ),
    )
    # 稳定内核只要求它是字符串类型代码；具体允许哪些值由领域配置约束。
    type: str = Field(
        description="实体类型代码，必须使用当前领域配置中允许的类型",
    )
    aliases: list[str] = Field(
        default_factory=list,
        description="该实体在本文中的其他叫法/简称/英文名/旧称，去重后列出",
    )
    mentions: list[EntityMention] = Field(
        default_factory=list,
        description="该实体在原文中的每一次具体出现（含字面写法和位置）",
    )
    confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="抽取置信度，0~1；越确信越接近 1",
    )
    # 动态属性包：领域专有的、不稳定的字段都放这里（如合同金额、药品剂量）。
    # L08 入库时映射到 PostgreSQL 的 JSONB 列，可索引、加领域字段不改表结构。
    attributes: dict = Field(
        default_factory=dict,
        description=(
            "该实体类型专有的附加属性键值对，如金额/币种/日期/规格等；"
            "没有专有属性时返回空对象 {}"
        ),
    )


# ---------------------------------------------------------------------------
# ④ 关系：把孤立实体连成知识
# ---------------------------------------------------------------------------
class Relation(BaseModel):
    """一条 (头实体, 关系, 尾实体) 的三元组。

    head/tail 用实体的标准名称引用，和 Entity.name 对齐；
    第 8 课入库后会改成稳定的实体 ID。
    """

    head: str = Field(description="头实体的标准名称（关系的发起方）")
    predicate: str = Field(
        description=(
            "关系类型，尽量使用规范动词短语，如 "
            "「创立」「任职于」「总部位于」「投资」「收购」「子公司」"
        ),
    )
    tail: str = Field(description="尾实体的标准名称（关系的承受方）")
    evidence_span: Span | None = Field(
        default=None,
        description="支撑该关系的原文证据句位置，用于溯源",
    )
    confidence: float = Field(
        default=0.0, ge=0.0, le=1.0, description="关系抽取置信度，0~1"
    )


# ---------------------------------------------------------------------------
# ⑤ 抽取结果：Agent 结构化输出的顶层模型
# ---------------------------------------------------------------------------
class ExtractionResult(BaseModel):
    """对一篇文档一次抽取的完整产物（这就是 Agent 必须吐出的结构）。"""

    entities: list[Entity] = Field(
        default_factory=list,
        description="抽取出的去重实体列表；文中没有任何实体时返回空列表",
    )
    relations: list[Relation] = Field(
        default_factory=list,
        description=(
            "实体之间成立的关系三元组列表；"
            "只抽取原文有明确证据的关系，禁止臆测，不确定就不要输出"
        ),
    )
