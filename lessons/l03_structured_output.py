"""
================================================================================
L03 · 结构化输出：让 Agent 吐出能直接入库的实体对象（项目地基）
================================================================================

前两课模型返回的是"自由文本"，没法可靠入库。这节课解决：
    怎么强制模型输出一份 程序可校验、类型安全、能直接落库 的数据？

你要建立 5 个认知：

  认知 1：结构化输出 = AgentScope 给模型"临时发一个只准填一次的工具"
           看源码会发现：reply(..., structured_schema=SomeModel) 时，框架自动
           注册一个内置工具 GenerateStructuredOutput（read-only，自动放行），
           它的 input_schema 就是你的 Pydantic 模型。模型必须调用它才算答完。
           所以它本质复用了 L02 的 function calling——这也是为什么只要模型
           支持工具调用，就能用结构化输出（我们的 ARK 网关已实测支持）。

  认知 2：Pydantic 模型有"两副面孔"
           · 给程序：强类型、自动校验（类型错/枚举越界/缺字段直接报错）
           · 给模型：model_json_schema() 变成 JSON Schema，
                    Field(description=...) 就是写给模型的抽取指令

  认知 3：结果在 Msg.structured_output 上（一个 dict）
           · await reply(msg, structured_schema=X)
               -> reply_msg.structured_output 是 dict
               -> X.model_validate(...) 还原成强类型对象
           · reply_stream(..., yield_final_msg=True)
               -> 流的最后一个元素是完整 assistant Msg，同样取 structured_output

  认知 4（最重要的工程认知）：Schema 设计 = 抽取质量
           模型把"1999/周四"也当实体，不是模型笨，而是你的类型和描述没约束它。
           类型用 Literal 枚举受控、MISC 描述里明确"纯数字不要抽"，立刻改善。

  认知 5：provenance（出处）必须从第一天就内建
           企业知识库要能回答"这条实体从哪个文档哪句话来的"，否则无法纠错审计。
           我们的 Schema 里每个实体/关系都带 span / evidence。

生产代码在 src/moray/schema/entities.py，本课直接 import 使用。

运行： .venv/bin/python lessons/l03_structured_output.py
================================================================================
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT / "src"))

from agentscope.agent import Agent
from agentscope.credential import VolcengineCredential
from agentscope.message import Msg, UserMsg
from agentscope.model import VolcengineChatModel

from moray.schema import ExtractionResult  # 生产 Schema


SAMPLE_TEXT = (
    "阿里巴巴（简称阿里，NYSE:BABA）由马云等人于1999年在杭州创立，"
    "总部位于杭州。2024年，阿里巴巴宣布其云计算子公司将在张家口投建新数据中心。"
    "苹果发布了新手机，果农也在卖苹果"
)


def build_agent(system_prompt: str) -> Agent:
    cred = VolcengineCredential(
        api_key=os.environ["ARK_API_KEY"], base_url=os.environ["ARK_BASE_URL"]
    )
    model = VolcengineChatModel(
        credential=cred, model=os.environ["ARK_CHAT_MODEL"], stream=False
    )
    return Agent(name="Extractor", system_prompt=system_prompt, model=model)


# -----------------------------------------------------------------------------
# Demo A：最朴素的 Schema（没有类型说明、没有别名/出处）——故意暴露问题
# -----------------------------------------------------------------------------
class NaiveEntity(BaseModel):
    name: str
    type: str  # 注意：纯 str，模型可以随便造类型
    description: str
class NaiveResult(BaseModel):
    entities: list[NaiveEntity]


async def demo_naive() -> None:
    print("\n" + "=" * 74)
    print("Demo A：朴素 Schema（type 是自由 str，无约束）——看它会抽成什么样")
    print("=" * 74)

    agent = build_agent("你是实体抽取助手，从文本中抽取命名实体。")
    reply = await agent.reply(
        UserMsg(name="u", content=f"文本：{SAMPLE_TEXT}\n请抽取实体。"),
        structured_schema=NaiveResult,
    )
    result = NaiveResult.model_validate(reply.structured_output)

    print(f"抽到 {len(result.entities)} 个实体：")
    for e in result.entities:
        print(f"  {e.type:<8} {e.name}")
    print("\n⚠️  观察问题：")
    print("   · 类型可能五花八门（公司/企业/ORG 混用），无法聚合统计")
    print("   · 「1999」「2024年」等年份可能被当实体（脏数据）")
    print("   · 没有别名：阿里 和 阿里巴巴 可能被当成两个实体")
    print("   · 没有出处：错了你也不知道是从哪句话抽的")


# -----------------------------------------------------------------------------
# Demo B：生产级 Schema（枚举 + 类型引导 + 别名 + 出处）——质量对比
# -----------------------------------------------------------------------------
async def demo_production() -> ExtractionResult:
    print("\n" + "=" * 74)
    print("Demo B：生产级 Schema（受控枚举/别名/出处/关系）——同一个模型")
    print("=" * 74)

    # 注：L04 起，类型枚举与提示词由"领域配置"动态生成（见 l04_domain_schema.py）。
    # 这里用通用领域的工厂，等价于 L03 当初写死的 PER/ORG/LOC/EVENT/MISC。
    from moray.schema import (
        GENERAL_DOMAIN,
        build_extraction_model,
        build_system_prompt,
        to_core_result,
    )

    general_model = build_extraction_model(GENERAL_DOMAIN)
    system_prompt = build_system_prompt(GENERAL_DOMAIN)

    agent = build_agent(system_prompt)
    reply = await agent.reply(
        UserMsg(name="u", content=f"请从下面文本抽取实体与关系：\n{SAMPLE_TEXT}"),
        structured_schema=general_model,
    )

    # ★ 先按（带枚举的）动态模型强校验，再转回稳定内核 ExtractionResult
    result = to_core_result(general_model.model_validate(reply.structured_output))

    print(f"\n✅ 抽取结果（已通过 Pydantic 校验）：实体 {len(result.entities)} 个，"
          f"关系 {len(result.relations)} 条\n")

    print("【实体】")
    for e in result.entities:
        alias = f"  别名={e.aliases}" if e.aliases else ""
        conf = f"  置信度={e.confidence:.2f}" if e.confidence else ""
        print(f"  [{e.type:5}] {e.name}{alias}{conf}")
        for m in e.mentions:
            pos = f"[{m.span.start}:{m.span.end}]" if m.span else ""
            print(f"         └─ 原文提及 {pos} 「{m.surface}」")

    print("\n【关系三元组】")
    for r in result.relations:
        print(f"  ({r.head}) —[{r.predicate}]→ ({r.tail})"
              f"{'  置信度=%.2f' % r.confidence if r.confidence else ''}")

    # 演示：出处真的能指回原文
    print("\n【溯源演示】取第一个实体的第一个 mention，回原文高亮：")
    ent = result.entities[0]
    if ent.mentions and ent.mentions[0].span:
        s = ent.mentions[0].span
        snippet = SAMPLE_TEXT[s.start:s.end]
        print(f"  span=[{s.start}:{s.end}] 切出的原文 = 「{snippet}」"
              f"  与记录「{s.text}」{'一致 ✅' if snippet == s.text else '不一致 ❌'}")

    return result


# -----------------------------------------------------------------------------
# Demo C：reply_stream 下怎么拿结构化结果（yield_final_msg=True）
# -----------------------------------------------------------------------------
async def demo_stream() -> None:
    print("\n" + "=" * 74)
    print("Demo C：流式场景拿结构化结果（yield_final_msg=True，末尾是完整 Msg）")
    print("=" * 74)

    agent = build_agent("你是实体抽取专家，只抽取明确出现的命名实体。")
    msg = UserMsg(name="u", content="抽取实体：字节跳动总部位于北京，创始人张一鸣。")

    from agentscope.event import EventType

    final_msg: Msg | None = None
    tool_seen = False
    async for event in agent.reply_stream(
        msg, structured_schema=ExtractionResult, yield_final_msg=True
    ):
        # 注意：yield_final_msg=True 时，流里既有事件也有最后一个完整 Msg。
        # Msg 没有 .type，必须先做 isinstance 判断，顺序不能反。
        if isinstance(event, Msg):
            final_msg = event
        elif event.type == EventType.TOOL_CALL_START:
            # 能"看见"模型其实是在调用那个内置结构化工具
            tool_seen = True
            print(f"🔧 模型调用内置结构化工具：{event.tool_call_name}")

    print("是否观察到结构化工具调用：", tool_seen)
    result = ExtractionResult.model_validate(final_msg.structured_output)
    print("结构化实体：", [(e.type, e.name) for e in result.entities])


# -----------------------------------------------------------------------------
# Demo D：校验失败长什么样（企业里要捕获并触发重试/纠错）
# -----------------------------------------------------------------------------
async def demo_validation() -> None:
    print("\n" + "=" * 74)
    print("Demo D：强类型校验如何拦下脏数据")
    print("=" * 74)

    # L04 起校验分两层，这里分别演示：
    from moray.schema import GENERAL_DOMAIN, build_extraction_model

    general_model = build_extraction_model(GENERAL_DOMAIN)

    # ① 动态模型层：拦"枚举越界"（type=企业集团 不在 PER/ORG/... 中）
    dirty_type = {"entities": [{"name": "某公司", "type": "企业集团"}], "relations": []}
    try:
        general_model.model_validate(dirty_type)
    except ValidationError as exc:
        print("❌ 动态模型拦截非法类型：")
        for err in exc.errors():
            print(f"   · 位置 {err['loc']}: {err['type']} —— {err['msg'][:70]}")

    # ② 稳定内核层：拦"数值越界"（confidence=1.5，任何领域都不允许）
    dirty_conf = {
        "entities": [{"name": "某公司", "type": "ORG", "confidence": 1.5}],
        "relations": [],
    }
    try:
        ExtractionResult.model_validate(dirty_conf)
    except ValidationError as exc:
        print("\n❌ 稳定内核拦截越界置信度：")
        for err in exc.errors():
            print(f"   · 位置 {err['loc']}: {err['type']} —— {err['msg'][:70]}")

    print("\n👉 两层分工：动态模型管'这个领域允许哪些类型'，内核管'永远成立的规则'。")
    print("   真实系统捕获 ValidationError -> 让模型按错误重试（第10课校验闭环）。")


async def main() -> None:
    await demo_naive()
    await demo_production()
    await demo_stream()
    await demo_validation()

    print("\n" + "#" * 74)
    print("# 动手任务：")
    print("# 1. 在 SAMPLE_TEXT 里加一句带歧义的话（如「苹果发布了新手机，")
    print("#    果农也在卖苹果」），观察模型如何区分 ORG 与普通名词。")
    print("# 2. 给 Entity 增加一个领域无关的属性（如 description:str），")
    print("#    在 description 里写清楚要求，重跑看模型是否会补一段实体简介。")
    print("# 3. 把 MISC 的引导文字删掉，连跑 3 次，统计「年份被误抽」的比例，")
    print("#    体会 prompt/字段描述对稳定性的影响。")
    print("#" * 74)


if __name__ == "__main__":
    asyncio.run(main())
