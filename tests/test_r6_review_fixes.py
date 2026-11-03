"""工单 03 第 6 轮评审（r6）发现的回归用例。

只钉住本轮要修的两条 should + 两条 nit 的可离线验证行为：

* S1：同一标准名对应不同类型（同名异型）是**歧义**，不是「未去重」；
      歧义-only 输入不得触发模型返工。
* S2：跨块（≥2 chunk）的「歧义-only」不能因为 chunk-weighted 计数被
      误报成「回填命中率不足」；真正漏抽的实体仍要拦。
* N1：歧义端点的关系不再额外报一句事实错误的「端点在正文中未命中」；
       r8 F1 起进一步降为纯信号：不报 issue、不触发模型补问。
* N2：关闭态（无 backfill 信号）恢复改造前的口径：
      `names` 不做 strip、只认原样标准名、不引入 `owners` 唯一性
      （r7 F1 修正为与 262319b 逐字面等价）。
"""

from types import SimpleNamespace

from moray.extraction import Document, ExtractionOutput
from moray.extraction.document import Usage
from moray.extraction.local_backfill import (
    backfill_provenance,
)
from moray.orchestration.specialists import (
    ReviewVerdict,
    rule_review,
    run_chunked_team,
    run_extraction_team,
)
from moray.schema import Entity, ExtractionResult, Relation, Span


# ---------------------------------------------------------------------------
# S1：同名异型（苹果/ORG + 苹果/MISC）是歧义，不是「未去重」
# ---------------------------------------------------------------------------
async def test_same_name_different_type_is_ambiguity_not_duplicate_rework():
    """正文只含歧义字面：2 mention 歧义、模型只调用 1 次、accepted=True。"""
    text = "苹果发布了新手机，苹果很好吃。"
    logical = ExtractionResult(entities=[
        Entity(name="苹果", type="ORG"),
        Entity(name="苹果", type="MISC"),
    ])
    sink: dict = {}
    calls: list[str | None] = []

    async def extractor(body: str, feedback: str | None):
        calls.append(feedback)
        result, report = backfill_provenance(logical, body)
        sink["backfill"] = report.to_dict()
        return result

    reviewed = await run_extraction_team(
        text, extractor=extractor, backfill_sink=sink)

    assert len(calls) == 1, "同名异型歧义不得触发模型补问/返工"
    assert reviewed.accepted is True, reviewed.review.issues
    assert len(reviewed.backfill["ambiguous_mentions"]) == 2
    assert all(not e.mentions for e in reviewed.result.entities), (
        "歧义未归属不得写错实体")


# ---------------------------------------------------------------------------
# S2：跨块歧义-only 不误报；真漏抽仍拦（走 run_chunked_team 真实入口）
# ---------------------------------------------------------------------------
class _ChunkExtractor:
    """每块都输出同一组逻辑实体，并按块正文做确定性回填。"""

    domain = SimpleNamespace(code="general")
    model_name = "fake-r6"
    local_backfill = True

    def __init__(self, extra_entities=()):
        self.extra_entities = list(extra_entities)

    async def extract(self, document: Document):
        logical = ExtractionResult(entities=[
            Entity(name="腾讯", type="ORG", aliases=["同名"]),
            Entity(name="网易", type="ORG", aliases=["同名"]),
            Entity(name="混元", type="MISC"),
            *self.extra_entities,
        ])
        result, report = backfill_provenance(logical, document.text)
        return ExtractionOutput(
            document_id=document.id, domain_code="general",
            result=result, model_name=self.model_name,
            usage=Usage(), attempts=1, backfill=report.to_dict(),
        )


class _Provider:
    def __init__(self, extractor):
        self.extractor = extractor

    async def get_extractor(self, domain_code: str):
        return self.extractor, self.extractor.domain


async def _run_two_chunks(text: str, extractor: _ChunkExtractor):
    return await run_chunked_team(
        text,
        extractor=extractor.extract,
        provider=_Provider(extractor),
        chunk_threshold_units=1,
        chunk_max_units=12,
        chunk_overlap_units=0,
        chunk_concurrency=1,
    )


async def test_two_chunk_ambiguous_only_passes_without_rework():
    """两块各自歧义命中：chunk-weighted 计数不得变成假「命中率不足」。"""
    text = "同名发布了混元。同名也发布了混元。"
    reviewed = await _run_two_chunks(text, _ChunkExtractor())

    assert reviewed.backfill["entities_total"] == 6
    assert reviewed.backfill["entities_grounded"] == 2
    assert len(reviewed.backfill["ambiguous_mentions"]) == 2
    assert reviewed.accepted is True, reviewed.review.issues
    assert not any("回填命中率" in issue for issue in reviewed.review.issues)


async def test_two_chunk_true_missing_entity_still_needs_fix():
    """不搞一刀切：跨块里真有实体没在正文出现，仍要判 NEEDS_FIX。"""
    text = "同名发布了混元。同名也发布了混元。"
    reviewed = await _run_two_chunks(
        text, _ChunkExtractor([Entity(name="幽灵公司", type="ORG")]))

    assert reviewed.accepted is False
    assert any("回填命中率" in issue for issue in reviewed.review.issues), (
        reviewed.review.issues)


# ---------------------------------------------------------------------------
# N1：歧义端点不再重复报「端点在正文中未命中」；r8 F1 起降为纯信号
# ---------------------------------------------------------------------------
def test_ambiguous_endpoint_does_not_emit_false_not_grounded_issue():
    text = "同名发布了X公司。"
    result, report = backfill_provenance(
        ExtractionResult(
            entities=[
                Entity(name="腾讯", type="ORG", aliases=["同名"]),
                Entity(name="网易", type="ORG", aliases=["同名"]),
                Entity(name="X公司", type="ORG"),
            ],
            relations=[Relation(head="同名", predicate="发布", tail="X公司")],
        ),
        text,
    )
    review = rule_review(text, result, backfill=report.to_dict())

    # 歧义不是模型靠返工能解决的问题（工单：这版不做模型补问）。
    assert review.verdict == ReviewVerdict.PASS, review.issues
    assert report.ambiguous_mentions, "歧义信号必须保留在 BackfillReport"
    assert not any("端点在正文中未命中" in issue for issue in review.issues), (
        "歧义端点已作为信号保留，④ 不得再报事实错误的未命中")


# ---------------------------------------------------------------------------
# N2：关闭态恢复改造前的严格口径（不 strip、标准名不豁免）
# ---------------------------------------------------------------------------
def test_off_mode_does_not_strip_names_for_duplicate_check():
    text = "腾讯发布财报。"
    review = rule_review(
        text,
        ExtractionResult(entities=[
            Entity(name="腾讯", type="ORG"),
            Entity(name=" 腾讯", type="ORG"),
        ]),
        backfill=None,
    )

    assert review.verdict == ReviewVerdict.PASS, review.issues


def test_off_mode_same_name_different_type_endpoint_matches_262319b():
    """关闭态 ② 与 262319b 一致：同名异型标准名不产生端点问题（r7 F1）。"""
    text = "腾讯发布了混元。"
    result, report = backfill_provenance(
        ExtractionResult(
            entities=[
                Entity(name="腾讯", type="ORG"),
                Entity(name="腾讯", type="MISC"),
                Entity(name="混元", type="MISC"),
            ],
            relations=[Relation(
                head="腾讯", predicate="发布", tail="混元",
                evidence_span=Span(start=0, end=2, text="腾讯"))],
        ),
        text,
    )
    off = rule_review(text, result, backfill=None)
    slim = rule_review(text, result, backfill=report.to_dict())

    # 基线只有 ③「未去重」，不会给标准名端点加 ②「归属不唯一」。
    assert off.verdict == ReviewVerdict.NEEDS_FIX
    assert any("未去重" in issue for issue in off.issues), off.issues
    assert not any(
        "关系头实体「腾讯」" in issue for issue in off.issues), off.issues
    assert slim.verdict == ReviewVerdict.PASS, slim.issues
