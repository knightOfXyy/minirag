"""
================================================================================
L04（上）· 动态 Schema：领域类型热插拔 + 稳定内核转换
================================================================================

这节课解决"不同行业/租户要不同实体类型和字段"的问题。核心套路三步：

    领域配置(DomainSpec)  ──factory──▶  动态枚举模型(给模型)
                                          │ reply(structured_schema=...)
                                          ▼
                                    动态结果  ──to_core_result──▶ 稳定内核
                                          （存储层/API 只认稳定内核）

要建立的认知：
  1. 动态 ≠ 整个结构乱变。稳定的是 name/type/aliases/mentions/confidence/
     attributes 这套内核；会变的是"允许哪些 type 取值"和"attributes 里放什么"。
  2. type 的枚举约束不是写死在 Entity 上，而是由"领域配置"在运行时生成。
  3. 抽取结果统一转回稳定 ExtractionResult —— 下游代码/数据库表结构不随领域变化，
     领域专有字段全部收进 attributes（L08 存 PostgreSQL JSONB）。
  4. 加一个新领域 = 加一份配置数据，不改动 schema/存储/工厂的任何一行代码。

生产代码：
  src/moray/schema/domains.py   领域配置目录（将来可换成 YAML/DB）
  src/moray/schema/factory.py   配置 -> 动态模型/提示词/稳定内核 的转换

运行： .venv/bin/python lessons/l04_domain_schema.py
================================================================================
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT / "src"))

from agentscope.agent import Agent
from agentscope.credential import VolcengineCredential
from agentscope.message import UserMsg
from agentscope.model import VolcengineChatModel

from moray.schema import (
    DOMAIN_REGISTRY,
    GENERAL_DOMAIN,
    FINANCE_DOMAIN,
    DomainSpec,
    TypeSpec,
    build_extraction_model,
    build_system_prompt,
    get_domain,
    to_core_result,
    validate_against_domain,
)


# 同一段文本，喂给不同领域，观察类型与属性的差异
FINANCE_TEXT = (
    "2024年3月，甲方字节跳动有限公司与乙方上海云启信息技术有限公司签订"
    "《三年期云计算服务采购合同》，合同金额人民币5亿元，期限三年，"
    "由招商银行上海分行提供受托支付，项目负责人为李明。"
)

MEDICAL_TEXT = (
    "患者张某因2型糖尿病合并高血压就诊，医生开具二甲双胍片（0.5g，每日两次）"
    "并建议联合缬沙坦胶囊控制血压。"
)


def build_agent(system_prompt: str) -> Agent:
    cred = VolcengineCredential(
        api_key=os.environ["ARK_API_KEY"], base_url=os.environ["ARK_BASE_URL"]
    )
    return Agent(
        name="Extractor",
        system_prompt=system_prompt,
        model=VolcengineChatModel(
            credential=cred, model=os.environ["ARK_CHAT_MODEL"], stream=False
        ),
    )


async def extract(domain: DomainSpec, text: str):
    """用指定领域配置跑一次完整抽取：动态模型 -> 模型 -> 校验 -> 转稳定内核。"""
    dynamic_model = build_extraction_model(domain)
    agent = build_agent(build_system_prompt(domain))

    reply = await agent.reply(
        UserMsg(name="u", content=f"请抽取下面文本：\n{text}"),
        structured_schema=dynamic_model,
    )

    # 先按"动态模型"强校验（枚举/类型在这一步把关）
    dyn_result = dynamic_model.model_validate(reply.structured_output)
    # 再转回"稳定内核"，供存储/检索统一消费
    core = to_core_result(dyn_result)
    # 领域级二次校验（类型必须属于该领域）
    errors = validate_against_domain(core, domain)

    return core, errors


def show(title: str, domain: DomainSpec, core, errors: list[str]) -> None:
    print("\n" + "=" * 76)
    print(f"{title}  领域={domain.code}  允许类型={domain.type_codes()}")
    print("=" * 76)
    for e in core.entities:
        attr = f"  attributes={e.attributes}" if e.attributes else ""
        print(f"  [{e.type:8}] {e.name}{attr}")
    for r in core.relations:
        print(f"  关系: ({r.head}) —[{r.predicate}]→ ({r.tail})")
    print("  领域校验:", "通过 ✅" if not errors else f"不通过 ❌ {errors}")


async def main() -> None:
    # ---- 场景 1：同一份金融文本，用【通用领域】抽 ----
    core_general, err_g = await extract(GENERAL_DOMAIN, FINANCE_TEXT)
    show("场景 1 · 通用领域视角", GENERAL_DOMAIN, core_general, err_g)
    print("  👉 注意：通用领域只有 PER/ORG/LOC/EVENT/MISC，抽不出合同/金额类型。")

    # ---- 场景 2：同一份文本，用【金融合同领域】抽（热插拔后的类型+属性）----
    core_fin, err_f = await extract(FINANCE_DOMAIN, FINANCE_TEXT)
    show("场景 2 · 金融合同领域视角", FINANCE_DOMAIN, core_fin, err_f)
    print("  👉 注意：出现了 CONTRACT/PARTY，金额被规范化进 attributes.amount。")

    # ---- 场景 3：现场新注册一个【医疗领域】，不改内核任何代码 ----
    medical = DomainSpec(
        code="medical",
        name="医疗病历",
        types=(
            TypeSpec("DISEASE", "疾病", "疾病/诊断名称",
                     attribute_hints=("icd10", "chronic"),
                     attributes_example={"icd10": "E11.9", "chronic": True}),
            TypeSpec("DRUG", "药物", "药品通用名",
                     attribute_hints=("dose", "frequency", "form"),
                     attributes_example={"dose": "0.5g", "frequency": "BID"}),
            TypeSpec("SYMPTOM", "症状", "患者表现出的症状体征"),
            TypeSpec("PATIENT", "患者", "就诊患者（可用脱敏代称）"),
        ),
        relation_predicates=("患有", "使用药物", "表现为", "联合用药"),
        extra_instruction="药物剂量、频次放入对应 DRUG 的 attributes；患者用脱敏代称。",
    )
    # 注册进目录（生产中这一步发生在服务启动，配置来自 YAML/DB）
    DOMAIN_REGISTRY[medical.code] = medical

    core_med, err_m = await extract(get_domain("medical"), MEDICAL_TEXT)
    show("场景 3 · 运行时新注册的医疗领域", medical, core_med, err_m)
    print("  👉 注意：这是代码运行时临时定义并注册的领域，内核/存储一行没改。")

    # ---- 关键：三个领域的产物，落库时都是同一种稳定结构 ----
    print("\n" + "=" * 76)
    print("存储层视角：金融合同实体的稳定内核 JSON（attributes 是动态口袋）")
    print("=" * 76)
    contract = next((e for e in core_fin.entities if e.type == "CONTRACT"), None)
    if contract:
        import json
        print(json.dumps(contract.model_dump(), ensure_ascii=False, indent=2))

    print("\n# 动手任务：")
    print("# 1. 在 domains.py 里给 FINANCE_DOMAIN 再加一个类型 GUARANTOR(担保方)，")
    print("#    重跑场景2，无需改 Entity/factory/存储即可看到新类型生效。")
    print("# 2. 故意用金融领域模型去 model_validate 一个 type='PER' 的结果，")
    print("#    观察动态枚举在 Pydantic 层直接报错。")
    print("# 3. 把医疗领域配置改写成一个 dict（参考 domains.py 字段），体会它")
    print("#    将来如何直接从 YAML/数据库加载。")


if __name__ == "__main__":
    asyncio.run(main())
