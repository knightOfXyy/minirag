"""DB 配置中心（domain_configs 热更）集成测试。需要 moray-pgvector。

    RUN_DB_TESTS=1 .venv/bin/python -m pytest tests/test_config_center_integration.py -q
"""

import os

import pytest

from moray.config_center import DbDomainResolver, DomainConfigStore
from moray.kb import close_pool, init_pool
from moray.schema import (
    AttrSpec,
    DomainSpec,
    TypeSpec,
    build_extraction_model,
    build_system_prompt,
    domain_from_dict,
    domain_to_dict,
    validate_against_domain,
)
from moray.schema.entities import Entity, ExtractionResult

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DB_TESTS") != "1",
    reason="需要 PostgreSQL；设置 RUN_DB_TESTS=1 且启动 moray-pgvector 后运行",
)


@pytest.fixture()
async def resolver():
    pool = await init_pool()
    await pool.execute("DROP TABLE IF EXISTS domain_configs")
    store = DomainConfigStore(pool)
    await store.init_schema()
    res = DbDomainResolver(pool)
    yield res
    await close_pool(pool)


def _custom_domain():
    return DomainSpec(
        code="recruit", name="招聘",
        types=(
            TypeSpec("JOB", "职位", "招聘岗位", attributes=(
                AttrSpec("title", "职位名", required=True),
                AttrSpec("salary_min", "最低薪资", value_type="integer"),
                AttrSpec("seniority", "级别", enum=("初级", "中级", "高级")),
            )),
            TypeSpec("SKILL", "技能", "要求的技能点"),
        ),
        relation_predicates=("要求掌握",),
    )


@pytest.mark.asyncio
async def test_db_missing_falls_back_to_builtin(resolver):
    # 表里没有 general -> 回退内置常量
    d = await resolver.resolve("general")
    assert d.code == "general"
    assert any(t.code == "ORG" for t in d.types)


@pytest.mark.asyncio
async def test_upsert_and_resolve_roundtrip(resolver):
    await resolver.store.upsert(_custom_domain())
    d = await resolver.resolve("recruit")
    assert d.code == "recruit"
    job = d.get("JOB")
    assert {a.key for a in job.attributes} == {"title", "salary_min", "seniority"}
    assert job.attributes[2].enum == ("初级", "中级", "高级")
    assert "recruit" in await resolver.list_codes()


@pytest.mark.asyncio
async def test_hot_reload_without_restart(resolver):
    await resolver.store.upsert(_custom_domain())
    d1 = await resolver.resolve("recruit")
    assert d1.name == "招聘"

    # 运营直接改表：改名 + 给 JOB 增加一个必填属性
    spec = domain_to_dict(d1)
    spec["name"] = "招聘v2"
    spec["types"][0]["attributes"].append(
        {"key": "city", "label": "工作地", "required": True, "type": "string"})
    await resolver.store.upsert(domain_from_dict(spec))

    d2 = await resolver.resolve("recruit")
    assert d2.name == "招聘v2"
    assert "city" in {a.key for a in d2.get("JOB").attributes}


@pytest.mark.asyncio
async def test_serialization_is_lossless():
    d = _custom_domain()
    back = domain_from_dict(domain_to_dict(d))
    assert back == d  # frozen dataclass 全等


def test_dynamic_model_and_prompt_reflect_spec():
    d = _custom_domain()
    model = build_extraction_model(d)
    # 动态模型的 JSON Schema 里出现自定义类型 JOB/SKILL 和属性键
    schema = model.model_json_schema()
    blob = str(schema)
    assert "JOB" in blob and "SKILL" in blob and "salary_min" in blob
    prompt = build_system_prompt(d)
    assert "招聘" in prompt and "要求掌握" in prompt


def test_attribute_validation_required_type_enum():
    d = _custom_domain()
    bad = ExtractionResult(entities=[
        # 缺必填 title；salary_min 类型错；seniority 不在枚举
        Entity(name="后端工程师", type="JOB", attributes={
            "salary_min": "两万", "seniority": "专家"}),
        Entity(name="Python", type="SKILL"),
    ])
    errors = validate_against_domain(bad, d)
    joined = " | ".join(errors)
    assert "title" in joined and "必填" in joined
    assert "salary_min" in joined and "integer" in joined
    assert "seniority" in joined and "初级" in joined

    good = ExtractionResult(entities=[
        Entity(name="后端", type="JOB", attributes={
            "title": "后端工程师", "salary_min": 20000, "seniority": "高级",
            "undeclared": "放行"}),  # 未声明的键不拦
    ])
    assert validate_against_domain(good, d) == []


@pytest.mark.asyncio
async def test_seed_builtin_is_idempotent(resolver):
    n1 = await resolver.store.seed_builtin()
    n2 = await resolver.store.seed_builtin()
    assert n1 >= 2 and n2 == 0  # 第二次没有新增
    d = await resolver.resolve("finance")
    assert d.code == "finance"
