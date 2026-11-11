"""talent-person 领域 SHACL 校验离线测试（人员档案枚举/格式约束）。"""

import pytest

from moray.validation import ValidationRequest, validate_extraction
from moray.validation.schemas import EntityIn


def _person(attrs):
    return ValidationRequest(
        entities=[EntityIn(name=attrs.pop("name", "测试人"), type="PER", attributes=attrs)],
        domain_code="talent-person",
    )


@pytest.mark.asyncio
async def test_valid_talent_person_conforms():
    req = _person({
        "gender": "男",
        "politicalStatus": "中共党员",
        "employmentType": "全职",
        "secretLevel": "秘密",
        "riskLevel": "高",
        "phone": "13812345678",
        "idNumber": "11010119900307889X",
    })
    resp = await validate_extraction(req)
    assert resp.conforms is True, [v.model_dump() for v in resp.violations]


@pytest.mark.asyncio
async def test_bad_gender_enum_violates():
    resp = await validate_extraction(_person({"gender": "未知"}))
    assert resp.conforms is False
    v = next(x for x in resp.violations if x.path == "gender")
    assert "InConstraintComponent" in v.constraint


@pytest.mark.asyncio
async def test_bad_phone_pattern_violates():
    resp = await validate_extraction(_person({"phone": "12345"}))
    assert resp.conforms is False
    assert any(x.path == "phone" and "PatternConstraintComponent" in x.constraint for x in resp.violations)


@pytest.mark.asyncio
async def test_bad_id_number_pattern_violates():
    resp = await validate_extraction(_person({"idNumber": "123"}))
    assert resp.conforms is False
    assert any(x.path == "idNumber" for x in resp.violations)


@pytest.mark.asyncio
async def test_empty_optional_fields_do_not_violate():
    # 只给必填 name，其余留空不应触发 pattern/in 约束（mapper 跳过空值）
    resp = await validate_extraction(_person({}))
    assert resp.conforms is True
    assert resp.violations == []


@pytest.mark.asyncio
async def test_multiple_enum_fields_violations_reported_separately():
    resp = await validate_extraction(_person({
        "gender": "X", "secretLevel": "瞎填", "riskLevel": "超高",
    }))
    paths = {v.path for v in resp.violations}
    assert {"gender", "secretLevel", "riskLevel"} <= paths


@pytest.mark.asyncio
async def test_violation_focus_node_identifies_entity():
    resp = await validate_extraction(_person({"gender": "X"}))
    assert resp.violations
    assert all(v.focus_node.startswith("http://talent.local/entity/") for v in resp.violations)
