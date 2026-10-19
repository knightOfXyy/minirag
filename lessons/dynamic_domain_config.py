"""
================================================================================
动态领域配置（PostgreSQL 配置中心 · 热更）演示
================================================================================

需求：不改代码、不重启服务，运营就能"动态定义要抽哪些实体、每类要哪些属性"。

实现：把领域包（实体类型 + 结构化属性规格）存进 domain_configs 表，
      · DbDomainResolver 按 updated_at 版本缓存解析 DomainSpec；
      · DynamicExtractorProvider 按 (code, 版本) 缓存 Extractor，配置变了才重建；
      · /extract 与 worker 都按请求/作业的 domain_code 取最新配置。

本脚本离线演示配置链路（不调 LLM，抽取用桩替代）：
  1) 导入一个全新的「招聘 recruit」领域（JOB/SKILL + 必填/类型/枚举属性）；
  2) 解析它生成动态结构化模型（模型 JSON Schema 已含新类型和属性约束）；
  3) 直接 UPDATE 表给 JOB 增加必填属性 city —— 不重启，再次解析立即生效；
  4) 属性校验：缺必填 / 类型错 / 枚举越界都能被 validate_against_domain 抓住。

真实环境把"桩抽取器"换成 DynamicExtractorProvider 即可，见：
  python -m moray.config_center.cli import recruit.json
  POST /extract {"text": ..., "domain_code": "recruit"}
================================================================================
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")


RECRUIT = {
    "code": "recruit", "name": "招聘",
    "relation_predicates": ["要求掌握"],
    "extra_instruction": "只抽与岗位招聘直接相关的实体。",
    "types": [
        {"code": "JOB", "label": "职位", "guide": "招聘岗位", "attributes": [
            {"key": "title", "label": "职位名", "required": True, "type": "string"},
            {"key": "salary_min", "label": "最低月薪", "type": "integer"},
            {"key": "seniority", "label": "级别", "type": "string",
             "enum": ["初级", "中级", "高级"]},
        ]},
        {"code": "SKILL", "label": "技能", "guide": "要求的技能点"},
    ],
}


async def main() -> None:
    from moray.config_center import DbDomainResolver, DomainConfigStore
    from moray.kb import close_pool, init_pool
    from moray.schema import (
        build_extraction_model, build_system_prompt,
        domain_from_dict, domain_to_dict, validate_against_domain,
    )
    from moray.schema import Entity, ExtractionResult

    pool = await init_pool()
    store = DomainConfigStore(pool)
    await store.init_schema()
    resolver = DbDomainResolver(pool)

    # ① 管理端写入一个全新领域（等价 CLI import / 运营改表）
    await store.upsert(domain_from_dict(RECRUIT))
    print("① 已写入领域 recruit（JOB/SKILL + 属性规格），代码未改动")

    # ② 抽取侧解析最新配置，动态生成结构化模型与提示词
    d = await resolver.resolve("recruit")
    model = build_extraction_model(d)
    blob = json.dumps(model.model_json_schema(), ensure_ascii=False)
    print(f"② 动态模型已含类型 JOB/SKILL：{'JOB' in blob and 'SKILL' in blob}；"
          f"属性键 salary_min 进入 schema：{'salary_min' in blob}")
    print("   系统提示词片段：", build_system_prompt(d).splitlines()[2])

    # ③ 运营直接改表：给 JOB 增加必填属性 city（模拟热更，全程无重启）
    spec = domain_to_dict(d)
    spec["types"][0]["attributes"].append(
        {"key": "city", "label": "工作地", "required": True, "type": "string"})
    await store.upsert(domain_from_dict(spec))

    d2 = await resolver.resolve("recruit")  # 下一请求即新版
    keys = {a.key for a in d2.get("JOB").attributes}
    print(f"③ 改表后再次解析（无重启），JOB 属性变为：{sorted(keys)}")
    assert "city" in keys

    # ④ 属性校验：模拟模型返回的结构化结果，检查必填/类型/枚举
    bad = ExtractionResult(entities=[
        Entity(name="后端工程师", type="JOB", attributes={
            # 缺 title(必填)、缺 city(必填)、salary_min 类型错、seniority 越界
            "salary_min": "两万", "seniority": "专家"}),
        Entity(name="Python", type="SKILL"),
    ])
    errors = validate_against_domain(bad, d2)
    print(f"④ 属性校验发现 {len(errors)} 个问题：")
    for e in errors:
        print("   -", e)

    good = ExtractionResult(entities=[
        Entity(name="后端工程师", type="JOB", attributes={
            "title": "后端工程师", "salary_min": 20000,
            "seniority": "高级", "city": "深圳", "bonus": "未声明键放行"}),
        Entity(name="Python", type="SKILL"),
    ])
    print("   合规结果的问题数：", len(validate_against_domain(good, d2)))

    print("\n可用管理命令：")
    print("  PYTHONPATH=src .venv/bin/python -m moray.config_center.cli list")
    print("  PYTHONPATH=src .venv/bin/python -m moray.config_center.cli show recruit")
    await close_pool(pool)


if __name__ == "__main__":
    asyncio.run(main())
