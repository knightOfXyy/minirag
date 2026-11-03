"""工单 03 第 12 轮评审（r12）发现的回归用例。

只钉住本轮要修的、可离线验证的行为：

* S1（should）slim 归并不仅去重键 strip，落库值也规范化；入库解析到真实实体；
* S2（should）歧义去重用集合，20k+ 全歧义文本保持线性；
* N1（nit）重叠块的关系证据候选按位置去重并聚合 chunk_ids；
* N2（nit）进程内候选有高于 20 的软上限，结果缓存/块间传递不被撑大；
* N3（nit）真实 ``Extractor.extract`` 保留 30×30 全部候选（不是 fake 自证）；
* N4（nit）缓存函数不再接受 owner，多余参数直接 TypeError；
* N6（nit）``single_chunk_covers_document`` 校验单块覆盖全文，而非只看块数；
* N7（nit）``reconciled_unattributed`` 进入看板字段；
* N8（nit）序列化边界深拷贝嵌套容器；
* N9（nit）``MergeMode.SLIM`` 一个开关表达四个归并口径；
* N10（nit）候选上限只保留 None=全量 / 0=空；负数在序列化边界 clamp + warning
  （r13 N6 从抛错改为降级）。
"""

from types import SimpleNamespace

import pytest

from moray.extraction import Chunk, ChunkedExtractionPipeline, Document, Extractor
from moray.extraction import local_backfill as lb
from moray.extraction.document import ExtractionOutput, Usage
from moray.extraction.local_backfill import backfill_provenance
from moray.extraction.pipeline import naive_merge
from moray.schema import Entity, ExtractionResult, Relation

_SLIM_KWARGS = dict(
    merge_relation_duplicates=True,
    fill_description=True,
    strip_entity_name=True,
    strip_relation_endpoints=True,
)


# ---------------------------------------------------------------------------
# S1：slim 归并的存值也去空白，入库解析到真实实体、不建 MISC 占位
# ---------------------------------------------------------------------------
class _FakeEmbedder:
    def embed_one(self, text):
        return [0.0]


class _RecordingStore:
    """最小的入库桩：只按精确名记录实体，并记录关系端点 id。"""

    def __init__(self):
        self.entities = {}
        self.relations = []
        self.evidence = []

    async def upsert_entity(self, *, name, type, **_kwargs):
        rec = self.entities.get(name)
        if rec is None:
            rec = {"id": len(self.entities) + 1, "name": name, "type": type}
            self.entities[name] = rec
        return rec["id"]

    async def get_entity_id(self, name, *, domain_code=None):
        rec = self.entities.get(name)
        return rec["id"] if rec is not None else None

    async def upsert_relation(self, **kwargs):
        self.relations.append(kwargs)
        return len(self.relations)

    async def add_relation_evidence(self, rid, **kwargs):
        self.evidence.append((rid, kwargs))


async def test_slim_merge_normalizes_stored_values_and_ingest_resolves_entity():
    from moray.kb.ingestor import Ingestor

    p0 = ExtractionResult(
        entities=[
            Entity(name=" 腾讯", type="ORG"),
            Entity(name="混元", type="PRODUCT"),
        ],
        relations=[Relation(head="腾讯 ", predicate="发布", tail="混元 ")],
    )
    p1 = ExtractionResult(
        entities=[
            Entity(name="腾讯", type="ORG"),
            Entity(name="混元", type="PRODUCT"),
        ],
        relations=[Relation(head="腾讯", predicate="发布", tail="混元")],
    )

    merged = naive_merge([p0, p1], **_SLIM_KWARGS)
    # 去重键与存值必须同口径：不能只合并键、把带空白的原串写回去。
    assert [e.name for e in merged.entities] == ["腾讯", "混元"]
    assert len(merged.relations) == 1
    assert (merged.relations[0].head, merged.relations[0].tail) == ("腾讯", "混元")

    store = _RecordingStore()
    report = await Ingestor(store, _FakeEmbedder()).ingest(
        ExtractionOutput(
            document_id="d", domain_code="general", result=merged))

    # 只有干净的实体名；没有带空白的 MISC 占位。
    assert set(store.entities) == {"腾讯", "混元"}
    assert store.entities["腾讯"]["type"] == "ORG"
    assert store.entities["混元"]["type"] == "PRODUCT"
    assert report.relations_skipped == []
    assert len(store.relations) == 1
    assert store.relations[0]["source_id"] == store.entities["腾讯"]["id"]
    assert store.relations[0]["target_id"] == store.entities["混元"]["id"]


# ---------------------------------------------------------------------------
# S2：歧义去重集合化——16000 次全歧义命中仍应在 1.5s 内
# ---------------------------------------------------------------------------
def test_ambiguity_dedup_is_linear_on_16k_all_ambiguous_hits():
    import time

    text = "苹果" * 16000
    result = ExtractionResult(entities=[
        Entity(name="苹果", type="ORG"),
        Entity(name="苹果", type="MISC"),
    ])
    t0 = time.perf_counter()
    _filled, report = backfill_provenance(result, text)
    elapsed = time.perf_counter() - t0

    assert len(report.ambiguous_mentions) == 16000
    assert elapsed < 1.5, elapsed


# ---------------------------------------------------------------------------
# N1：重叠块的关系证据候选不能按块重复
# ---------------------------------------------------------------------------
class _OverlapEvidenceFake:
    domain = SimpleNamespace(code="general")
    model_name = "fake-r12"
    local_backfill = True

    async def extract(self, document):
        logical = ExtractionResult(
            entities=[
                Entity(name="华为", type="ORG"),
                Entity(name="深圳", type="LOC"),
            ],
            relations=[Relation(head="华为", predicate="位于", tail="深圳")],
        )
        result, report = backfill_provenance(logical, document.text)
        return ExtractionOutput(
            document_id=document.id, domain_code="general",
            result=result, model_name=self.model_name, usage=Usage(),
            attempts=1,
            backfill=report.to_dict(max_relation_evidence_candidates=None),
        )


async def test_overlap_relation_candidates_are_deduped_with_source_chunks():
    body = "华为在深圳发布。"
    chunks = [
        Chunk(id=f"d#c{i}", text=body, start=0, end=len(body), index=i)
        for i in range(2)
    ]
    out = await ChunkedExtractionPipeline(
        _OverlapEvidenceFake(), concurrency=1).run(
            Document(id="d", text=body), chunks)

    key = next(iter(out.backfill["relation_evidence_candidates"]))
    cands = out.backfill["relation_evidence_candidates"][key]
    assert len(cands) == 1
    assert set(cands[0]["chunk_ids"]) == {"d#c0", "d#c1"}
    assert cands[0]["selected"] is True


# ---------------------------------------------------------------------------
# N2 / N3：真实 Extractor 路径的候选数量与内部软上限
# ---------------------------------------------------------------------------
class _RealExtractorStub(Extractor):
    """走真实 ``Extractor.extract`` 的固定模型桩（不联网）。"""

    def __init__(self, reply, *, local_backfill=True):
        super().__init__(
            "general", max_retries=0, local_backfill=local_backfill)
        self._reply = reply
        self._calls = 0

    def _build_agent(self):
        return SimpleNamespace()

    async def _call_structured(self, agent, content):
        self._calls += 1
        return SimpleNamespace(
            structured_output=self._reply,
            usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        )


def _dense_reply() -> dict:
    return {
        "entities": [
            {"name": "华为", "type": "ORG"},
            {"name": "深圳", "type": "LOC"},
        ],
        "relations": [
            {"head": "华为", "predicate": "位于", "tail": "深圳"},
        ],
    }


async def test_real_extractor_keeps_all_30x30_candidates():
    """把 extractor.py 的 ``to_dict(None)`` 改回 ``to_dict()`` 时本用例必须红。"""
    text = "华为深圳" * 30
    out = await _RealExtractorStub(_dense_reply()).extract(
        Document(id="d", text=text))

    key = next(iter(out.backfill["relation_evidence_candidates"]))
    cands = out.backfill["relation_evidence_candidates"][key]
    assert len(cands) == 30 * 30


async def test_real_extractor_caps_internal_candidates_above_soft_limit():
    """50×50=2500 条笛卡尔积：进程内软上限 2000 先兜住，不撑大结果缓存。"""
    text = "华为深圳" * 50
    out = await _RealExtractorStub(_dense_reply()).extract(
        Document(id="d", text=text))

    key = next(iter(out.backfill["relation_evidence_candidates"]))
    cands = out.backfill["relation_evidence_candidates"][key]
    assert len(cands) == lb.MAX_INTERNAL_RELATION_EVIDENCE_CANDIDATES
    assert cands[0]["selected"] is True


class _DenseChunkFake(_RealExtractorStub):
    """分块桩：块内先产出全量候选（模拟修复后的 Extractor）。"""

    domain = SimpleNamespace(code="general")
    model_name = "fake-r12"

    def __init__(self, repeats):
        super().__init__(_dense_reply())
        self.repeats = repeats

    async def extract(self, document):
        text = document.text
        logical = ExtractionResult(
            entities=[
                Entity(name="华为", type="ORG"),
                Entity(name="深圳", type="LOC"),
            ],
            relations=[Relation(head="华为", predicate="位于", tail="深圳")],
        )
        result, report = backfill_provenance(logical, text)
        return ExtractionOutput(
            document_id=document.id, domain_code="general",
            result=result, model_name=self.model_name, usage=Usage(),
            attempts=1,
            backfill=report.to_dict(max_relation_evidence_candidates=None),
        )


async def test_pipeline_caps_merged_candidates_at_internal_soft_limit():
    text = "华为深圳" * 50
    chunks = [Chunk(
        id="d#c0", text=text, start=0, end=len(text), index=0)]
    out = await ChunkedExtractionPipeline(
        _DenseChunkFake(50), concurrency=1).run(
            Document(id="d", text=text), chunks)

    key = next(iter(out.backfill["relation_evidence_candidates"]))
    cands = out.backfill["relation_evidence_candidates"][key]
    assert len(cands) == lb.MAX_INTERNAL_RELATION_EVIDENCE_CANDIDATES
    assert len([c for c in cands if c.get("selected")]) == 1


# ---------------------------------------------------------------------------
# N4：缓存函数不再接受 owner（旧 API 会静默挂私有属性）
# ---------------------------------------------------------------------------
def test_cached_backfill_rejects_legacy_owner_argument():
    cache: dict = {}
    result = ExtractionResult(entities=[Entity(name="华为", type="ORG")])
    first = lb.cached_backfill_provenance(cache, result, "华为在深圳。")
    second = lb.cached_backfill_provenance(cache, result, "华为在深圳。")
    assert second[0] is first[0]
    assert second[1] is first[1]

    with pytest.raises(TypeError):
        lb.cached_backfill_provenance(
            cache, result, "华为在深圳。", SimpleNamespace())


# ---------------------------------------------------------------------------
# N6：单块必须真的覆盖全文才跳过整篇别名补扫
# ---------------------------------------------------------------------------
class _AliasOutsideChunkFake:
    domain = SimpleNamespace(code="general")
    model_name = "fake-r12"
    local_backfill = True

    async def extract(self, document):
        logical = ExtractionResult(entities=[
            Entity(name="华为", type="ORG", aliases=["深圳"]),
        ])
        result, report = backfill_provenance(logical, document.text)
        return ExtractionOutput(
            document_id=document.id, domain_code="general",
            result=result, model_name=self.model_name, usage=Usage(),
            attempts=1,
            backfill=report.to_dict(max_relation_evidence_candidates=None),
        )


async def test_partial_single_chunk_still_rescans_document_for_aliases():
    full = "华为在这里。深圳也有。"
    prefix = "华为在这里。"
    chunks = [Chunk(
        id="d#c0", text=prefix, start=0, end=len(prefix), index=0)]
    out = await ChunkedExtractionPipeline(
        _AliasOutsideChunkFake(), concurrency=1).run(
            Document(id="d", text=full), chunks)

    # 单块只是整篇的前缀：块外真实存在的别名不能被判成幻觉。
    assert out.backfill["unmatched_aliases"] == []


# ---------------------------------------------------------------------------
# N7：撤销归属数进入看板字段
# ---------------------------------------------------------------------------
def test_recorder_exposes_reconciled_unattributed():
    from moray.observability import RunRecorder

    fields = RunRecorder._backfill_fields({
        "reconciled_unattributed": 2,
        "ambiguous_mentions": [],
    })
    assert fields["reconciled_unattributed"] == 2

    recorder = RunRecorder.from_pipeline(
        document_id="d",
        reviewed=SimpleNamespace(
            accepted=True, result=ExtractionResult(),
            review=SimpleNamespace(iterations=1, span_ratio=1.0, issues=[])),
        ingest_report=SimpleNamespace(
            merged=[], queued=[], created=[]),
        backfill={"reconciled_unattributed": 2, "ambiguous_mentions": []})
    assert recorder.reconciled_unattributed == 2
    # 未传 backfill（开关关闭）仍是 0，不伪造信号。
    assert RunRecorder._backfill_fields(None) == {}


# ---------------------------------------------------------------------------
# N8：序列化边界深拷贝全部嵌套容器
# ---------------------------------------------------------------------------
def test_serialize_backfill_deep_copies_every_nested_container():
    original = {
        "block_confidence": 1.0,
        "matched_literals": {"k": 1},
        "seen_literals": ["华为"],
        "unmatched_aliases": [{"type": "ORG", "literal": "阿里"}],
        "ambiguous_mentions": [{
            "surface": "苹果", "start": 0, "end": 2,
            "candidates": ["a"], "chunk_ids": ["c0"],
        }],
        "relations_without_evidence": [
            {"head": "A", "tail": "B", "chunk_ids": ["c0"]}],
        "relation_evidence_candidates": {
            "k": [{"head_start": 0, "chunk_ids": ["c0"]}]},
    }
    out = lb.serialize_backfill_for_output(original)

    out["matched_literals"]["k"] = 99
    out["seen_literals"].append("x")
    out["unmatched_aliases"][0]["literal"] = "x"
    out["ambiguous_mentions"][0]["candidates"].append("b")
    out["ambiguous_mentions"][0]["chunk_ids"].append("c1")
    out["relations_without_evidence"][0]["head"] = "X"
    out["relations_without_evidence"][0]["chunk_ids"].append("c1")
    out["relation_evidence_candidates"]["k"][0]["head_start"] = 99
    out["relation_evidence_candidates"]["k"][0]["chunk_ids"].append("c1")

    assert original["matched_literals"] == {"k": 1}
    assert original["seen_literals"] == ["华为"]
    assert original["unmatched_aliases"][0]["literal"] == "阿里"
    assert original["ambiguous_mentions"][0]["candidates"] == ["a"]
    assert original["ambiguous_mentions"][0]["chunk_ids"] == ["c0"]
    assert original["relations_without_evidence"][0]["head"] == "A"
    assert original["relations_without_evidence"][0]["chunk_ids"] == ["c0"]
    assert original["relation_evidence_candidates"]["k"][0]["head_start"] == 0
    assert original["relation_evidence_candidates"]["k"][0]["chunk_ids"] == ["c0"]


# ---------------------------------------------------------------------------
# N9：MergeMode.SLIM 一个开关替代四个布尔
# ---------------------------------------------------------------------------
def test_merge_mode_slim_is_a_single_switch_for_all_four_behaviors():
    from moray.extraction import MergeMode

    p0 = ExtractionResult(
        entities=[Entity(name=" 腾讯", type="ORG")],
        relations=[Relation(head="腾讯 ", predicate="发布", tail="混元 ")],
    )
    p1 = ExtractionResult(
        entities=[Entity(name="腾讯", type="ORG", description="腾讯集团")],
        relations=[Relation(head="腾讯", predicate="发布", tail="混元")],
    )

    slim = naive_merge([p0, p1], mode=MergeMode.SLIM)
    assert [e.name for e in slim.entities] == ["腾讯"]
    assert slim.entities[0].description == "腾讯集团"
    assert len(slim.relations) == 1
    assert (slim.relations[0].head, slim.relations[0].tail) == ("腾讯", "混元")

    # 显式关键字仍可覆盖模式开关（兼容 shim）。
    no_fill = naive_merge(
        [p0, p1], mode=MergeMode.SLIM, fill_description=False)
    assert no_fill.entities[0].description == ""

    # 默认仍是 262319b 的 LEGACY：精确键、不合并重复关系。
    legacy = naive_merge([p0, p1])
    assert len(legacy.entities) == 2
    assert len(legacy.relations) == 2


# ---------------------------------------------------------------------------
# N10 / r13 N6：候选上限 None=全量 / 0=空；负数 clamp + warning
# ---------------------------------------------------------------------------
def test_relation_candidate_cap_clamps_negative_limit():
    result = ExtractionResult(
        entities=[
            Entity(name="华为", type="ORG"),
            Entity(name="深圳", type="LOC"),
        ],
        relations=[Relation(head="华为", predicate="位于", tail="深圳")],
    )
    _filled, report = backfill_provenance(result, "华为深圳")
    key = next(iter(report.relation_evidence_candidates))

    assert report.to_dict(
        max_relation_evidence_candidates=0
    )["relation_evidence_candidates"][key] == []
    # r13 N6：负数在序列化边界 clamp 到默认窗口 + warning，不抛错。
    all_cands = report.to_dict(
        max_relation_evidence_candidates=None
    )["relation_evidence_candidates"][key]
    clamped = report.to_dict(
        max_relation_evidence_candidates=-5
    )["relation_evidence_candidates"][key]
    assert len(clamped) == min(
        len(all_cands), lb.MAX_SERIALIZED_RELATION_EVIDENCE_CANDIDATES)
    assert len(lb.serialize_backfill_for_output(
        report.to_dict(), max_relation_evidence_candidates=-1
    )["relation_evidence_candidates"][key]) == (
        min(len(all_cands), lb.MAX_SERIALIZED_RELATION_EVIDENCE_CANDIDATES))
