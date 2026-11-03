"""工单 03 r13 评审发现的确定性离线探针（无模型、无 DB）。

逐条打印本轮修复的可观测结果：

* S1：重试只改 attributes/description/confidence 时，回填缓存不再回灌旧值；
* N1：to_dict / serialize_backfill_for_output 的 chunk_ids 是独立副本；
* N3：短文档路径（backfill_provenance）规范化实体名与关系端点；
* N4：entity_signal_key 去空白，跨块 merged matched_literals 不拆键；
* N6：负数候选上限在序列化边界 clamp + warning，不抛错。

用法::

    PYTHONPATH=src python tools/backfill_r13_probe.py
"""

from __future__ import annotations

import logging

from moray.extraction import local_backfill as lb
from moray.schema import Entity, ExtractionResult, Relation


def probe_s1() -> None:
    print("== S1 缓存键覆盖重试会修正的字段 ==")
    text = "华为在深圳。"
    for field, old, new in (
        ("attributes", {"code": "WRONG"}, {"code": "RIGHT"}),
        ("description", "旧描述", "新描述"),
        ("confidence", 0.1, 0.9),
    ):
        cache: dict = {}
        first = ExtractionResult(
            entities=[Entity(name="华为", type="ORG", **{field: old})])
        revised = ExtractionResult(
            entities=[Entity(name="华为", type="ORG", **{field: new})])
        lb.cached_backfill_provenance(cache, first, text)
        filled, _ = lb.cached_backfill_provenance(cache, revised, text)
        print(f"  {field}: 期望 {new!r} -> 实际 "
              f"{getattr(filled.entities[0], field)!r}")
        assert getattr(filled.entities[0], field) == new
        assert len(cache) == 2


def probe_n1() -> None:
    print("== N1 chunk_ids 深拷贝 ==")
    report = lb.BackfillReport()
    report.relations_without_evidence = [
        {"head": "A", "tail": "B", "chunk_ids": ["c0"]}]
    report.relation_evidence_candidates = {
        "k": [{"head_start": 0, "chunk_ids": ["c0"]}]}

    data = report.to_dict(max_relation_evidence_candidates=None)
    data["relations_without_evidence"][0]["chunk_ids"].append("MUTATED")
    data["relation_evidence_candidates"]["k"][0]["chunk_ids"].append("MUTATED")
    print("  to_dict 后内存:",
          report.relations_without_evidence[0]["chunk_ids"],
          report.relation_evidence_candidates["k"][0]["chunk_ids"])
    assert report.relations_without_evidence[0]["chunk_ids"] == ["c0"]
    assert report.relation_evidence_candidates["k"][0]["chunk_ids"] == ["c0"]

    payload = report.to_dict(max_relation_evidence_candidates=None)
    out = lb.serialize_backfill_for_output(payload)
    out["relations_without_evidence"][0]["chunk_ids"].append("MUTATED")
    out["relation_evidence_candidates"]["k"][0]["chunk_ids"].append("MUTATED")
    print("  serialize 后输入:",
          payload["relations_without_evidence"][0]["chunk_ids"],
          payload["relation_evidence_candidates"]["k"][0]["chunk_ids"])
    assert payload["relations_without_evidence"][0]["chunk_ids"] == ["c0"]
    assert payload["relation_evidence_candidates"]["k"][0]["chunk_ids"] == ["c0"]


def probe_n3() -> None:
    print("== N3 短文档路径存值规范化 ==")
    logical = ExtractionResult(
        entities=[
            Entity(name=" 腾讯", type="ORG", aliases=[" 鹅厂 "]),
            Entity(name="混元 ", type="PRODUCT"),
        ],
        relations=[Relation(head=" 腾讯 ", predicate="发布", tail="混元 ")],
    )
    filled, _ = lb.backfill_provenance(logical, "腾讯发布混元。")
    print("  names=", [e.name for e in filled.entities],
          " relation=", [(r.head, r.tail) for r in filled.relations])
    assert [e.name for e in filled.entities] == ["腾讯", "混元"]
    assert (filled.relations[0].head, filled.relations[0].tail) == (
        "腾讯", "混元")
    assert filled.entities[0].mentions[0].span.text == "腾讯"


def probe_n4() -> None:
    print("== N4 信号键去空白 ==")
    padded = lb.entity_signal_key(Entity(name=" 腾讯", type="ORG"))
    clean = lb.entity_signal_key(Entity(name="腾讯", type="ORG"))
    print("  padded=", padded, " clean=", clean)
    assert padded == clean
    merged = lb.merge_backfill_reports([
        lb.BackfillReport(matched_literals={padded: 1}),
        lb.BackfillReport(matched_literals={clean: 2}),
    ])
    print("  merged.matched_literals=", merged.matched_literals)
    assert merged.matched_literals == {clean: 3}


def probe_n6() -> None:
    print("== N6 负数候选上限 clamp + warning ==")
    logical = ExtractionResult(
        entities=[
            Entity(name="华为", type="ORG"),
            Entity(name="深圳", type="LOC"),
        ],
        relations=[Relation(head="华为", predicate="位于", tail="深圳")],
    )
    _filled, report = lb.backfill_provenance(logical, "华为深圳" * 30)
    key = next(iter(report.relation_evidence_candidates))
    logging.basicConfig(level=logging.WARNING, format="  %(levelname)s %(message)s")
    data = report.to_dict(max_relation_evidence_candidates=-5)
    print("  to_dict(-5) candidates=",
          len(data["relation_evidence_candidates"][key]))
    assert len(data["relation_evidence_candidates"][key]) == (
        lb.MAX_SERIALIZED_RELATION_EVIDENCE_CANDIDATES)


if __name__ == "__main__":
    probe_s1()
    probe_n1()
    probe_n3()
    probe_n4()
    probe_n6()
    print("全部探针通过")
