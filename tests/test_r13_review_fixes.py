"""工单 03 第 13 轮评审（r13）发现的回归用例。

只钉住本轮可离线验证的行为：

* S1（should）回填缓存键必须覆盖 attributes/description/confidence，
  否则重试只改属性时会取回上一轮的 filled 结果；
* N1（nit）序列化边界连 ``chunk_ids`` 一起深拷贝；
* N3（nit）短文档/直连 Extractor 路径也做存值规范化（实体名、关系端点）；
* N4（nit）``entity_signal_key`` 对 name 去空白，跨块不拆信号键；
* N5（nit）四个兼容 shim 都能显式覆盖 ``MergeMode``；
* N6（nit）负数候选上限在序列化边界 clamp 到默认值并记 warning，不再 500。
"""

import logging

import pytest

from moray.extraction import local_backfill as lb
from moray.extraction.pipeline import MergeMode, naive_merge
from moray.schema import Entity, ExtractionResult, Relation


# ---------------------------------------------------------------------------
# S1：缓存键覆盖回填结果里所有会被重试修正的字段
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "field, old_value, new_value",
    [
        ("attributes", {"code": "WRONG"}, {"code": "RIGHT"}),
        ("description", "旧描述", "新描述"),
        ("confidence", 0.1, 0.9),
    ],
)
def test_backfill_cache_key_includes_revised_fields(
        field, old_value, new_value):
    cache: dict = {}
    text = "华为在深圳。"

    first = ExtractionResult(entities=[
        Entity(name="华为", type="ORG", **{field: old_value})])
    revised = ExtractionResult(entities=[
        Entity(name="华为", type="ORG", **{field: new_value})])

    _filled_first, _ = lb.cached_backfill_provenance(cache, first, text)
    filled_revised, _ = lb.cached_backfill_provenance(cache, revised, text)

    # 重试只改了一个不参与 mention/span 定位的字段，但缓存值里它会被回灌。
    assert getattr(filled_revised.entities[0], field) == new_value


# ---------------------------------------------------------------------------
# N1：序列化/落盘边界连 chunk_ids 一起复制
# ---------------------------------------------------------------------------
def _report_with_nested_chunk_ids() -> lb.BackfillReport:
    report = lb.BackfillReport()
    report.relations_without_evidence = [
        {"head": "A", "predicate": "关联", "tail": "B", "chunk_ids": ["c0"]},
    ]
    report.relation_evidence_candidates = {
        "k": [{
            "head_start": 0, "tail_start": 1, "evidence_start": 0,
            "evidence_end": 2, "selected": True, "chunk_ids": ["c0"],
        }],
    }
    return report


def test_to_dict_deep_copies_nested_chunk_ids():
    report = _report_with_nested_chunk_ids()
    out = report.to_dict(max_relation_evidence_candidates=None)

    out["relations_without_evidence"][0]["chunk_ids"].append("MUTATED")
    out["relation_evidence_candidates"]["k"][0]["chunk_ids"].append("MUTATED")

    assert report.relations_without_evidence[0]["chunk_ids"] == ["c0"]
    assert report.relation_evidence_candidates["k"][0]["chunk_ids"] == ["c0"]


def test_serialize_backfill_deep_copies_nested_chunk_ids():
    payload = _report_with_nested_chunk_ids().to_dict(
        max_relation_evidence_candidates=None)
    out = lb.serialize_backfill_for_output(payload)

    out["relations_without_evidence"][0]["chunk_ids"].append("MUTATED")
    out["relation_evidence_candidates"]["k"][0]["chunk_ids"].append("MUTATED")

    assert payload["relations_without_evidence"][0]["chunk_ids"] == ["c0"]
    assert payload["relation_evidence_candidates"]["k"][0]["chunk_ids"] == ["c0"]


# ---------------------------------------------------------------------------
# N3：短文档路径（不走 naive_merge）也要把存值规范化
# ---------------------------------------------------------------------------
def test_backfill_normalizes_entity_names_and_relation_endpoints():
    result = ExtractionResult(
        entities=[
            Entity(name=" 腾讯", type="ORG", aliases=[" 鹅厂 "]),
            Entity(name="混元 ", type="PRODUCT"),
        ],
        relations=[Relation(head=" 腾讯 ", predicate="发布", tail="混元 ")],
    )
    filled, _report = lb.backfill_provenance(result, "腾讯发布混元。")

    assert [e.name for e in filled.entities] == ["腾讯", "混元"]
    assert len(filled.relations) == 1
    assert (filled.relations[0].head, filled.relations[0].tail) == (
        "腾讯", "混元")
    # span 仍精确切片，规范化没有动坐标系。
    assert filled.entities[0].mentions[0].span.text == "腾讯"


# ---------------------------------------------------------------------------
# N4：信号键与身份键同口径（name 去首尾空白）
# ---------------------------------------------------------------------------
def test_entity_signal_key_strips_name():
    assert (lb.entity_signal_key(Entity(name=" 腾讯 ", type="ORG"))
            == lb.entity_signal_key(Entity(name="腾讯", type="ORG")))


def test_merged_matched_literals_use_stripped_signal_key():
    key_padded = lb.entity_signal_key(Entity(name=" 腾讯", type="ORG"))
    key_clean = lb.entity_signal_key(Entity(name="腾讯", type="ORG"))

    merged = lb.merge_backfill_reports([
        lb.BackfillReport(matched_literals={key_padded: 1}),
        lb.BackfillReport(matched_literals={key_clean: 2}),
    ])

    assert merged.matched_literals == {key_clean: 3}


# ---------------------------------------------------------------------------
# N5：四个兼容 shim 都能显式覆盖 MergeMode.SLIM
# ---------------------------------------------------------------------------
def test_all_four_shims_override_slim_mode():
    p0 = ExtractionResult(
        entities=[Entity(name=" 腾讯", type="ORG")],
        relations=[Relation(head="腾讯 ", predicate="发布", tail="混元 ")],
    )
    p1 = ExtractionResult(
        entities=[Entity(name="腾讯", type="ORG", description="腾讯集团")],
        relations=[Relation(head="腾讯", predicate="发布", tail="混元")],
    )

    # 四个 shim 全部显式关掉：即使 mode=SLIM，也逐项回到 LEGACY 行为。
    out = naive_merge(
        [p0, p1],
        mode=MergeMode.SLIM,
        merge_relation_duplicates=False,
        fill_description=False,
        strip_entity_name=False,
        strip_relation_endpoints=False,
    )

    assert len(out.entities) == 2          # strip_entity_name=False
    assert out.entities[1].description == ""  # fill_description=False
    assert len(out.relations) == 2         # merge_relation_duplicates=False
    assert {(r.head, r.tail) for r in out.relations} == {
        ("腾讯 ", "混元 "), ("腾讯", "混元")}   # strip_relation_endpoints=False


# ---------------------------------------------------------------------------
# N6：负数候选上限 clamp + warning，不在序列化边界抛错
# ---------------------------------------------------------------------------
def _dense_report() -> lb.BackfillReport:
    # 同句 30×30 共现 -> 一条关系 900 条候选，确保超过对外窗口 20。
    result = ExtractionResult(
        entities=[
            Entity(name="华为", type="ORG"),
            Entity(name="深圳", type="LOC"),
        ],
        relations=[Relation(head="华为", predicate="位于", tail="深圳")],
    )
    _filled, report = lb.backfill_provenance(result, "华为深圳" * 30)
    return report


def test_negative_candidate_limit_clamps_to_default_and_warns(caplog):
    report = _dense_report()
    key = next(iter(report.relation_evidence_candidates))
    assert len(report.relation_evidence_candidates[key]) > (
        lb.MAX_SERIALIZED_RELATION_EVIDENCE_CANDIDATES)

    with caplog.at_level(logging.WARNING, logger=lb.__name__):
        data = report.to_dict(max_relation_evidence_candidates=-5)

    assert len(data["relation_evidence_candidates"][key]) == (
        lb.MAX_SERIALIZED_RELATION_EVIDENCE_CANDIDATES)
    assert any("clamp" in rec.message for rec in caplog.records)

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=lb.__name__):
        out = lb.serialize_backfill_for_output(
            report.to_dict(max_relation_evidence_candidates=None),
            max_relation_evidence_candidates=-1)

    assert len(out["relation_evidence_candidates"][key]) == (
        lb.MAX_SERIALIZED_RELATION_EVIDENCE_CANDIDATES)
    assert any("clamp" in rec.message for rec in caplog.records)
