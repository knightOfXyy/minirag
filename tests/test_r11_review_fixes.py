"""工单 03 第 11 轮评审（r11）发现的回归用例。

只钉住本轮要修的、可离线验证的行为：

* F1（should）分块路径的块间传递不得截断关系证据候选；只在 API/作业
  落盘边界按默认上限裁剪；
* N1（nit）跨块 span 歧义条目要与 merge 的歧义索引共用去重，并带来源块；
* N2（nit）撤销归属后要留下确定性的 ``reconciled_unattributed`` 计数；
* N3（nit）``to_dict()`` 序列化出的候选不得与内存报告共享引用；
* N4（nit）``cached_backfill_provenance`` 收显式 cache，不再往 owner 上写；
* N5（nit）候选上限只认 ``None`` 为全量；``<=0`` 返回空，无死分支；
* N8（nit）单块文档不再对别名做第二次整篇扫描；
* N10（nit）同一实体在重叠块的同 span 重复 mention 被收敛为一条；
* N11（nit）slim 归并的关系去重键也忽略端点首尾空白，关闭态保持原样。
"""

from types import SimpleNamespace

import pytest

from moray.extraction import Chunk, ChunkedExtractionPipeline, Document
from moray.extraction import local_backfill as lb
from moray.extraction.document import ExtractionOutput, Usage
from moray.extraction.local_backfill import (
    BackfillReport,
    backfill_provenance,
    merge_backfill_reports,
)
from moray.extraction.pipeline import naive_merge
from moray.schema import Entity, ExtractionResult, Relation

# ---------------------------------------------------------------------------
# 公用：同句 30×30 共现 -> 900 条关系证据候选
# ---------------------------------------------------------------------------
_DENSE_TEXT = "华为深圳" * 30


def _dense_logical() -> ExtractionResult:
    return ExtractionResult(
        entities=[
            Entity(name="华为", type="ORG"),
            Entity(name="深圳", type="LOC"),
        ],
        relations=[Relation(head="华为", predicate="位于", tail="深圳")],
    )


def _dense_report() -> BackfillReport:
    _result, report = backfill_provenance(_dense_logical(), _DENSE_TEXT)
    return report


class _DenseChunkFake:
    """模拟真实 Extractor 的块产物：块间传递用全量候选（r11 F1）。"""

    domain = SimpleNamespace(code="general")
    model_name = "fake-r11"
    local_backfill = True

    async def extract(self, document):
        logical = _dense_logical()
        result, report = backfill_provenance(logical, document.text)
        return ExtractionOutput(
            document_id=document.id, domain_code="general",
            result=result, model_name=self.model_name, usage=Usage(),
            attempts=1,
            # 真实 Extractor 修复后也是全量传给上层，序列化边界另行裁剪。
            backfill=report.to_dict(max_relation_evidence_candidates=None),
        )


async def _run_dense_pipeline():
    chunks = [Chunk(
        id="d#c0", text=_DENSE_TEXT, start=0, end=len(_DENSE_TEXT), index=0)]
    return await ChunkedExtractionPipeline(
        _DenseChunkFake(), concurrency=1).run(
            Document(id="d", text=_DENSE_TEXT), chunks)


# ---------------------------------------------------------------------------
# F1：块间传递全量候选，只在对外序列化边界裁剪
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_chunked_pipeline_preserves_all_relation_evidence_candidates():
    out = await _run_dense_pipeline()

    key = next(iter(out.backfill["relation_evidence_candidates"]))
    cands = out.backfill["relation_evidence_candidates"][key]
    assert len(cands) == 900
    assert len([c for c in cands if c.get("selected")]) == 1


def test_serialize_backfill_for_output_caps_candidates_and_keeps_selected():
    report = _dense_report()
    key = next(iter(report.relation_evidence_candidates))
    internal = report.to_dict(max_relation_evidence_candidates=None)
    assert len(internal["relation_evidence_candidates"][key]) == 900

    serialized = lb.serialize_backfill_for_output(internal)
    cands = serialized["relation_evidence_candidates"][key]
    assert len(cands) == lb.MAX_SERIALIZED_RELATION_EVIDENCE_CANDIDATES
    assert len([c for c in cands if c.get("selected")]) == 1
    # 边界函数不能反向改写到内部字典
    assert len(internal["relation_evidence_candidates"][key]) == 900


@pytest.mark.asyncio
async def test_extract_endpoint_caps_backfill_candidates(monkeypatch):
    """API 落盘边界：内部 900 条候选，响应体只出默认上限 20 条。"""
    from httpx import ASGITransport, AsyncClient

    from moray.api import app as api_app
    from moray.orchestration.specialists import (
        ExtractionReview,
        ReviewedExtraction,
    )

    report = _dense_report()
    internal = report.to_dict(max_relation_evidence_candidates=None)
    reviewed = ReviewedExtraction(
        result=ExtractionResult(),
        accepted=True,
        review=ExtractionReview(verdict="pass"),
        backfill=internal,
    )

    async def fake_chunked_team(*_args, **_kwargs):
        return reviewed

    async def dummy_extractor(_text, _feedback):
        return ExtractionResult()

    monkeypatch.setattr(api_app, "run_chunked_team", fake_chunked_team)

    deps = api_app.AppDeps(
        pool=None, store=SimpleNamespace(), kb=SimpleNamespace(),
        ingestor=SimpleNamespace(), extractor_fn=dummy_extractor)
    app = api_app.create_app(deps)
    async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t") as ac:
        resp = await ac.post(
            "/extract", json={"text": "华为深圳", "ingest": False})

    assert resp.status_code == 200, resp.text
    key = next(iter(internal["relation_evidence_candidates"]))
    cands = resp.json()["backfill"]["relation_evidence_candidates"][key]
    assert len(cands) == lb.MAX_SERIALIZED_RELATION_EVIDENCE_CANDIDATES
    assert len([c for c in cands if c.get("selected")]) == 1


# ---------------------------------------------------------------------------
# N1 / N2 / N10：跨块 span 歧义去重、来源块、撤销计数与同 owner 去重
# ---------------------------------------------------------------------------
class _ThreeOverlapFake:
    """三块完全重叠：块 0 判歧义，块 1 归 ORG，块 2 归 MISC。"""

    domain = SimpleNamespace(code="general")
    model_name = "fake-r11"
    local_backfill = True

    async def extract(self, document):
        if document.id.endswith("c0"):
            entities = [
                Entity(name="苹果", type="ORG"),
                Entity(name="苹果", type="MISC"),
            ]
        elif document.id.endswith("c1"):
            entities = [Entity(name="苹果", type="ORG")]
        else:
            entities = [Entity(name="苹果", type="MISC")]
        result, report = backfill_provenance(
            ExtractionResult(entities=entities), document.text)
        return ExtractionOutput(
            document_id=document.id, domain_code="general",
            result=result, model_name=self.model_name, usage=Usage(),
            attempts=1,
            backfill=report.to_dict(max_relation_evidence_candidates=None),
        )


async def _run_three_overlap():
    body = "苹果发布了新品。"
    chunks = [
        Chunk(id=f"d#c{i}", text=body, start=0, end=len(body), index=i)
        for i in range(3)
    ]
    return await ChunkedExtractionPipeline(
        _ThreeOverlapFake(), concurrency=1).run(
            Document(id="d", text=body), chunks)


@pytest.mark.asyncio
async def test_reconciled_ambiguity_is_deduped_and_carries_source_chunks():
    out = await _run_three_overlap()

    ambiguous = out.backfill["ambiguous_mentions"]
    assert len(ambiguous) == 1
    entry = ambiguous[0]
    assert (entry["surface"], entry["start"], entry["end"]) == ("苹果", 0, 2)
    assert set(entry["candidates"]) == {
        '["ORG", "苹果"]', '["MISC", "苹果"]'}
    # 三块都参与过这处原文：块 0 的块内歧义 + 块 1/2 的跨块归属冲突。
    assert set(entry["chunk_ids"]) == {"d#c0", "d#c1", "d#c2"}
    assert entry["chunk_id"] == "d#c0"

    # 看板只按条数计数，不能因为重叠被放大。
    from moray.observability import RunRecorder

    fields = RunRecorder._backfill_fields(out.backfill)
    assert fields["ambiguous_mentions"] == 1


@pytest.mark.asyncio
async def test_reconcile_counts_revoked_mentions_without_reweighting_blocks():
    out = await _run_three_overlap()

    # 块 1/2 各有一条 mention 被撤销；计数口径保持 chunk-weighted。
    assert out.backfill["reconciled_unattributed"] == 2
    # 块 0 的歧义不算命中，块 1/2 各命中 1 个实体；撤销归属不反算这个
    # 块加权计数（N2 只在信号里额外暴露被撤销的条数）。
    assert out.backfill["entities_grounded"] == 2
    assert out.backfill["entities_total"] == 4     # 2 + 1 + 1，仍按块相加


@pytest.mark.asyncio
async def test_overlap_same_entity_duplicate_span_is_collapsed_to_one_mention():
    class _SameEntityFake:
        domain = SimpleNamespace(code="general")
        model_name = "fake-r11"
        local_backfill = True

        async def extract(self, document):
            logical = ExtractionResult(
                entities=[Entity(name="苹果", type="ORG")])
            result, report = backfill_provenance(logical, document.text)
            return ExtractionOutput(
                document_id=document.id, domain_code="general",
                result=result, model_name=self.model_name, usage=Usage(),
                attempts=1,
                backfill=report.to_dict(max_relation_evidence_candidates=None),
            )

    body = "苹果发布了新品。"
    chunks = [
        Chunk(id="d#c0", text=body, start=0, end=len(body), index=0),
        Chunk(id="d#c1", text=body, start=0, end=len(body), index=1),
    ]
    out = await ChunkedExtractionPipeline(
        _SameEntityFake(), concurrency=1).run(
            Document(id="d", text=body), chunks)

    [entity] = out.result.entities
    assert [m.surface for m in entity.mentions] == ["苹果"]
    assert out.backfill["ambiguous_mentions"] == []


# ---------------------------------------------------------------------------
# N3：to_dict() 的候选是副本，不与内存报告共享引用
# ---------------------------------------------------------------------------
def test_serialized_candidates_are_copies_and_do_not_mutate_report():
    report = _dense_report()
    key = next(iter(report.relation_evidence_candidates))
    before = [dict(c) for c in report.relation_evidence_candidates[key]]

    serialized = report.to_dict(max_relation_evidence_candidates=None)
    serialized["relation_evidence_candidates"][key][0]["selected"] = "mutated"
    serialized["relation_evidence_candidates"][key][0]["distance"] = -1

    assert report.relation_evidence_candidates[key] == before

    # matched_literals / unmatched_aliases / seen_literals 也不共享可变容器
    logical = ExtractionResult(entities=[
        Entity(name="苹果", type="ORG", aliases=["Acme"]),
    ])
    _r, rep = backfill_provenance(logical, "苹果发布了新品。")
    data = rep.to_dict()
    data["matched_literals"].clear()
    data["unmatched_aliases"].append({"type": "X", "entity": "Y", "literal": "Z"})
    data["seen_literals"].clear()
    assert rep.matched_literals
    assert len(rep.unmatched_aliases) == 1
    assert rep.seen_literals == ["苹果"]


# ---------------------------------------------------------------------------
# N4：cached_backfill_provenance 收显式 cache，不碰 owner、不全量序列化
# ---------------------------------------------------------------------------
def test_cached_backfill_uses_explicit_cache_without_mutating_owner(monkeypatch):
    calls = {"n": 0}
    real = lb.backfill_provenance

    def counting(result, text):
        calls["n"] += 1
        return real(result, text)

    monkeypatch.setattr(lb, "backfill_provenance", counting)

    serializations = {"n": 0}
    real_dump = ExtractionResult.model_dump_json

    def counting_dump(self, *args, **kwargs):
        serializations["n"] += 1
        return real_dump(self, *args, **kwargs)

    monkeypatch.setattr(ExtractionResult, "model_dump_json", counting_dump)

    cache: dict = {}
    result = ExtractionResult(entities=[Entity(name="华为", type="ORG")])
    filled_a, report_a = lb.cached_backfill_provenance(
        cache, result, "华为在深圳。")
    filled_b, report_b = lb.cached_backfill_provenance(
        cache, result, "华为在深圳。")

    assert calls["n"] == 1
    assert report_b is report_a
    assert filled_b.entities[0].mentions
    # 缓存是唯一副作用：显式 dict 里恰好一份产物，且命中的是同一份对象。
    # 旧版这条位置是「owner 从未被传入却断言它没有属性」的永不失败断言
    # （r12 N4），已替换为可失败的真实行为检查。
    assert len(cache) == 1
    assert cache[next(iter(cache))][0] is filled_a
    assert serializations["n"] == 0


# ---------------------------------------------------------------------------
# N5：候选上限语义（只认 None 为全量；<=0 为空；无「替换末条」死分支）
# ---------------------------------------------------------------------------
def test_relation_candidate_cap_semantics():
    report = _dense_report()
    key = next(iter(report.relation_evidence_candidates))

    assert len(report.to_dict(
        max_relation_evidence_candidates=None
    )["relation_evidence_candidates"][key]) == 900
    assert report.to_dict(
        max_relation_evidence_candidates=0
    )["relation_evidence_candidates"][key] == []
    # r13 N6：负数不再抛错（坏配置会把整次响应打成 500），clamp 到默认窗口。
    clamped = report.to_dict(
        max_relation_evidence_candidates=-5
    )["relation_evidence_candidates"][key]
    assert len(clamped) == lb.MAX_SERIALIZED_RELATION_EVIDENCE_CANDIDATES

    # selected 始终在首条：裁剪前 5 条也必须包含它（没有替换末条的分支）。
    kept = report.to_dict(
        max_relation_evidence_candidates=5
    )["relation_evidence_candidates"][key]
    assert len(kept) == 5
    assert kept[0]["selected"] is True


# ---------------------------------------------------------------------------
# N8：单块文档不再做第二次整篇别名补扫
# ---------------------------------------------------------------------------
def test_single_chunk_merge_skips_document_alias_rescan(monkeypatch):
    logical = ExtractionResult(entities=[
        Entity(name="苹果", type="ORG", aliases=["Acme"]),
    ])
    _result, report = backfill_provenance(logical, "苹果发布了新品。")
    assert report.unmatched_aliases  # Acme 不在正文里，是待判候选

    constructions = {"n": 0}
    real_ac = lb.AhoCorasick

    class _SpyAC(real_ac):
        def __init__(self, patterns):
            constructions["n"] += 1
            super().__init__(patterns)

    monkeypatch.setattr(lb, "AhoCorasick", _SpyAC)

    merged = merge_backfill_reports(
        [BackfillReport.from_dict(
            report.to_dict(), chunk_start=0, chunk_id="d#c0")],
        document_text="苹果发布了新品。",
        single_chunk_covers_document=True)
    assert constructions["n"] == 0
    # 单块 seen_literals 已是全量：Acme 仍判幻觉。
    assert [u["literal"] for u in merged.unmatched_aliases] == ["Acme"]

    merge_backfill_reports(
        [BackfillReport.from_dict(
            report.to_dict(), chunk_start=0, chunk_id="d#c0")],
        document_text="苹果发布了新品。")
    assert constructions["n"] == 1


# ---------------------------------------------------------------------------
# N11：slim 归并的关系去重键忽略端点首尾空白；关闭态保持精确键
# ---------------------------------------------------------------------------
def test_slim_naive_merge_relation_key_ignores_whitespace_but_legacy_keeps_it():
    p0 = ExtractionResult(relations=[
        Relation(head="腾讯 ", predicate="发布", tail="混元")])
    p1 = ExtractionResult(relations=[
        Relation(head="腾讯", predicate="发布", tail="混元 ")])

    slim = naive_merge([p0, p1], strip_relation_endpoints=True)
    assert len(slim.relations) == 1

    legacy = naive_merge([p0, p1])
    assert len(legacy.relations) == 2
