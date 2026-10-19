"""MCP server 工具的进程内集成测试（需要 moray-pgvector）。

用 FastMCP 的 call_tool 在同一进程直接调用工具函数，不拉起 stdio 子进程；
嵌入用确定性 FakeEmbedder。无 DB 自动 skip。
    RUN_DB_TESTS=1 .venv/bin/python -m pytest tests/test_mcp_server_integration.py -q
"""

import json
import os

import numpy as np
import pytest

from moray.kb import (
    EntityStore,
    Ingestor,
    KnowledgeBase,
    close_pool,
    init_pool,
)
from moray.mcp_server import build_server
from moray.schema import Entity, ExtractionResult, Relation
from moray.extraction.document import ExtractionOutput

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DB_TESTS") != "1",
    reason="需要 PostgreSQL；设置 RUN_DB_TESTS=1 且启动 moray-pgvector 后运行",
)


class FakeEmbedder:
    dim = 512

    def __init__(self):
        self._cache = {}

    def embed(self, texts):
        single = isinstance(texts, str)
        items = [texts] if single else texts
        out = np.array([self._one(t) for t in items], dtype=np.float32)
        return out[0] if single else out

    def embed_one(self, text):
        return self._one(text).tolist()

    def _one(self, text):
        if text not in self._cache:
            v = np.random.default_rng(abs(hash(text)) % (2 ** 32)).normal(
                size=self.dim).astype(np.float32)
            v /= np.linalg.norm(v)
            self._cache[text] = v
        return np.array(self._cache[text])


async def _payload(mcp, name, args):
    """从 FastMCP call_tool 结果里解析 JSON。

    FastMCP 会把 list 的每个元素作为独立 TextContent（形如 {...}{...}），
    因此要按顺序解码多个 JSON 文档：多段 -> 合并成 list，单段 -> 原样。
    """
    contents, _ = await mcp.call_tool(name, args)
    text = "".join(getattr(b, "text", str(b)) for b in contents).strip()
    if not text:
        return None
    decoder = json.JSONDecoder()
    vals, idx, n = [], 0, len(text)
    while idx < n:
        while idx < n and text[idx].isspace():
            idx += 1
        if idx >= n:
            break
        obj, idx = decoder.raw_decode(text, idx)
        vals.append(obj)
    return vals if len(vals) > 1 else vals[0]


def _as_list(x):
    """FastMCP 对单元素 list 只输出一个 JSON 对象，这里统一包回 list。"""
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


@pytest.fixture()
async def mcp():
    pool = await init_pool()
    store = EntityStore(pool)
    await store.init_schema()
    await pool.execute("TRUNCATE entities RESTART IDENTITY CASCADE")
    emb = FakeEmbedder()

    out = ExtractionOutput(
        document_id="mcp-doc", domain_code="general",
        result=ExtractionResult(
            entities=[
                Entity(name="腾讯控股有限公司", type="ORG", confidence=0.9,
                       aliases=["腾讯"]),
                Entity(name="深圳", type="LOC", confidence=0.9),
                Entity(name="马化腾", type="PER", confidence=0.9),
            ],
            relations=[
                Relation(head="腾讯控股有限公司", predicate="总部位于",
                         tail="深圳", confidence=0.85),
                Relation(head="马化腾", predicate="创立",
                         tail="腾讯控股有限公司", confidence=0.85),
            ],
        ),
    )
    await Ingestor(store, emb).ingest(out)
    server = build_server(KnowledgeBase(store, emb))
    yield server
    await close_pool(pool)


@pytest.mark.asyncio
async def test_locate_by_alias(mcp):
    res = _as_list(await _payload(mcp, "locate_entity", {"query": "腾讯", "k": 1}))
    assert res and res[0]["name"] == "腾讯控股有限公司"
    assert res[0]["similarity"] == 1.0  # 精确别名命中


@pytest.mark.asyncio
async def test_entity_card_has_edges(mcp):
    card = await _payload(mcp, "get_entity_card", {"name_or_id": "腾讯"})
    assert card["name"] == "腾讯控股有限公司"
    out_preds = [r["predicate"] for r in card["outgoing"]]
    in_preds = [r["predicate"] for r in card["incoming"]]
    assert "总部位于" in out_preds
    assert "创立" in in_preds


@pytest.mark.asyncio
async def test_list_relations_directions(mcp):
    out = _as_list(await _payload(mcp, "list_relations",
                                  {"name": "腾讯", "direction": "out"}))
    assert {r["target"] for r in out} == {"深圳"}

    inc = _as_list(await _payload(mcp, "list_relations",
                                  {"name": "腾讯", "direction": "in"}))
    assert {r["source"] for r in inc} == {"马化腾"}

    none = await _payload(mcp, "list_relations",
                          {"name": "不存在的实体", "direction": "both"})
    assert not none  # 空 list 经 MCP 序列化为空载荷


@pytest.mark.asyncio
async def test_find_similar_returns_list(mcp):
    # FakeEmbedder 随机向量相似度可能偏低，测试里把阈值降到 0，只验证过滤与封装
    res = _as_list(await _payload(mcp, "find_similar",
                                  {"query": "某公司", "entity_type": "ORG", "k": 3,
                                   "min_similarity": -1.0}))
    assert res and all(r["type"] == "ORG" for r in res)


@pytest.mark.asyncio
async def test_tools_listed(mcp):
    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    assert {"locate_entity", "get_entity_card", "find_similar",
            "list_relations"} <= names
