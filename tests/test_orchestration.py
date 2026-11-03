"""多智能体编排质量闭环的单测（离线、确定性、不联网）。"""

import pytest

from moray.extraction.document import Usage

from moray.orchestration import run_chunked_team, run_extraction_team
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


# ---------------------------------------------------------------------------
# 波次 4：run_chunked_team 长文分块编排（离线、确定性、假 provider）
# ---------------------------------------------------------------------------
class _FakeRealExtractor:
    """满足 ChunkedExtractionPipeline 的最小契约：.domain/.model_name/extract。"""

    def __init__(self, domain_code: str):
        self.domain = type("D", (), {"code": domain_code})()
        self.model_name = "fake"
        self.seen_chunks: list[str] = []

    async def extract(self, document):
        from moray.extraction.document import ExtractionOutput, Usage
        self.seen_chunks.append(document.text)
        # 每块首 6 字出一个实体（避免「第1」vs「第10」前缀撞名被 naive_merge 合并），
        # span 为【块内】偏移 0..6——pipeline 会 remap 回全局
        name = document.text[:6]
        res = ExtractionResult(entities=[Entity(
            name=name, type="ORG",
            mentions=[EntityMention(surface=name,
                                    span=Span(start=0, end=len(name), text=name))])])
        return ExtractionOutput(document_id=document.id, domain_code=self.domain.code,
                                result=res, model_name=self.model_name,
                                usage=Usage(input_tokens=3, output_tokens=2), attempts=1)


class _FakeProvider:
    def __init__(self, real: _FakeRealExtractor):
        self._real = real
        self.get_calls = 0

    async def get_extractor(self, domain_code):
        self.get_calls += 1
        return self._real, self._real.domain


@pytest.mark.asyncio
async def test_chunked_team_below_threshold_uses_fulltext():
    """未超阈值：委托旧整篇闭环，provider.get_extractor 绝不触发。"""
    called = {"n": 0}

    async def closure(text, feedback):
        called["n"] += 1
        return _good()

    real = _FakeRealExtractor("case_domain")
    provider = _FakeProvider(real)
    out = await run_chunked_team(TEXT, extractor=closure, provider=provider,
                                 domain_code="case_domain", chunk_threshold_units=999999)
    assert called["n"] == 1            # 走了整篇闭环
    assert provider.get_calls == 0     # 没碰分块
    assert out.accepted is True


@pytest.mark.asyncio
async def test_chunked_team_no_provider_always_fulltext():
    """无 provider（离线闭包）：无论多长都不分块，保持旧行为。"""
    called = {"n": 0}

    async def closure(text, feedback):
        called["n"] += 1
        return ExtractionResult(entities=[], relations=[])  # 空结果首轮即 PASS

    long_text = "句子内容。" * 5000
    out = await run_chunked_team(long_text, extractor=closure, provider=None,
                                 chunk_threshold_units=100)
    assert called["n"] == 1
    assert out.accepted is True


@pytest.mark.asyncio
async def test_chunked_team_long_text_maps_and_remaps():
    """超阈值 + 真 provider：走分块 pipeline，闭包不执行，span 换算回全局。"""
    async def closure(text, feedback):  # 不该被调用
        raise AssertionError("chunk path should not invoke fulltext closure")

    # 每句起头唯一，overlap=0 时块边界落在句首，首 2 字即唯一实体名
    text = "".join(f"第{n}段记载某某事件{n}。" for n in range(1, 40))
    real = _FakeRealExtractor("case_domain")
    provider = _FakeProvider(real)
    usage = Usage(input_tokens=0, output_tokens=0)

    out = await run_chunked_team(text, extractor=closure, provider=provider,
                                 domain_code="case_domain",
                                 chunk_threshold_units=20, chunk_max_units=40,
                                 chunk_overlap_units=0, chunk_concurrency=2,
                                 usage=usage)
    assert provider.get_calls == 1
    assert len(real.seen_chunks) >= 2          # 确实被切了多块
    assert len(out.result.entities) == len(real.seen_chunks)
    # 全局坐标：每条 mention 的 span.text 必须能在全文原文里对上
    for e in out.result.entities:
        for m in e.mentions:
            assert m.span is not None
            assert text[m.span.start:m.span.end] == m.span.text
    # usage 由 pipeline 聚合后累加进外部 sink（每块 3/2）
    assert usage.input_tokens == 3 * len(real.seen_chunks)
    assert usage.output_tokens == 2 * len(real.seen_chunks)
    # 分块路径单轮审校，iterations 恒 1
    assert out.review.iterations == 1


@pytest.mark.asyncio
async def test_chunked_team_exposes_backfill_signal():
    """编排层也不能吞掉回填信号：ReviewedExtraction.backfill 要能透出。"""
    from moray.extraction.document import ExtractionOutput
    from moray.extraction.local_backfill import backfill_provenance
    from moray.schema import ExtractionResult as ER

    class _BackfillReal:
        def __init__(self):
            self.domain = type("D", (), {"code": "case_domain"})()
            self.model_name = "fake"

        async def extract(self, document):
            local, report = backfill_provenance(
                ER(entities=[Entity(name=document.text[:2], type="ORG")]),
                document.text)
            return ExtractionOutput(
                document_id=document.id, domain_code="case_domain",
                result=local, model_name=self.model_name, attempts=1,
                backfill=report.to_dict())

    text = "华为在深圳。" * 200
    provider = _FakeProvider(_BackfillReal())
    async def closure(text, feedback):
        raise AssertionError("chunk path should not invoke fulltext closure")

    out = await run_chunked_team(
        text, extractor=closure, provider=provider, domain_code="case_domain",
        chunk_threshold_units=10, chunk_max_units=20, chunk_overlap_units=0)
    assert isinstance(out.backfill, dict)
    assert out.backfill["entities_total"] >= 1
    assert out.backfill["entities_grounded"] >= 1
