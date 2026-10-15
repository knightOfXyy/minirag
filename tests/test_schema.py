"""Schema 契约层的单测（不调用任何模型，纯本地校验）。"""

import pytest
from pydantic import ValidationError

from moray.schema import (
    Entity,
    ExtractionResult,
    FINANCE_DOMAIN,
    GENERAL_DOMAIN,
    Relation,
    Span,
    build_extraction_model,
    get_domain,
    to_core_result,
    validate_against_domain,
)


def test_entity_defaults():
    e = Entity(name="阿里巴巴", type="ORG")
    assert e.aliases == []
    assert e.mentions == []
    assert e.confidence == 0.0
    assert e.attributes == {}


def test_core_type_is_free_string_but_domain_validates():
    """核心 Entity 的 type 是稳定字符串；枚举约束由领域层负责（动态 schema）。"""
    # 核心层不再因类型字符串报错（存储稳定，允许任意领域代码）
    e = Entity(name="某主体", type="PARTY")
    assert e.type == "PARTY"

    # 但通用领域校验会拒绝 PARTY
    bad = ExtractionResult(entities=[Entity(name="某主体", type="PARTY")])
    errors = validate_against_domain(bad, GENERAL_DOMAIN)
    assert errors and "PARTY" in errors[0]

    # 金融领域则接受 PARTY
    ok = ExtractionResult(entities=[Entity(name="某主体", type="PARTY")])
    assert validate_against_domain(ok, FINANCE_DOMAIN) == []


def test_dynamic_model_enum_matches_domain():
    """动态生成的模型，其 type 枚举必须等于领域配置的类型码。"""
    model = build_extraction_model(FINANCE_DOMAIN)
    literal = model.model_fields["entities"].annotation
    # 构造一个越界类型应被动态模型拒绝
    payload = {"entities": [{"name": "x", "type": "ORG"}], "relations": []}
    with pytest.raises(ValidationError):
        model.model_validate(payload)
    # 合法领域类型通过
    payload["entities"][0]["type"] = "CONTRACT"
    assert model.model_validate(payload).entities[0].type == "CONTRACT"


def test_to_core_result_is_domain_agnostic():
    """任意领域的动态结果都能转成同一套稳定内核（下游无感知）。"""
    model = build_extraction_model(get_domain("finance"))
    dyn = model.model_validate({
        "entities": [{
            "name": "采购合同", "type": "CONTRACT",
            "attributes": {"amount": 500000000, "currency": "CNY"},
        }],
        "relations": [],
    })
    core = to_core_result(dyn)
    assert isinstance(core, ExtractionResult)
    assert core.entities[0].type == "CONTRACT"
    assert core.entities[0].attributes["amount"] == 500000000


def test_confidence_bounds():
    with pytest.raises(ValidationError):
        Entity(name="x", type="PER", confidence=1.5)
    # 边界值合法
    assert Entity(name="x", type="PER", confidence=1.0).confidence == 1.0


def test_span_negative_rejected():
    with pytest.raises(ValidationError):
        Span(start=-1, end=3, text="x")


def test_extraction_result_roundtrip():
    """模拟模型返回的 dict -> 强类型对象 -> 再序列化的往返。"""
    raw = {
        "entities": [
            {
                "name": "阿里巴巴",
                "type": "ORG",
                "aliases": ["阿里"],
                "mentions": [
                    {"surface": "阿里", "span": {"start": 7, "end": 9, "text": "阿里"}}
                ],
                "confidence": 0.98,
            }
        ],
        "relations": [
            {
                "head": "马云",
                "predicate": "创立",
                "tail": "阿里巴巴",
                "confidence": 0.95,
            }
        ],
    }
    result = ExtractionResult.model_validate(raw)
    assert len(result.entities) == 1
    assert result.entities[0].mentions[0].span.start == 7
    assert result.relations[0].predicate == "创立"

    # 再 dump 回 dict，别名等字段完整保留
    again = ExtractionResult.model_validate(result.model_dump())
    assert again.entities[0].aliases == ["阿里"]
    assert isinstance(again.relations[0], Relation)
