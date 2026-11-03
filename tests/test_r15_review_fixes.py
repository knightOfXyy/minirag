"""工单 03 第 15 轮评审（r15）发现的回归用例。

只钉住本轮可离线验证的行为：

* F1（should）跨块重叠时，块内已判歧义、其他块只抽到一个实体，mention
  不得再写给该实体；歧义 candidates 必须求并集而不是拆成两条；
* F3（nit）跨块合并后 ``relations_without_evidence`` 不得保留已有证据的
  同一三元组；
* F4（nit）跨块候选窗口按全局距离排序（selected 固定首条）；
* F8（nit）API/作业共用的回填取值与 ``Document.audit_text`` 单一入口。
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
    resolve_backfill,
)
from moray.schema import Entity, ExtractionResult, Relation


# ---------------------------------------------------------------------------
# F1：块 0 判歧义，块 1/2 只抽到其中一个实体
# ---------------------------------------------------------------------------
class _OverlapSingleOwnerFake:
    """三块完全重叠：块 0 同时抽到 ORG+MISC，块 1/2 只抽到 ORG。"""

    domain = SimpleNamespace(code="general")
    model_name = "fake-r15"
    local_backfill = True

    async def extract(self, document):
        if document.id.endswith("c0"):
            entities = [
                Entity(name="苹果", type="ORG"),
                Entity(name="苹果", type="MISC"),
            ]
        else:
            entities = [Entity(name="苹果", type="ORG")]
        result, report = backfill_provenance(
            ExtractionResult(entities=entities), document.text)
        return ExtractionOutput(
            document_id=document.id, domain_code="general",
            result=result, model_name=self.model_name, usage=Usage(),
            attempts=1,
            backfill=report.to_dict(max_relation_evidence_candidates=None),
        )


async def _run_overlap_single_owner():
    body = "苹果发布了新品。"
    chunks = [
        Chunk(id=f"d#c{i}", text=body, start=0, end=len(body), index=i)
        for i in range(3)
    ]
    return await ChunkedExtractionPipeline(
        _OverlapSingleOwnerFake(), concurrency=1).run(
            Document(id="d", text=body), chunks)


@pytest.mark.asyncio
async def test_overlap_block_ambiguity_blocks_later_single_owner_mention():
    """块内已判歧义时，后续块只抽到一个实体也不能写 mention（r15 F1）。"""
    out = await _run_overlap_single_owner()

    owners = [e for e in out.result.entities if e.name == "苹果"]
    assert {e.type for e in owners} == {"ORG", "MISC"}
    assert all(e.mentions == [] for e in owners), (
        "歧义 span 不得归属任何实体，否则结果与 ambiguous_mentions 自相矛盾")

    ambiguous = out.backfill["ambiguous_mentions"]
    assert len(ambiguous) == 1, "同一处原文只能有一条歧义记录"
    entry = ambiguous[0]
    assert (entry["surface"], entry["start"], entry["end"]) == (
        "苹果", 0, 2)
    assert set(entry["candidates"]) == {
        '["ORG", "苹果"]', '["MISC", "苹果"]'}, (
        "后续块只看到 ORG 时，也不能把 MISC 从歧义候选并集里抹掉")
    # 归并会先把同一实体跨块重复的同一 span 收敛成一条，因此这里只要求
    # 确有 mention 被撤销；具体条数是 chunk-weighted/去重后的实现细节。
    assert out.backfill["reconciled_unattributed"] >= 1


# ---------------------------------------------------------------------------
# F3：有证据的关系不得同时留在 relations_without_evidence
# ---------------------------------------------------------------------------
def _report(chunk_text: str, chunk_id: str, chunk_start: int,
            *, entities, relations) -> BackfillReport:
    _filled, report = backfill_provenance(
        ExtractionResult(entities=entities, relations=relations), chunk_text)
    return BackfillReport.from_dict(
        report.to_dict(max_relation_evidence_candidates=None),
        chunk_start=chunk_start, chunk_id=chunk_id)


def test_merge_drops_missing_relation_when_overlapping_block_has_evidence():
    """块 0 同句有证据、块 1 只命中一个端点：不能同时报「无证据」。"""
    text0 = "张三创立腾飞公司。"
    text1 = "张三到了会场。"
    relations = [Relation(head="张三", predicate="创立", tail="腾飞公司")]
    r0 = _report(
        text0, "d#c0", 0,
        entities=[Entity(name="张三", type="PER"),
                  Entity(name="腾飞公司", type="ORG")],
        relations=relations)
    r1 = _report(
        text1, "d#c1", len(text0),
        entities=[Entity(name="张三", type="PER"),
                  Entity(name="腾飞公司", type="ORG")],
        relations=relations)
    assert r0.relation_evidence_candidates
    assert r1.relations_without_evidence

    merged = merge_backfill_reports([r0, r1])

    assert merged.relation_evidence_candidates
    assert merged.relations_without_evidence == [], (
        "同一三元组已有证据时，文档级「无证据」信号必须对账删除")


def test_merge_keeps_missing_relation_without_any_evidence():
    """对账只删「其实有证据」的项，不能把真正无证据的关系一起吞掉。"""
    text = "张三到了会场。"
    relations = [Relation(head="张三", predicate="创立", tail="腾飞公司")]
    r0 = _report(
        text, "d#c0", 0,
        entities=[Entity(name="张三", type="PER"),
                  Entity(name="腾飞公司", type="ORG")],
        relations=relations)

    merged = merge_backfill_reports([r0])

    assert merged.relation_evidence_candidates == {}
    assert [e["predicate"] for e in merged.relations_without_evidence] == [
        "创立"]


# ---------------------------------------------------------------------------
# F4：跨块候选按全局距离排序，selected 固定首条
# ---------------------------------------------------------------------------
def _candidate(distance: int, start: int, *, selected: bool = False) -> dict:
    return {
        "head_start": start,
        "tail_start": start + distance,
        "sent_start": 0,
        "sent_end": 100,
        "evidence_start": 0,
        "evidence_end": 100,
        "distance": distance,
        "selected": selected,
    }


def test_merged_candidates_are_globally_distance_sorted():
    """后一个块更近的候选不能被「块顺序」挤到后面（r15 F4）。"""
    key = '["张三", "创立", "腾飞公司"]'
    r0 = BackfillReport(
        chunk_id="d#c0", chunk_start=0,
        relation_evidence_candidates={
            key: [_candidate(50, 500, selected=True), _candidate(60, 600)]})
    r1 = BackfillReport(
        chunk_id="d#c1", chunk_start=1000,
        relation_evidence_candidates={
            key: [_candidate(1, 1000), _candidate(2, 2000)]})

    merged = merge_backfill_reports([r0, r1])
    cands = merged.relation_evidence_candidates[key]

    # selected（块 0 真正用于 evidence_span 的对）固定首条。
    assert cands[0]["selected"] is True
    assert cands[0]["distance"] == 50
    # 其余按全局距离升序，而不是「块 0 全部在前、块 1 在后」。
    assert [c["distance"] for c in cands[1:]] == [1, 2, 60]
    # 裁剪时 selected + 最近的候选不被块顺序截掉。
    capped = merged.to_dict(max_relation_evidence_candidates=2)
    kept = capped["relation_evidence_candidates"][key]
    assert [c["distance"] for c in kept] == [50, 1]
    assert kept[0]["selected"] is True


# ---------------------------------------------------------------------------
# F8：回填取值与审计原文的统一入口
# ---------------------------------------------------------------------------
def test_document_audit_text_preserves_empty_source_text():
    """空字符串是合法空正文，不能回退到带反馈的 text。"""
    assert Document(id="d", text="正文+反馈", source_text="").audit_text == ""
    assert Document(id="d", text="正文").audit_text == "正文"


def test_resolve_backfill_prefers_reviewed_then_sink_and_caps():
    key = '["张三", "创立", "腾飞公司"]'
    internal = {
        "relation_evidence_candidates": {
            key: [_candidate(i, i) for i in range(30)]}}
    reviewed = SimpleNamespace(backfill=internal)

    from_reviewed = resolve_backfill(reviewed, {"backfill": None})
    assert len(from_reviewed["relation_evidence_candidates"][key]) == (
        lb.MAX_SERIALIZED_RELATION_EVIDENCE_CANDIDATES)

    from_sink = resolve_backfill(SimpleNamespace(backfill=None),
                                 {"backfill": internal})
    assert len(from_sink["relation_evidence_candidates"][key]) == (
        lb.MAX_SERIALIZED_RELATION_EVIDENCE_CANDIDATES)

    assert resolve_backfill(SimpleNamespace(backfill=None), {}) is None
