"""Schema 契约层的单测（不调用任何模型，纯本地校验）。"""

import pytest
from pydantic import ValidationError

from moray.schema import Entity, ExtractionResult, Relation, Span


def test_entity_defaults():
    e = Entity(name="阿里巴巴", type="ORG")
    assert e.aliases == []
    assert e.mentions == []
    assert e.confidence == 0.0


def test_invalid_entity_type_rejected():
    """受控枚举：模型自造的类型必须在校验阶段被拦下。"""
    with pytest.raises(ValidationError) as exc:
        Entity(name="某公司", type="企业集团")
    assert "type" in str(exc.value)


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
