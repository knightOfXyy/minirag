"""偏移换算与朴素归并的单测（不联网）。"""

from moray.extraction import Chunk, naive_merge, remap_spans
from moray.schema import Entity, EntityMention, ExtractionResult, Relation, Span


def test_remap_shifts_spans_by_chunk_start():
    text = "某某前缀华为技术有限公司后缀"
    # 块从第 4 个字符开始，块内 [0:8] = 华为技术有限公司
    chunk = Chunk(id="x#c0", text=text[4:12], start=4, end=12, index=0)
    partial = ExtractionResult(
        entities=[
            Entity(
                name="华为技术有限公司", type="ORG",
                mentions=[EntityMention(
                    surface="华为技术有限公司",
                    span=Span(start=0, end=8, text="华为技术有限公司"),
                )],
            )
        ]
    )
    out = remap_spans(partial, chunk)
    span = out.entities[0].mentions[0].span
    assert (span.start, span.end) == (4, 12)
    assert text[span.start:span.end] == "华为技术有限公司"


def test_remap_drops_out_of_range_span():
    # 块内偏移越界（end 超过块长）应被置空，而不是写错位置
    chunk = Chunk(id="x", text="短", start=0, end=1, index=0)
    partial = ExtractionResult(entities=[
        Entity(name="x", type="ORG", mentions=[
            EntityMention(surface="x", span=Span(start=0, end=5, text="x"))])
    ])
    out = remap_spans(partial, chunk)
    assert out.entities[0].mentions[0].span is None


def test_naive_merge_dedupes_entity_and_unions_mentions():
    a = ExtractionResult(entities=[
        Entity(name="华为", type="ORG", aliases=["华为公司"],
               mentions=[EntityMention(surface="华为")], confidence=0.8)
    ])
    b = ExtractionResult(entities=[
        Entity(name="华为", type="ORG",
               mentions=[EntityMention(surface="华为技术")], confidence=0.95)
    ])
    merged = naive_merge([a, b])
    assert len(merged.entities) == 1
    e = merged.entities[0]
    assert e.confidence == 0.95
    assert "华为公司" in e.aliases
    assert len(e.mentions) == 2


def test_naive_merge_relation_dedup():
    r1 = [Relation(head="甲", predicate="投", tail="乙")]
    a = ExtractionResult(entities=[], relations=r1)
    b = ExtractionResult(entities=[], relations=r1)  # 完全重复
    merged = naive_merge([a, b])
    assert len(merged.relations) == 1


def test_naive_merge_does_not_drop_later_relation_evidence():
    """同一三元组重复出现时，先见的空证据不能覆盖后见的真实证据。"""
    a = ExtractionResult(
        entities=[Entity(name="甲", type="ORG"), Entity(name="乙", type="ORG")],
        relations=[Relation(head="甲", predicate="投", tail="乙")],
    )
    ev = Span(start=0, end=4, text="甲投乙。")
    b = ExtractionResult(
        entities=[],
        relations=[Relation(head="甲", predicate="投", tail="乙",
                            evidence_span=ev, confidence=0.8)],
    )
    merged = naive_merge([a, b])
    assert len(merged.relations) == 1
    r = merged.relations[0]
    assert r.evidence_span is not None
    assert (r.evidence_span.start, r.evidence_span.end) == (0, 4)
    assert r.confidence == 0.8
    # 反向顺序（先有证据）也不能丢
    merged2 = naive_merge([b, a])
    assert merged2.relations[0].evidence_span is not None


def _merge_golden_inputs():
    a = ExtractionResult(
        entities=[
            Entity(name="甲", type="ORG", aliases=["甲甲"], confidence=0.4,
                   attributes={"k": "a"}),
            Entity(name="乙", type="PER", confidence=0.6),
        ],
        relations=[Relation(head="甲", predicate="投", tail="乙", confidence=0.4)],
    )
    b = ExtractionResult(
        entities=[
            Entity(name="甲", type="ORG", aliases=["甲总"], confidence=0.9,
                   attributes={"k": "b", "x": 1}, description="甲集团简介"),
        ],
        relations=[Relation(
            head="甲", predicate="投", tail="乙",
            evidence_span=Span(start=0, end=4, text="甲投乙。"), confidence=0.9)],
    )
    return a, b


def test_switch_off_merge_matches_pre_change_full_field_golden():
    """验收 7：开关关闭时归并结果要对 262319b 的逐字段快照（全字段，不只 schema）。"""
    import json
    from pathlib import Path

    golden = json.loads(
        (Path(__file__).parent / "golden" / "switch_off_naive_merge_262319b.json")
        .read_text(encoding="utf-8"))
    a, b = _merge_golden_inputs()
    merged = naive_merge([a, b], merge_relation_duplicates=False)
    assert merged.model_dump() == golden["legacy_merged"]

    # 开关打开（默认改进版）才把后见证据/更高置信度并进重复三元组
    improved = naive_merge([a, b])
    relation = improved.relations[0]
    assert relation.evidence_span is not None
    assert relation.confidence == 0.9
