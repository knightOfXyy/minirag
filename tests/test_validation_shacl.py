"""shacl_validator 离线测试：合规/违约/结构化报告/离线安全/降级。"""

import pytest

from moray.validation import ValidationRequest, validate_extraction
from moray.validation.schemas import EntityIn, RelationIn


def _req(entities, relations=None, domain="general"):
    return ValidationRequest(
        entities=[EntityIn(**e) for e in entities],
        relations=[RelationIn(**r) for r in (relations or [])],
        domain_code=domain,
    )


@pytest.mark.asyncio
async def test_valid_entities_conform():
    req = _req([
        {"name": "张三", "type": "PER", "attributes": {"email": "z@x.cn"}},
        {"name": "某研究所", "type": "ORG"},
    ])
    resp = await validate_extraction(req)
    assert resp.conforms is True
    assert resp.violations == []
    assert resp.degraded is False
    assert resp.entity_count == 2


@pytest.mark.asyncio
async def test_person_missing_name_violates_min_count():
    # 名称为空串：mapper 不写 name 三元组 → SHACL minCount 违约
    req = _req([{"name": "", "type": "PER"}])
    resp = await validate_extraction(req)
    assert resp.conforms is False
    assert any(v.constraint.endswith("MinCountConstraintComponent") for v in resp.violations)
    # 每条违约都能定位到主体和属性
    v = resp.violations[0]
    assert v.focus_node
    assert "name" in v.path


@pytest.mark.asyncio
async def test_bad_email_pattern_violates():
    req = _req([
        {"name": "李四", "type": "PER", "attributes": {"email": "not-an-email"}},
    ])
    resp = await validate_extraction(req)
    assert resp.conforms is False
    assert any("PatternConstraintComponent" in v.constraint for v in resp.violations)
    assert any("邮箱" in v.message for v in resp.violations)


@pytest.mark.asyncio
async def test_violation_value_is_captured():
    req = _req([
        {"name": "王五", "type": "PER", "attributes": {"email": "bad"}},
    ])
    resp = await validate_extraction(req)
    email_viol = next(v for v in resp.violations if "email" in v.path)
    assert email_viol.value == "bad"


@pytest.mark.asyncio
async def test_empty_request_conforms():
    resp = await validate_extraction(_req([]))
    assert resp.conforms is True
    assert resp.entity_count == 0


@pytest.mark.asyncio
async def test_relation_count_reflected():
    req = _req(
        [{"name": "张三", "type": "PER"}, {"name": "某所", "type": "ORG"}],
        relations=[{"head": "张三", "predicate": "任职于", "tail": "某所"}],
    )
    resp = await validate_extraction(req)
    assert resp.relation_count == 1
    assert resp.conforms is True


@pytest.mark.asyncio
async def test_unknown_domain_falls_back_to_general_shapes():
    # 未配置形状的领域代码不报错，回退通用形状
    req = _req([{"name": "赵六", "type": "PER"}], domain="nonexistent-domain")
    resp = await validate_extraction(req)
    assert resp.conforms is True
    assert resp.degraded is False


@pytest.mark.asyncio
async def test_validator_is_offline_no_remote_fetch(monkeypatch):
    # 守护离线约束：校验过程不得发起任何网络请求。
    # urllib 是 rdflib 远程解析的底层通道，若被调用即视为破坏离线保证。
    import urllib.request as ur

    def _boom(*a, **k):
        raise AssertionError("校验过程不允许联网抓取远程词汇/context")

    monkeypatch.setattr(ur, "urlopen", _boom)
    req = _req([{"name": "离线人", "type": "PER"}])
    resp = await validate_extraction(req)
    assert resp.conforms is True
