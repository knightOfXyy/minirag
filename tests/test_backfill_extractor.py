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
    # 瘦身模型不输出 confidence：内核保持默认 0.0，回填不得用 grounding 覆盖
    assert all(e.confidence == 0.0 for e in res.entities)

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

    # 走真实 pipeline.run（不再手工绕过它）：接线/归并/信号汇总一起验证。
    fake = _ChunkFake()
    pipe = ChunkedExtractionPipeline(fake, concurrency=1)
    out = await pipe.run(Document(id="d", text=full), chunker_chunks)

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
    # 每块的回填信号被汇总而不是丢掉
    assert isinstance(out.backfill, dict)
    assert out.backfill["entities_total"] == 8   # 3 + 4 + 1
    assert out.backfill["entities_grounded"] == 8
    assert out.backfill["relations_with_evidence"] == 2


async def test_switch_off_does_not_backfill_model_fields_preserved():
    """开关关：模型没给 mentions/confidence 时内核保持默认，不做任何回填。"""
    out = await _run(False)
    # 关闭路径完全不触碰回填，backfill 信号为 None，mentions 为空、置信度为模型值(缺省0)
    assert out.backfill is None
    assert all(e.mentions == [] for e in out.result.entities)
    assert all(r.evidence_span is None for r in out.result.relations)


class _ChunkBackfillFake(_FakeExtractor):
    """每块只产出逻辑实体/关系，并用真实回填产出块内偏移与 backfill 信号。"""

    def __init__(self):
        super().__init__([], local_backfill=True)

    async def extract(self, document):
        from moray.schema import ExtractionResult as ER
        from moray.extraction.local_backfill import backfill_provenance

        t = document.text
        ents, rels = [], []
        for name, etype in (("阿里巴巴", "ORG"), ("马云", "PER"), ("杭州", "LOC"),
                            ("腾讯", "ORG"), ("深圳", "LOC")):
            if name in t:
                ents.append(Entity(name=name, type=etype))
        if "马云" in t and "阿里巴巴" in t:
            rels.append(Relation(head="马云", predicate="创立", tail="阿里巴巴"))
        if "腾讯" in t and "深圳" in t:
            rels.append(Relation(head="腾讯", predicate="总部位于", tail="深圳"))
        local, report = backfill_provenance(ER(entities=ents, relations=rels), t)
        return ExtractionOutput(
            document_id=document.id, domain_code="general",
            result=local, attempts=1, backfill=report.to_dict())


async def test_pipeline_run_aggregates_backfill_signal_with_global_spans():
    """信号必须经 pipeline.run 汇总，而不是在测试里手工绕过 pipeline 自证。"""
    from moray.extraction import Chunk, ChunkedExtractionPipeline

    full = "阿里巴巴由马云在杭州创立。腾讯总部位于深圳。"
    s1 = full.index("。") + 1
    chunks = [
        Chunk(id="d#c0", text=full[:s1], start=0, end=s1, index=0),
        Chunk(id="d#c1", text=full[s1:], start=s1, end=len(full), index=1),
    ]
    pipe = ChunkedExtractionPipeline(_ChunkBackfillFake(), concurrency=2)
    out = await pipe.run(Document(id="d", text=full), chunks)

    # 回填信号没有被 pipeline 丢掉：计数按块相加
    assert isinstance(out.backfill, dict)
    assert out.backfill["entities_total"] == 5
    assert out.backfill["entities_grounded"] == 5
    assert out.backfill["block_confidence"] == 1.0
    assert out.backfill["relations_with_evidence"] == 2
    # 全局 span 仍然正确（块内回填 -> remap）
    for e in out.result.entities:
        for m in e.mentions:
            assert full[m.span.start:m.span.end] == m.surface
    for r in out.result.relations:
        assert full[r.evidence_span.start:r.evidence_span.end] == r.evidence_span.text



async def test_provider_passes_backfill_sink(monkeypatch):
    """DynamicExtractorProvider 不能吞掉 output.backfill。"""
    from moray.extraction import provider as provider_mod
    from moray.extraction.document import Usage
    from moray.extraction.local_backfill import backfill_provenance
    from moray.extraction.provider import DynamicExtractorProvider
    from moray.schema import Entity, GENERAL_DOMAIN, ExtractionResult as ER

    class _FakeExt:
        def __init__(self, domain, **kwargs):
            self.domain = domain
            self.model_name = "fake"

        async def extract(self, document):
            local, report = backfill_provenance(
                ER(entities=[Entity(name="华为", type="ORG")]), document.text)
            return ExtractionOutput(
                document_id=document.id, domain_code=self.domain.code,
                result=local, model_name="fake", usage=Usage(), attempts=1,
                backfill=report.to_dict())

    class _Resolver:
        async def resolve_with_version(self, code):
            return GENERAL_DOMAIN, 1

    monkeypatch.setattr(provider_mod, "Extractor", _FakeExt)
    provider = DynamicExtractorProvider(_Resolver(), max_retries=0)
    sink: dict = {}
    fn = await provider.extract("general", backfill_sink=sink)
    result = await fn("华为在深圳。")
    assert result.entities[0].name == "华为"
    assert sink["backfill"]["entities_grounded"] == 1


class _SinkProvider:
    """把回填信号按真实 provider 契约写进 backfill_sink。"""

    async def extract(self, domain_code, usage_sink=None, backfill_sink=None):
        from moray.extraction.document import Usage
        from moray.extraction.local_backfill import backfill_provenance
        from moray.schema import ExtractionResult as ER

        async def fn(text, feedback=None):
            local, report = backfill_provenance(
                ER(entities=[Entity(name="华为", type="ORG")]), text)
            if usage_sink is not None:
                usage_sink.add(Usage(input_tokens=1, output_tokens=1))
            if backfill_sink is not None:
                backfill_sink["backfill"] = report.to_dict()
            return local

        return fn

    async def get_extractor(self, domain_code):
        raise AssertionError("short text should not reach chunked pipeline")


async def test_job_handler_result_and_output_carry_backfill(tmp_path):
    """批处理作业主链：ExtractionOutput 与作业结果都要带回填信号。"""
    from types import SimpleNamespace

    from moray.jobs.handlers import build_document_handler
    from moray.jobs.jobstore import JobRecord

    class _Ingestor:
        def __init__(self):
            self.seen = None

        async def ingest(self, output):
            self.seen = output
            return SimpleNamespace(entities_written=1, merged=0, queued=0,
                                   created=1, relations_written=0)

    f = tmp_path / "d.txt"
    f.write_text("华为在深圳。", encoding="utf-8")
    ing = _Ingestor()
    handler = await build_document_handler(
        store=None, ingestor=ing, extractor_provider=_SinkProvider())
    job = JobRecord({
        "job_id": "j1", "document_id": "d1", "domain_code": "general",
        "document_ref": str(f), "status": "running", "attempts": 1,
    })
    result = await handler(job)

    assert ing.seen.backfill["entities_grounded"] == 1
    assert result["backfill"]["entities_grounded"] == 1


async def test_extract_api_response_carries_backfill():
    """HTTP /extract 主链：响应中的 backfill 不能被丢。"""
    from httpx import ASGITransport, AsyncClient

    from moray.api.app import AppDeps, create_app

    deps = AppDeps(
        pool=None, store=None, kb=None, ingestor=None,
        extractor_provider=_SinkProvider())
    app = create_app(deps)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.post("/extract", json={
            "text": "华为在深圳。", "ingest": False})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["entities"][0]["name"] == "华为"
    assert body["backfill"]["entities_grounded"] == 1
    assert body["backfill"]["block_confidence"] == 1.0
