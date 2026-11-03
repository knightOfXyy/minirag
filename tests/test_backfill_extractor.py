"""Extractor 瘦身回填接线测试（不联网，固定模型桩）。"""

from types import SimpleNamespace

from moray.extraction import Document, ExtractionOutput, Extractor
from moray.extraction.local_backfill import backfill_provenance
from moray.schema import Entity, Relation


class _FakeExtractor(Extractor):
    """按预设返回序列返回模型输出；记录使用的结构化 schema 与系统提示词。"""

    def __init__(self, replies, *, local_backfill=False):
        super().__init__("general", max_retries=0, local_backfill=local_backfill)
        self._replies = list(replies)
        self._calls = 0

    def _build_agent(self):
        return SimpleNamespace()

    async def _call_structured(self, agent, content):
        reply = self._replies[self._calls]
        self._calls += 1
        return SimpleNamespace(
            structured_output=reply,
            usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        )


_TEXT = "阿里巴巴由马云在杭州创立。阿里巴巴总部位于杭州。"


def _logical_model_output():
    """瘦身后模型只给逻辑实体/关系（无任何偏移、无置信度）。"""
    return {
        "entities": [
            {"name": "阿里巴巴", "type": "ORG", "aliases": ["阿里"]},
            {"name": "马云", "type": "PER"},
            {"name": "杭州", "type": "LOC"},
        ],
        "relations": [
            {"head": "马云", "predicate": "创立", "tail": "阿里巴巴"},
            {"head": "阿里巴巴", "predicate": "总部位于", "tail": "杭州"},
        ],
    }


def test_default_switch_off_is_identical_to_before():
    """开关默认关闭：schema/提示词/结果口径与改造前逐字段一致（固定桩）。"""
    # 默认构造：仍是含 mentions/confidence/evidence 的「全量」schema
    default = Extractor("general", max_retries=0)
    full = Extractor("general", max_retries=0, local_backfill=False)
    slim = Extractor("general", max_retries=0, local_backfill=True)

    full_fields = set(
        full._schema_model.model_fields["entities"].annotation.__args__[0].model_fields
    )
    slim_fields = set(
        slim._schema_model.model_fields["entities"].annotation.__args__[0].model_fields
    )
    assert "mentions" in full_fields and "confidence" in full_fields
    assert "mentions" not in slim_fields and "confidence" not in slim_fields
    # 默认与显式关闭完全一致
    assert default._schema_model.__name__ == full._schema_model.__name__
    assert default._system_prompt == full._system_prompt
    assert default.local_backfill is False


async def _run(local_backfill):
    ex = _FakeExtractor([_logical_model_output()], local_backfill=local_backfill)
    return await ex.extract(Document(id="d", text=_TEXT))


async def test_switch_on_backfills_mentions_and_evidence():
    out = await _run(True)
    res = out.result
    by_name = {e.name: e for e in res.entities}

    # 阿里巴巴出现 2 次
    assert [m.surface for m in by_name["阿里巴巴"].mentions] == ["阿里巴巴", "阿里巴巴"]
    # 置信度由确定性回填给出（有命中=1.0），不再读模型
    assert all(e.confidence in (0.0, 1.0) for e in res.entities)

    # 关系证据被回填为同句整句
    by_rel = {(r.head, r.predicate, r.tail): r for r in res.relations}
    ev = by_rel[("马云", "创立", "阿里巴巴")].evidence_span
    assert ev is not None and _TEXT[ev.start:ev.end] == ev.text

    # 回填信号挂在产物上，是确定性的块级置信度来源（序列化为纯字典）
    assert isinstance(out.backfill, dict)
    assert out.backfill["entities_grounded"] == 3
    assert out.backfill["block_confidence"] == 1.0
    assert out.backfill["relations_with_evidence"] == 2


def test_env_flag_enables_backfill_by_default(monkeypatch):
    """MORAY_LOCAL_BACKFILL=1 打开，未显式传参的 Extractor 默认走瘦身回填；默认关。"""
    from moray import config

    monkeypatch.delenv("MORAY_LOCAL_BACKFILL", raising=False)
    assert config.local_backfill_enabled() is False
    assert Extractor("general", max_retries=0).local_backfill is False

    monkeypatch.setenv("MORAY_LOCAL_BACKFILL", "1")
    assert config.local_backfill_enabled() is True
    ex_env = Extractor("general", max_retries=0)
    assert ex_env.local_backfill is True
    # 显式传 False 仍可强制关闭（即便全局开关打开）
    assert Extractor("general", max_retries=0, local_backfill=False).local_backfill is False
    monkeypatch.delenv("MORAY_LOCAL_BACKFILL", raising=False)


async def test_chunked_pipeline_backfills_then_maps_to_global(monkeypatch):
    """分块流水线：块内本地回填 -> 既有 remap 平移 -> 全局 span 对原文切片一致。"""
    from moray.extraction import Chunk, ChunkedExtractionPipeline

    full = (
        "阿里巴巴由马云在杭州创立。"
        "腾讯总部位于深圳，马化腾主导。"
        "阿里巴巴再次在杭州投资。"
    )
    # 句子边界对齐的块（Chunker 在真实语料上正是句子成块；这里手工给三块）。
    s1, s2 = full.index("。") + 1, full.index("。", 14) + 1
    chunker_chunks = [
        Chunk(id="d#c0", text=full[0:s1], start=0, end=s1, index=0),
        Chunk(id="d#c1", text=full[s1:s2], start=s1, end=s2, index=1),
        Chunk(id="d#c2", text=full[s2:], start=s2, end=len(full), index=2),
    ]
    # 不变量：每块都是原文的真实切片（与 Chunker 的自检一致）
    for c in chunker_chunks:
        assert full[c.start:c.end] == c.text

    class _ChunkFake(_FakeExtractor):
        def __init__(self):
            # 每块都返回该块可见的逻辑实体/关系（瘦身后的模型行为：无偏移）
            super().__init__([], local_backfill=True)

        async def extract(self, document):
            from moray.schema import ExtractionResult as ER
            t = document.text
            ents, rels = [], []
            if "阿里巴巴" in t:
                ents.append(Entity(name="阿里巴巴", type="ORG"))
            if "马云" in t:
                ents.append(Entity(name="马云", type="PER"))
            if "马云" in t and "阿里巴巴" in t:
                rels.append(Relation(head="马云", predicate="创立", tail="阿里巴巴"))
            if "腾讯" in t:
                ents += [Entity(name="腾讯", type="ORG"),
                         Entity(name="深圳", type="LOC"),
                         Entity(name="马化腾", type="PER")]
                rels.append(Relation(head="腾讯", predicate="总部位于", tail="深圳"))
            if "杭州" in t:
                ents.append(Entity(name="杭州", type="LOC"))
            logical = ER(entities=ents, relations=rels)
            local, report = backfill_provenance(logical, t)
            return ExtractionOutput(
                document_id=document.id, domain_code="general",
                result=local, attempts=1,
                backfill=report.to_dict())

    # 直接复用流水线的归并/换算，但喂入自定义块与 fake extractor
    fake = _ChunkFake()
    pipe = ChunkedExtractionPipeline(fake, concurrency=1)

    async def fake_run(document, chunks, on_chunk=None):
        from moray.extraction.chunk_metrics import DocumentMetrics
        from moray.extraction.pipeline import naive_merge
        partials = []
        # 合并后 _extract_one_chunk 多带一个文档级度量参数（工单 01），
        # 这里自己建一个 metrics 收集器以保持本测试的最小假流水线。
        metrics = DocumentMetrics(document_id=document.id, n_chunks=len(chunks))
        for c in chunks:
            res, _u, _cid, _m = await pipe._extract_one_chunk(c, metrics)
            partials.append(res)
        return ExtractionOutput(
            document_id=document.id, domain_code="general",
            result=naive_merge(partials), attempts=len(chunks))

    out = await fake_run(Document(id="d", text=full), chunker_chunks)
    by_name = {e.name: e for e in out.result.entities}
    # 阿里巴巴出现在块0与块2 -> 合并后 2 个全局 mention，切片都正确
    ali = by_name["阿里巴巴"]
    assert len(ali.mentions) == 2
    for m in ali.mentions:
        assert full[m.span.start:m.span.end] == "阿里巴巴"
    # 关系证据是全局坐标，且对原文切片一致
    for r in out.result.relations:
        if r.evidence_span is not None:
            assert full[r.evidence_span.start:r.evidence_span.end] == r.evidence_span.text


async def test_switch_off_does_not_backfill_model_fields_preserved():
    """开关关：模型没给 mentions/confidence 时内核保持默认，不做任何回填。"""
    out = await _run(False)
    # 关闭路径完全不触碰回填，backfill 信号为 None，mentions 为空、置信度为模型值(缺省0)
    assert out.backfill is None
    assert all(e.mentions == [] for e in out.result.entities)
    assert all(r.evidence_span is None for r in out.result.relations)
