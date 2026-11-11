"""POST /validate 进程内集成测试（不起端口、不触网）。"""

import pytest
from httpx import ASGITransport, AsyncClient

from moray.api.app import create_app


@pytest.fixture()
async def client():
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        yield ac


@pytest.mark.asyncio
async def test_validate_conforms(client):
    r = await client.post("/validate", json={
        "entities": [
            {"name": "张三", "type": "PER", "attributes": {"email": "z@x.cn"}},
            {"name": "某研究所", "type": "ORG"},
        ],
        "relations": [
            {"head": "张三", "predicate": "任职于", "tail": "某研究所"},
        ],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["conforms"] is True
    assert body["violations"] == []
    assert body["entityCount"] == 2
    assert body["relationCount"] == 1
    assert body["degraded"] is False


@pytest.mark.asyncio
async def test_validate_reports_violation(client):
    r = await client.post("/validate", json={
        "entities": [
            {"name": "李四", "type": "PER", "attributes": {"email": "bad-email"}},
        ],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["conforms"] is False
    assert len(body["violations"]) >= 1
    v = body["violations"][0]
    # 字段级定位信息齐全，可供前端高亮
    assert {"focusNode", "path", "constraint", "message"} <= set(v.keys())
    assert v["path"] == "email"


@pytest.mark.asyncio
async def test_validate_empty_body_ok(client):
    r = await client.post("/validate", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["conforms"] is True and body["entityCount"] == 0


@pytest.mark.asyncio
async def test_validate_wrong_field_type_422(client):
    # entities 必须是数组；传字符串触发 Pydantic 422（统一在网关侧转 400）
    r = await client.post("/validate", json={"entities": "not-a-list"})
    assert r.status_code == 422
