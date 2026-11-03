"""工单 03 第 7 轮评审（r7）发现的回归用例。

只钉住本轮要修的四条发现：

* F1（should）关闭态 ② 必须与 262319b 逐字面等价：只认实体标准名原样
  精确命中，不 strip、不看归属唯一性；带空白标准名端点不得触发返工。
  这里同时归档 262319b 的 ``rule_review`` 做差分（verdict + issues）。
* F2（should）验收 8 的口径由 owner 决定，用例面不涉及（见证据文档）。
* F3（nit）⑤ 遇到空 ``matched_literals`` 时必须退回计数口径，不能当成
  「全部命中」静默放行。
* F4（nit）真同型同名重复在真实链路上由回填前 ``_coalesce_entities``
  合并；补端到端用例钉住「合并为 1 个实体、accepted=True」的真实语义，
  不再用生产不可达的 slim ③ 分支冒充保障。
"""

from moray.extraction.local_backfill import backfill_provenance
from moray.extraction.quality import verify_spans
from moray.orchestration.specialists import (
    ReviewVerdict,
    rule_review,
    run_extraction_team,
)
from moray.schema import Entity, ExtractionResult, Relation, Span


# ---------------------------------------------------------------------------
# 归档基线：262319b 的 rule_review（逐字复制，仅用于差分回归）
# ---------------------------------------------------------------------------
def _rule_review_262319b(text: str, result: ExtractionResult):
    """262319b:src/moray/orchestration/specialists.py 的 rule_review 原文。

    差分用例比较 ①②③④ 的 verdict + issues；除 ``high_conf_no_evidence``
    刻意构造「高置信 + 无证据」外，其余 fixture 都带合法证据 span，避免 ④
    因后续「置信度未知」语义变更产生无关差异。
    """
    issues: list[str] = []

    span_report = verify_spans(result, text)
    if not span_report.ok:
        for mm in span_report.mismatches:
            issues.append(f"出处错误·{mm['where']}：{mm['reason']}")

    names = {e.name for e in result.entities}
    for r in result.relations:
        if r.head not in names:
            issues.append(f"关系头实体「{r.head}」不在实体列表中（疑似臆测或漏抽实体）")
        if r.tail not in names:
            issues.append(f"关系尾实体「{r.tail}」不在实体列表中（疑似臆测或漏抽实体）")

    if len(names) != len(result.entities):
        issues.append("实体列表存在同名重复项，未去重")

    for r in result.relations:
        if r.evidence_span is None and r.confidence >= 0.8:
            issues.append(
                f"关系 {r.head}-[{r.predicate}]->{r.tail} 置信度高却无原文证据")

    verdict = ReviewVerdict.PASS if not issues else ReviewVerdict.NEEDS_FIX
    return verdict, issues


def _span(text: str, surface: str) -> Span:
    start = text.index(surface)
    return Span(start=start, end=start + len(surface), text=surface)


def _review_fixtures():
    """覆盖干净端点/别名/带空白名/同名异型/真重复/缺端点六类输入。"""
    clean_text = "腾讯发布了混元。"
    clean = ExtractionResult(
        entities=[Entity(name="腾讯", type="ORG"), Entity(name="混元", type="MISC")],
        relations=[Relation(
            head="腾讯", predicate="发布", tail="混元", confidence=0.9,
            evidence_span=_span(clean_text, "腾讯发布了混元"))],
    )

    alias_text = "鹅厂发布了混元。"
    alias = ExtractionResult(
        entities=[
            Entity(name="腾讯", type="ORG", aliases=["鹅厂"]),
            Entity(name="混元", type="MISC"),
        ],
        relations=[Relation(
            head="鹅厂", predicate="发布", tail="混元", confidence=0.9,
            evidence_span=_span(alias_text, "鹅厂发布了混元"))],
    )

    whitespace_text = " 腾讯发布了混元。"
    whitespace = ExtractionResult(
        entities=[Entity(name=" 腾讯", type="ORG"), Entity(name="混元", type="MISC")],
        relations=[Relation(
            head=" 腾讯", predicate="发布", tail="混元", confidence=0.9,
            evidence_span=_span(whitespace_text, " 腾讯发布了混元"))],
    )

    ambiguous_text = "腾讯发布了混元。"
    same_name_diff_type = ExtractionResult(
        entities=[
            Entity(name="腾讯", type="ORG"),
            Entity(name="腾讯", type="MISC"),
            Entity(name="混元", type="MISC"),
        ],
        relations=[Relation(
            head="腾讯", predicate="发布", tail="混元", confidence=0.9,
            evidence_span=_span(ambiguous_text, "腾讯发布了混元"))],
    )

    duplicate_text = "腾讯发布了混元。"
    true_duplicate = ExtractionResult(
        entities=[
            Entity(name="腾讯", type="ORG"),
            Entity(name="腾讯", type="ORG"),
            Entity(name="混元", type="MISC"),
        ],
        relations=[Relation(
            head="腾讯", predicate="发布", tail="混元", confidence=0.9,
            evidence_span=_span(duplicate_text, "腾讯发布了混元"))],
    )

    missing_text = "腾讯发布了混元。"
    missing_endpoint = ExtractionResult(
        entities=[Entity(name="腾讯", type="ORG"), Entity(name="混元", type="MISC")],
        relations=[Relation(
            head="幽灵", predicate="发布", tail="混元", confidence=0.9,
            evidence_span=_span(missing_text, "腾讯发布了混元"))],
    )

    # r8 F2：把 ④ 也纳入关闭态逐字节差分——高置信 + 无证据时，
    # 262319b 的反馈文案是「置信度高却无原文证据」。
    high_conf_text = "腾讯发布了混元。"
    high_conf_no_evidence = ExtractionResult(
        entities=[Entity(name="腾讯", type="ORG"), Entity(name="混元", type="MISC")],
        relations=[Relation(
            head="腾讯", predicate="发布", tail="混元", confidence=0.9)],
    )

    return [
        ("clean", clean_text, clean),
        ("alias_endpoint", alias_text, alias),
        ("whitespace_name", whitespace_text, whitespace),
        ("same_name_diff_type", ambiguous_text, same_name_diff_type),
        ("true_duplicate", duplicate_text, true_duplicate),
        ("missing_endpoint", missing_text, missing_endpoint),
        ("high_conf_no_evidence", high_conf_text, high_conf_no_evidence),
    ]


def test_off_mode_rule_review_matches_262319b_on_all_fixtures():
    """关闭态 ①②③④ 与归档基线逐条一致（verdict + issues 原文）。"""
    for name, text, result in _review_fixtures():
        expected_verdict, expected_issues = _rule_review_262319b(text, result)
        review = rule_review(text, result, backfill=None)
        assert review.verdict == expected_verdict, (name, review.issues)
        assert review.issues == expected_issues, name


async def test_off_mode_whitespace_name_endpoint_passes_in_one_model_call():
    """带空白标准名端点不得被 ② 误判：与 262319b 一样 1 次调用即 accepted。"""
    text = " 腾讯发布了混元。"
    logical = ExtractionResult(
        entities=[Entity(name=" 腾讯", type="ORG"), Entity(name="混元", type="MISC")],
        relations=[Relation(
            head=" 腾讯", predicate="发布", tail="混元", confidence=0.9,
            evidence_span=_span(text, " 腾讯发布了混元"))],
    )
    calls: list[str | None] = []

    async def extractor(body: str, feedback: str | None):
        calls.append(feedback)
        return logical

    reviewed = await run_extraction_team(text, extractor=extractor, backfill_sink={})

    assert len(calls) == 1, f"关闭态不得因空白名端点返工：{reviewed.review.issues}"
    assert reviewed.accepted is True, reviewed.review.issues


# ---------------------------------------------------------------------------
# F3：空 matched_literals 不能当成「全部命中」
# ---------------------------------------------------------------------------
def test_empty_matched_literals_falls_back_to_count_based_undercoverage():
    """旧式精简 backfill 带空 matched_literals 时，仍按计数判定漏抽。"""
    text = "腾讯发布了混元。"
    result = ExtractionResult(entities=[
        Entity(name="腾讯", type="ORG"),
        Entity(name="幽灵公司", type="ORG"),
    ])
    backfill = {
        "entities_total": 2,
        "entities_grounded": 1,
        "block_confidence": 0.5,
        "matched_literals": {},
        "ambiguous_mentions": [],
        "unmatched_aliases": [],
    }

    review = rule_review(text, result, backfill=backfill)

    assert review.verdict == ReviewVerdict.NEEDS_FIX, review.issues
    assert any("回填命中率" in issue for issue in review.issues), review.issues


# ---------------------------------------------------------------------------
# F4：真同型同名重复在真实链路被静默合并（不是靠 slim ③ 拦截）
# ---------------------------------------------------------------------------
async def test_duplicate_same_type_and_name_is_coalesced_end_to_end():
    """重复输出 (type,name) → 合并为 1 个实体、别名取并集、不返工。"""
    text = "腾讯发布了混元，鹅厂和企鹅都在。"
    logical = ExtractionResult(entities=[
        Entity(name="腾讯", type="ORG", aliases=["鹅厂"]),
        Entity(name="腾讯", type="ORG", aliases=["企鹅"]),
        Entity(name="混元", type="MISC"),
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

    assert len(calls) == 1, "同型同名重复是静默合并，不应触发返工"
    assert reviewed.accepted is True, reviewed.review.issues
    assert len(reviewed.result.entities) == 2
    tencent = next(e for e in reviewed.result.entities if e.name == "腾讯")
    assert tencent.aliases == ["鹅厂", "企鹅"]
    assert reviewed.backfill["entities_total"] == 2

