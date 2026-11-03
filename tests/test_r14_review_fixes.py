"""工单 03 第 14 轮评审（r14）发现的回归用例。

只钉住本轮可离线验证的行为：

* F1（should）无标点块上关系证据配对也必须**边配对边限流**，不能先把
  ``H×T`` 全物化再裁到 2000；
* F2（nit）别名存值与标准名/关系端点同口径（strip + 去空 + 去重）；
* F3（nit）回填取值语义变更后，抽取结果缓存的载荷版本必须递增；
* F5（nit）负数候选上限只告警一次，坏配置不刷日志。
"""

import logging
import time

import pytest

from moray.extraction import cache as cache_mod
from moray.extraction import local_backfill as lb
from moray.extraction.document import ExtractionOutput
from moray.kb.ingestor import Ingestor
from moray.schema import Entity, ExtractionResult, Relation


# ---------------------------------------------------------------------------
# F1：无标点块（单句）也不得先物化 H×T 全笛卡尔积
# ---------------------------------------------------------------------------
def test_relation_evidence_pairing_without_punctuation_is_bounded():
    """3000 字无标点、500×500 mention：配对应 <1s，候选在配对时就 ≤2000。

    旧实现先物化 250k 条 pair 再排序，候选列表长度会远大于进程内软上限。
    """
    text = "华为 深圳 " * 500  # 3000 字，整段没有句末标点 -> 单句
    result = ExtractionResult(
        entities=[Entity(name="华为", type="ORG"), Entity(name="深圳", type="LOC")],
        relations=[Relation(head="华为", predicate="位于", tail="深圳")],
    )

    started = time.perf_counter()
    _filled, report = lb.backfill_provenance(result, text)
    elapsed = time.perf_counter() - started

    assert elapsed < 1.0, f"无标点块配对耗时 {elapsed:.3f}s，疑似仍是 H×T 全物化"
    key = next(iter(report.relation_evidence_candidates))
    cands = report.relation_evidence_candidates[key]
    assert len(cands) == lb.MAX_INTERNAL_RELATION_EVIDENCE_CANDIDATES
    # 保留的是最近的一批：距离单调不减，首条即被选中的最近一对。
    distances = [c["distance"] for c in cands]
    assert distances == sorted(distances)
    assert cands[0]["selected"] is True
    assert cands[0]["distance"] == 3  # 第一次 「华为 深圳」的 head/tail 距离
    # 500×500 里距离 3 有 500 对、距离 9 有 998 对，前 2000 条必然落到距离 15。
    assert distances[-1] == 15


def test_relation_evidence_pairing_keeps_nearest_pairs_when_capped():
    """超过上限时保留的是**最近**的候选，而不是按出现顺序先到先得。"""
    # 让远距离对先出现、近距离对后出现，避免「截断前 N 条」也能通过。
    text = ("深圳 " * 300) + "华为在深圳。"
    result = ExtractionResult(
        entities=[Entity(name="华为", type="ORG"), Entity(name="深圳", type="LOC")],
        relations=[Relation(head="华为", predicate="位于", tail="深圳")],
    )
    _filled, report = lb.backfill_provenance(result, text)

    cands = report.relation_evidence_candidates[
        next(iter(report.relation_evidence_candidates))]
    selected = [c for c in cands if c["selected"]]
    assert len(selected) == 1
    assert selected[0]["distance"] == min(c["distance"] for c in cands)
    # 最近的一对出现在文末，若实现只保留扫描前缀就会丢掉它。
    assert selected[0]["head_start"] == text.index("华为")


def test_bounded_pairing_matches_bruteforce(monkeypatch):
    """把上限压到 5，逐值对比暴力全量排序的前 5 对（含距离并列的稳定序）。"""
    monkeypatch.setattr(lb, "MAX_INTERNAL_RELATION_EVIDENCE_CANDIDATES", 5)
    text = "华为 深圳 " * 20
    heads = [i for i in range(0, len(text), 6)]
    tails = [i + 3 for i in range(0, len(text), 6)]
    expected = sorted(
        ((abs(h - t), h, t) for h in heads for t in tails))[:5]

    _filled, report = lb.backfill_provenance(
        ExtractionResult(
            entities=[
                Entity(name="华为", type="ORG"),
                Entity(name="深圳", type="LOC"),
            ],
            relations=[Relation(head="华为", predicate="位于", tail="深圳")],
        ),
        text,
    )
    cands = report.relation_evidence_candidates[
        next(iter(report.relation_evidence_candidates))]
    actual = [(c["distance"], c["head_start"], c["tail_start"]) for c in cands]
    assert actual == expected


# ---------------------------------------------------------------------------
# F2：别名存值规范化
# ---------------------------------------------------------------------------
def test_backfill_normalizes_alias_values():
    text = "腾讯在深圳。"
    result, _report = lb.backfill_provenance(
        ExtractionResult(entities=[
            Entity(name=" 腾讯 ", type="ORG",
                   aliases=[" 鹅厂 ", "腾讯", "鹅厂", "", "   "]),
            # 重复输出同一逻辑实体，走 merge_entity_metadata 的别名并集路径。
            Entity(name="腾讯", type="ORG", aliases=[" 鹅厂 ", "企鹅帝国"]),
        ]),
        text,
    )

    assert len(result.entities) == 1
    assert result.entities[0].name == "腾讯"
    assert result.entities[0].aliases == ["鹅厂", "企鹅帝国"]


class _CapturingStore:
    """只记录 upsert 入参的最小 store 替身（不连库）。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def upsert_entity(self, **kwargs):
        self.calls.append(kwargs)
        return len(self.calls)


class _StubEmbedder:
    def embed_one(self, _text: str):
        return [0.0]


@pytest.mark.asyncio
async def test_ingestor_receives_clean_alias_values():
    """回填产物送进 Ingestor 时，落库入参已经是干净别名。"""
    text = "腾讯在深圳。"
    filled, _report = lb.backfill_provenance(
        ExtractionResult(entities=[
            Entity(name=" 腾讯 ", type="ORG", aliases=[" 鹅厂 ", "腾讯", ""]),
        ]),
        text,
    )
    store = _CapturingStore()
    output = ExtractionOutput(
        document_id="doc", domain_code="general", result=filled)

    await Ingestor(store, _StubEmbedder()).ingest(output)

    assert store.calls[0]["name"] == "腾讯"
    assert store.calls[0]["aliases"] == ["鹅厂"]


# ---------------------------------------------------------------------------
# F3：回填取值语义变了，缓存载荷版本必须跟着换命名空间
# ---------------------------------------------------------------------------
def test_payload_version_bumped_for_backfill_value_normalization():
    """r13 N3/N4 改了缓存载荷里的取值语义，版本号必须递增才不会回放旧脏值。"""
    assert cache_mod._PAYLOAD_VERSION >= 2
    before = cache_mod.build_cache_key("fp", "m", "正文")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cache_mod, "_PAYLOAD_VERSION", 1)
        legacy = cache_mod.build_cache_key("fp", "m", "正文")
    assert before != legacy


# ---------------------------------------------------------------------------
# F5：坏配置只告警一次
# ---------------------------------------------------------------------------
def test_negative_candidate_limit_warns_once(caplog):
    report = lb.BackfillReport()
    report.relation_evidence_candidates = {"k": [{"selected": True}]}
    logger_name = "moray.extraction.local_backfill"

    with caplog.at_level(logging.WARNING, logger=logger_name):
        report.to_dict(max_relation_evidence_candidates=-4321)
        report.to_dict(max_relation_evidence_candidates=-4321)

    warnings = [
        r for r in caplog.records
        if r.name == logger_name and r.levelno == logging.WARNING
        and "-4321" in r.getMessage()
    ]
    assert len(warnings) == 1
