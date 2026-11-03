"""工单 03 第 11 轮评审（r11）定点探针：确定性、无模型、无网络。

逐条复现本轮修复的关键行为（对应 ``docs/evidence/03-r11-probe-output.txt``）::

    PYTHONPATH=src python tools/backfill_r11_probe.py

覆盖：F1（块间全量/对外 20）、N1（跨块歧义去重 + 来源块）、N2（撤销计数）、
N3（序列化副本）、N4（显式 cache/不序列化）、N5（上限语义）、N8（单块不补扫）、
N10（同 owner 同 span 去重）、N11（slim 关系端点去空白）。
"""

from __future__ import annotations

from types import SimpleNamespace

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

DENSE_TEXT = "华为深圳" * 30


def _dense_logical() -> ExtractionResult:
    return ExtractionResult(
        entities=[Entity(name="华为", type="ORG"), Entity(name="深圳", type="LOC")],
        relations=[Relation(head="华为", predicate="位于", tail="深圳")],
    )


class _DenseFake:
    domain = SimpleNamespace(code="general")
    model_name = "probe-r11"
    local_backfill = True

    async def extract(self, document):
        result, report = backfill_provenance(_dense_logical(), document.text)
        return ExtractionOutput(
            document_id=document.id, domain_code="general",
            result=result, model_name=self.model_name, usage=Usage(),
            attempts=1,
            backfill=report.to_dict(max_relation_evidence_candidates=None),
        )


async def _pipeline(fake, body, n_chunks):
    chunks = [
        Chunk(id=f"d#c{i}", text=body, start=0, end=len(body), index=i)
        for i in range(n_chunks)
    ]
    return await ChunkedExtractionPipeline(fake, concurrency=1).run(
        Document(id="d", text=body), chunks)


class _OverlapFake:
    domain = SimpleNamespace(code="general")
    model_name = "probe-r11"
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


async def main() -> None:
    # ---- F1：块间全量，对外 20 ----
    report = lb.backfill_provenance(_dense_logical(), DENSE_TEXT)[1]
    key = next(iter(report.relation_evidence_candidates))
    internal = report.to_dict(max_relation_evidence_candidates=None)
    out = await _pipeline(_DenseFake(), DENSE_TEXT, 1)
    print("== F1: 分块路径保留全部候选 ==")
    print("in-memory:", len(report.relation_evidence_candidates[key]),
          "| to_dict(None):",
          len(internal["relation_evidence_candidates"][key]),
          "| pipeline out:",
          len(out.backfill["relation_evidence_candidates"][key]))
    serialized = lb.serialize_backfill_for_output(out.backfill)
    print("API/job 边界:",
          len(serialized["relation_evidence_candidates"][key]),
          "selected=",
          len([c for c in serialized["relation_evidence_candidates"][key]
               if c.get("selected")]))

    # ---- N1/N2/N10：三块重叠 ----
    print("\n== N1/N2/N10: 跨块 span 歧义对账 ==")
    out3 = await _pipeline(_OverlapFake(), "苹果发布了新品。", 3)
    ambiguous = out3.backfill["ambiguous_mentions"]
    print("ambiguous 条数:", len(ambiguous), "| chunk_ids:",
          ambiguous[0]["chunk_ids"] if ambiguous else [])
    print("reconciled_unattributed:", out3.backfill["reconciled_unattributed"],
          "| entities_grounded/total:",
          out3.backfill["entities_grounded"], "/",
          out3.backfill["entities_total"])

    class _SameFake(_DenseFake):
        async def extract(self, document):
            logical = ExtractionResult(
                entities=[Entity(name="苹果", type="ORG")])
            result, rep = backfill_provenance(logical, document.text)
            return ExtractionOutput(
                document_id=document.id, domain_code="general",
                result=result, model_name=self.model_name, usage=Usage(),
                attempts=1,
                backfill=rep.to_dict(max_relation_evidence_candidates=None))

    out_same = await _pipeline(_SameFake(), "苹果发布了新品。", 2)
    [same_entity] = out_same.result.entities
    print("同 owner 同 span mention 数:", len(same_entity.mentions))

    # ---- N3：序列化副本 ----
    print("\n== N3: to_dict 候选是副本 ==")
    before = [dict(c) for c in report.relation_evidence_candidates[key]]
    mutated = report.to_dict(max_relation_evidence_candidates=None)
    mutated["relation_evidence_candidates"][key][0]["selected"] = "mutated"
    print("内存候选未被改写:", report.relation_evidence_candidates[key] == before)

    # ---- N4：显式 cache，不序列化 ----
    print("\n== N4: 只扫一次 + 不 model_dump_json ==")
    scans = {"n": 0}
    dumps = {"n": 0}
    real_backfill = lb.backfill_provenance
    real_dump = ExtractionResult.model_dump_json

    def counting(result, text):
        scans["n"] += 1
        return real_backfill(result, text)

    def counting_dump(self, *a, **kw):
        dumps["n"] += 1
        return real_dump(self, *a, **kw)

    lb.backfill_provenance = counting
    ExtractionResult.model_dump_json = counting_dump
    try:
        cache: dict = {}
        logical = ExtractionResult(entities=[Entity(name="华为", type="ORG")])
        lb.cached_backfill_provenance(cache, logical, "华为在深圳。")
        lb.cached_backfill_provenance(cache, logical, "华为在深圳。")
    finally:
        lb.backfill_provenance = real_backfill
        ExtractionResult.model_dump_json = real_dump
    print("scans:", scans["n"], "| model_dump_json:", dumps["n"])

    # ---- N5：上限语义 ----
    print("\n== N5: 候选上限语义 ==")
    for limit in (None, 0, -5, 5):
        n = len(report.to_dict(
            max_relation_evidence_candidates=limit
        )["relation_evidence_candidates"][key])
        print(f"limit={limit!r}: {n}")

    # ---- N8：单块不补扫 ----
    print("\n== N8: 单块文档跳过整篇别名补扫 ==")
    single = backfill_provenance(
        ExtractionResult(entities=[
            Entity(name="苹果", type="ORG", aliases=["Acme"])]),
        "苹果发布了新品。")[1]
    constructions = {"n": 0}
    real_ac = lb.AhoCorasick

    class _SpyAC(real_ac):
        def __init__(self, patterns):
            constructions["n"] += 1
            super().__init__(patterns)

    lb.AhoCorasick = _SpyAC
    try:
        merge_backfill_reports(
            [BackfillReport.from_dict(
                single.to_dict(), chunk_start=0, chunk_id="d#c0")],
            document_text="苹果发布了新品。",
            single_chunk_covers_document=True)
        skipped = constructions["n"]
        merge_backfill_reports(
            [BackfillReport.from_dict(
                single.to_dict(), chunk_start=0, chunk_id="d#c0")],
            document_text="苹果发布了新品。")
        rescanned = constructions["n"] - skipped
    finally:
        lb.AhoCorasick = real_ac
    print("单块补扫次数:", skipped, "| 多块路径补扫次数:", rescanned)

    # ---- N11：关系端点去空白 ----
    print("\n== N11: slim 关系去重键去空白 ==")
    p0 = ExtractionResult(relations=[
        Relation(head="腾讯 ", predicate="发布", tail="混元")])
    p1 = ExtractionResult(relations=[
        Relation(head="腾讯", predicate="发布", tail="混元 ")])
    print("slim:", len(naive_merge(
        [p0, p1], strip_relation_endpoints=True).relations),
        "| legacy:", len(naive_merge([p0, p1]).relations))


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
