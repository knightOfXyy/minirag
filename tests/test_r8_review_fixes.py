"""工单 03 第 8 轮评审（r8）发现的回归用例。

只钉住本轮要修的、可离线验证的行为：

* F1（should）别名冲突的关系端点只作为 ``ambiguous_mentions`` 质量信号，
  不再写进 issues/feedback 触发模型返工（工单「这版不做模型补问」）。
* F2（nit）关闭态 ④ 对「高置信 + 无证据」恢复 262319b 原文措辞
  （差分 fixture 在 ``test_r7_review_fixes.py`` 里同步补了无证据关系）。
* F4（nit）关系证据配对先按 mention 预计算句下标、按句分桶，
  不再对每个头 mention 线性扫全部句子（O(H·T·S)）。
* F5（nit）瘦身模式把只有空白差异的重复标准名合并成同一逻辑实体，
  不再退化成「同一字面归属多个实体」的歧义信号。

F3/F6 是文档/历史口径，不涉及可执行行为，证据见 r8 修复文档。
"""

import time

from moray.extraction.local_backfill import backfill_provenance
from moray.orchestration.specialists import (
    ReviewVerdict,
    rule_review,
    run_extraction_team,
)
from moray.schema import Entity, ExtractionResult, Relation


# ---------------------------------------------------------------------------
# F1：歧义关系端点只作为信号，不触发返工
# ---------------------------------------------------------------------------
def _ambiguous_alias_result() -> ExtractionResult:
    return ExtractionResult(
        entities=[
            Entity(name="腾讯", type="ORG", aliases=["同名"]),
            Entity(name="网易", type="ORG", aliases=["同名"]),
            Entity(name="X公司", type="ORG"),
        ],
        relations=[Relation(head="同名", predicate="发布", tail="X公司")],
    )


def test_ambiguous_alias_relation_endpoint_is_signal_not_rework_issue():
    """歧义端点不再报『归属不唯一（疑似臆测）』，信号留在 report 里。"""
    text = "同名发布了X公司。"
    result, report = backfill_provenance(_ambiguous_alias_result(), text)

    review = rule_review(text, result, backfill=report.to_dict())

    assert review.verdict == ReviewVerdict.PASS, review.issues
    assert review.issues == []
    assert report.ambiguous_mentions, "歧义信号必须保留，不能静默丢弃"


async def test_ambiguous_alias_relation_endpoint_passes_in_one_model_call():
    """端到端：歧义端点不得把合法歧义升级成 3 次调用 / accepted=False。"""
    text = "同名发布了X公司。"
    logical = _ambiguous_alias_result()
    sink: dict = {}
    calls: list[str | None] = []

    async def extractor(body: str, feedback: str | None):
        calls.append(feedback)
        result, report = backfill_provenance(logical, body)
        sink["backfill"] = report.to_dict()
        return result

    reviewed = await run_extraction_team(
        text, extractor=extractor, backfill_sink=sink)

    assert len(calls) == 1, f"歧义不是模型能靠返工解决的问题：{reviewed.review.issues}"
    assert reviewed.accepted is True, reviewed.review.issues
    assert reviewed.backfill["ambiguous_mentions"], "歧义信号必须随结果返回"


# ---------------------------------------------------------------------------
# F4：关系证据配对不得随「头×尾×句」超线性膨胀
# ---------------------------------------------------------------------------
def test_relation_evidence_pairing_is_linearish():
    """5600 字 / 800 句 / 1600 mention：先红时 O(H·T·S) 约 5.2s。"""
    text = "腾讯见了混元。" * 800
    result = ExtractionResult(
        entities=[Entity(name="腾讯", type="ORG"), Entity(name="混元", type="MISC")],
        relations=[Relation(head="腾讯", predicate="见", tail="混元")],
    )

    started = time.perf_counter()
    _result, report = backfill_provenance(result, text)
    elapsed = time.perf_counter() - started

    assert report.relations_with_evidence == 1
    assert elapsed < 1.0, f"关系证据配对耗时 {elapsed:.3f}s，疑似仍是 O(H·T·S)"


# ---------------------------------------------------------------------------
# F5：只有空白差异的重复标准名应合并，而不是变成歧义
# ---------------------------------------------------------------------------
def test_whitespace_variant_duplicate_is_coalesced_not_ambiguous():
    """``腾讯`` 与 `` 腾讯`` 是同一逻辑实体；正文只出现一次 -> 1 mention。"""
    text = "腾讯发布了混元。"
    result, report = backfill_provenance(
        ExtractionResult(entities=[
            Entity(name="腾讯", type="ORG"),
            Entity(name=" 腾讯", type="ORG"),
            Entity(name="混元", type="MISC"),
        ]),
        text,
    )

    assert report.entities_total == 2
    assert report.ambiguous_mentions == []
    tencent = [e for e in result.entities if e.type == "ORG"]
    assert len(tencent) == 1
    assert [m.surface for m in tencent[0].mentions] == ["腾讯"]
