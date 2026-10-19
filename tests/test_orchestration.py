"""多智能体编排质量闭环的单测（离线、确定性、不联网）。"""

import pytest

from moray.orchestration import run_extraction_team
from moray.orchestration.specialists import (
    ReviewVerdict,
    rule_review,
)
from moray.schema import (
    Entity,
    EntityMention,
    ExtractionResult,
    Relation,
    Span,
)

TEXT = "华为技术有限公司位于深圳。"


def _good() -> ExtractionResult:
    i = TEXT.find("华为技术有限公司")
    j = TEXT.find("深圳")
    return ExtractionResult(
        entities=[
            Entity(name="华为技术有限公司", type="ORG",
                   mentions=[EntityMention(
                       surface="华为技术有限公司",
                       span=Span(start=i, end=i + 8, text=TEXT[i:i + 8]))]),
            Entity(name="深圳", type="LOC",
                   mentions=[EntityMention(
                       surface="深圳", span=Span(start=j, end=j + 2, text="深圳"))]),
        ],
        relations=[
            Relation(head="华为技术有限公司", predicate="总部位于", tail="深圳",
                     confidence=0.7,
                     evidence_span=Span(start=i, end=j + 2, text=TEXT[i:j + 2])),
        ],
    )


@pytest.mark.asyncio
async def test_pass_on_first_round():
    async def extractor(text, feedback):
        assert feedback is None
        return _good()

    out = await run_extraction_team(TEXT, extractor=extractor, max_iters=3)
    assert out.accepted is True
    assert out.review.iterations == 1
    assert out.review.issues == []


@pytest.mark.asyncio
async def test_feedback_driven_rework_then_pass():
    calls = {"n": 0}

    async def extractor(text, feedback):
        calls["n"] += 1
        if feedback is None:
            # 首轮：span 错误 + 悬空关系
            return ExtractionResult(
                entities=[Entity(name="华为技术有限公司", type="ORG",
                    mentions=[EntityMention(surface="华为",
                        span=Span(start=0, end=2, text="错"))])],
                relations=[Relation(head="华为技术有限公司", predicate="总部位于",
                                    tail="不存在", confidence=0.95)])
        assert "不存在" in feedback  # 返工反馈确实送达
        return _good()

    out = await run_extraction_team(TEXT, extractor=extractor, max_iters=3)
    assert out.accepted is True
    assert out.review.iterations == 2
    assert calls["n"] == 2
    assert out.history[0].verdict == ReviewVerdict.NEEDS_FIX
    assert out.history[1].verdict == ReviewVerdict.PASS


@pytest.mark.asyncio
async def test_budget_exhausted_not_accepted():
    async def always_bad(text, feedback):
        # 永远有一个悬空关系
        return ExtractionResult(
            entities=[Entity(name="A", type="ORG")],
            relations=[Relation(head="A", predicate="r", tail="幽灵", confidence=0.9)])

    out = await run_extraction_team(TEXT, extractor=always_bad, max_iters=2)
    assert out.accepted is False
    assert out.review.iterations == 2
    assert len(out.history) == 2


@pytest.mark.asyncio
async def test_rule_review_detects_duplicate_and_missing_evidence():
    bad = ExtractionResult(
        entities=[
            Entity(name="A", type="ORG"),
            Entity(name="A", type="ORG"),  # 重复
        ],
        relations=[
            Relation(head="A", predicate="r", tail="A", confidence=0.95),  # 无证据
        ],
    )
    review = rule_review(TEXT, bad)
    assert review.verdict == ReviewVerdict.NEEDS_FIX
    joined = " ".join(review.issues)
    assert "重复" in joined
    assert "证据" in joined


@pytest.mark.asyncio
async def test_custom_reviewer_injection():
    """审校员也可注入：验证编排对角色函数完全解耦。"""
    from moray.orchestration.specialists import ExtractionReview

    async def lenient(text, result):
        return ExtractionReview(verdict=ReviewVerdict.PASS, issues=[])

    async def extractor(text, feedback):
        return ExtractionResult(entities=[Entity(name="A", type="ORG")])

    out = await run_extraction_team(TEXT, extractor=extractor, reviewer=lenient)
    assert out.accepted is True
