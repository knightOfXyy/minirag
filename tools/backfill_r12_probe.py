"""工单 03 r12 定点探针：逐条复现评审 12 条发现（离线、确定性）。

用法：
    PYTHONPATH=src python tools/backfill_r12_probe.py
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from moray.extraction import Chunk, ChunkedExtractionPipeline, Document, Extractor
from moray.extraction import local_backfill as lb
from moray.extraction.document import ExtractionOutput, Usage
from moray.extraction.local_backfill import (
    backfill_provenance,
    merge_backfill_reports,
)
from moray.extraction.pipeline import MergeMode, naive_merge
from moray.observability import RunRecorder
from moray.schema import Entity, ExtractionResult, Relation


class _FakeEmbedder:
    def embed_one(self, text):
        return [0.0]


class _Store:
    def __init__(self):
        self.entities = {}
        self.relations = []

    async def upsert_entity(self, *, name, type, **_kwargs):
        self.entities.setdefault(
            name, {"id": len(self.entities) + 1, "name": name, "type": type})
        return self.entities[name]["id"]

    async def get_entity_id(self, name, *, domain_code=None):
        rec = self.entities.get(name)
        return rec["id"] if rec else None

    async def upsert_relation(self, **kwargs):
        self.relations.append(kwargs)
        return len(self.relations)

    async def add_relation_evidence(self, rid, **kwargs):
        return None


class _Stub(Extractor):
    def __init__(self, reply, *, local_backfill=True):
        super().__init__("general", max_retries=0, local_backfill=local_backfill)
        self._reply = reply

    def _build_agent(self):
        return SimpleNamespace()

    async def _call_structured(self, agent, content):
        return SimpleNamespace(
            structured_output=self._reply,
            usage=SimpleNamespace(input_tokens=10, output_tokens=5))


def _dense_reply():
    return {
        "entities": [
            {"name": "华为", "type": "ORG"},
            {"name": "深圳", "type": "LOC"}],
        "relations": [{"head": "华为", "predicate": "位于", "tail": "深圳"}],
    }


async def s1():
    from moray.kb.ingestor import Ingestor

    p0 = ExtractionResult(
        entities=[Entity(name=" 腾讯", type="ORG"),
                  Entity(name="混元", type="PRODUCT")],
        relations=[Relation(head="腾讯 ", predicate="发布", tail="混元 ")])
    p1 = ExtractionResult(
        entities=[Entity(name="腾讯", type="ORG"),
                  Entity(name="混元", type="PRODUCT")],
        relations=[Relation(head="腾讯", predicate="发布", tail="混元")])
    merged = naive_merge([p0, p1], **_SLIM_KWARGS)
    store = _Store()
    await Ingestor(store, _FakeEmbedder()).ingest(
        ExtractionOutput(document_id="d", domain_code="general", result=merged))
    print(
        "S1 merged_entities=", [e.name for e in merged.entities],
        "relations=", [(r.head, r.tail) for r in merged.relations],
        "ingest_entities=", sorted(store.entities),
        "placeholder_misc=", [n for n, v in store.entities.items()
                              if v["type"] == "MISC"])


_SLIM_KWARGS = dict(
    merge_relation_duplicates=True, fill_description=True,
    strip_entity_name=True, strip_relation_endpoints=True)


def s2():
    text = "苹果" * 16000
    result = ExtractionResult(entities=[
        Entity(name="苹果", type="ORG"), Entity(name="苹果", type="MISC")])
    t0 = time.perf_counter()
    _filled, report = backfill_provenance(result, text)
    print(
        f"S2 16000_ambiguous_hits elapsed={time.perf_counter() - t0:.4f}s "
        f"entries={len(report.ambiguous_mentions)}")


class _EvidenceFake:
    domain = SimpleNamespace(code="general")
    model_name = "probe-r12"
    local_backfill = True

    async def extract(self, document):
        logical = ExtractionResult(
            entities=[Entity(name="华为", type="ORG"),
                      Entity(name="深圳", type="LOC")],
            relations=[Relation(head="华为", predicate="位于", tail="深圳")])
        result, report = backfill_provenance(logical, document.text)
        return ExtractionOutput(
            document_id=document.id, domain_code="general", result=result,
            model_name=self.model_name, usage=Usage(), attempts=1,
            backfill=report.to_dict(max_relation_evidence_candidates=None))


async def n1():
    body = "华为在深圳发布。"
    chunks = [Chunk(id=f"d#c{i}", text=body, start=0, end=len(body), index=i)
              for i in range(2)]
    out = await ChunkedExtractionPipeline(
        _EvidenceFake(), concurrency=1).run(Document(id="d", text=body), chunks)
    key = next(iter(out.backfill["relation_evidence_candidates"]))
    cands = out.backfill["relation_evidence_candidates"][key]
    print(
        f"N1 merged_candidates={len(cands)} "
        f"chunk_ids={cands[0].get('chunk_ids')} "
        f"selected={len([c for c in cands if c.get('selected')])}")


async def n2_n3():
    for repeats in (30, 50):
        text = "华为深圳" * repeats
        out = await _Stub(_dense_reply()).extract(Document(id="d", text=text))
        key = next(iter(out.backfill["relation_evidence_candidates"]))
        cands = out.backfill["relation_evidence_candidates"][key]
        print(f"N2/N3 real_extractor {repeats}x{repeats}="
              f"{repeats * repeats} candidates={len(cands)}")
    print("N2 internal_cap=", lb.MAX_INTERNAL_RELATION_EVIDENCE_CANDIDATES,
          "output_cap=", lb.MAX_SERIALIZED_RELATION_EVIDENCE_CANDIDATES)


def n4():
    cache = {}
    result = ExtractionResult(entities=[Entity(name="华为", type="ORG")])
    a = lb.cached_backfill_provenance(cache, result, "华为在深圳。")
    b = lb.cached_backfill_provenance(cache, result, "华为在深圳。")
    try:
        lb.cached_backfill_provenance(cache, result, "x", SimpleNamespace())
        extra_arg = "accepted"
    except TypeError:
        extra_arg = "TypeError"
    print(f"N4 cache_entries={len(cache)} same_result={b[0] is a[0]} "
          f"legacy_owner_arg={extra_arg}")


class _AliasFake:
    domain = SimpleNamespace(code="general")
    model_name = "probe-r12"
    local_backfill = True

    async def extract(self, document):
        logical = ExtractionResult(entities=[
            Entity(name="华为", type="ORG", aliases=["深圳"])])
        result, report = backfill_provenance(logical, document.text)
        return ExtractionOutput(
            document_id=document.id, domain_code="general", result=result,
            model_name=self.model_name, usage=Usage(), attempts=1,
            backfill=report.to_dict(max_relation_evidence_candidates=None))


async def n6():
    full, prefix = "华为在这里。深圳也有。", "华为在这里。"
    chunks = [Chunk(id="d#c0", text=prefix, start=0, end=len(prefix), index=0)]
    out = await ChunkedExtractionPipeline(
        _AliasFake(), concurrency=1).run(Document(id="d", text=full), chunks)
    print("N6 partial_single_chunk_unmatched_aliases=",
          out.backfill["unmatched_aliases"])


def n7():
    fields = RunRecorder._backfill_fields({
        "reconciled_unattributed": 2, "ambiguous_mentions": []})
    print("N7 dashboard_fields=", fields)


def n8():
    original = {
        "matched_literals": {"k": 1}, "seen_literals": ["华为"],
        "unmatched_aliases": [{"literal": "阿里"}],
        "ambiguous_mentions": [{"surface": "苹果", "candidates": ["a"],
                                "chunk_ids": ["c0"]}],
        "relations_without_evidence": [{"head": "A"}],
        "relation_evidence_candidates": {"k": [{"head_start": 0}]},
    }
    out = lb.serialize_backfill_for_output(original)
    out["matched_literals"]["k"] = 99
    out["ambiguous_mentions"][0]["candidates"].append("b")
    out["relation_evidence_candidates"]["k"][0]["head_start"] = 99
    ok = (
        original["matched_literals"] == {"k": 1}
        and original["ambiguous_mentions"][0]["candidates"] == ["a"]
        and original["relation_evidence_candidates"]["k"][0]["head_start"] == 0)
    print("N8 deep_copy=", ok)


def n9_n10():
    p0 = ExtractionResult(
        entities=[Entity(name=" 腾讯", type="ORG")],
        relations=[Relation(head="腾讯 ", predicate="发布", tail="混元 ")])
    p1 = ExtractionResult(
        entities=[Entity(name="腾讯", type="ORG", description="腾讯集团")],
        relations=[Relation(head="腾讯", predicate="发布", tail="混元")])
    slim = naive_merge([p0, p1], mode=MergeMode.SLIM)
    legacy = naive_merge([p0, p1])
    print(f"N9 slim_entities={[e.name for e in slim.entities]} "
          f"slim_relations={len(slim.relations)} "
          f"legacy_entities={len(legacy.entities)} "
          f"legacy_relations={len(legacy.relations)}")
    _filled, report = backfill_provenance(
        ExtractionResult(
            entities=[Entity(name="华为", type="ORG"),
                      Entity(name="深圳", type="LOC")],
            relations=[Relation(head="华为", predicate="位于", tail="深圳")]),
        "华为深圳")
    key = next(iter(report.relation_evidence_candidates))
    print("N10 limit_0=", report.to_dict(
        max_relation_evidence_candidates=0
    )["relation_evidence_candidates"][key])
    try:
        report.to_dict(max_relation_evidence_candidates=-5)
        negative = "accepted"
    except ValueError as exc:
        negative = f"ValueError: {exc}"
    print("N10 limit_-5=", negative)


async def main():
    await s1()
    s2()
    await n1()
    await n2_n3()
    n4()
    await n6()
    n7()
    n8()
    n9_n10()


if __name__ == "__main__":
    asyncio.run(main())
