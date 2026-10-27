"""
多智能体编排：抽取团队 + 质量闭环（L10）
========================================

AgentScope 2.0 提供两种编排原语（见 lessons/l10 的讲解）：
  · GoalPipeline：executor↔verifier 反思循环（结构化 schema 固定）；
  · TeamPipeline：leader 用 TeamAssign 工具把任务派给专精成员，成员并发、
    上下文隔离、互不直接对话，结果只回 leader。

本模块先不绑死某个框架类，而是把"抽取团队"抽象成【可注入的角色函数】：
    extractor(text, feedback) -> ExtractionResult   抽取员（可能犯错）
    reviewer(text, result)   -> ExtractionReview     审校员（挑错、给整改意见）
  外加一个编排循环 run_extraction_team：
    抽取 -> 审校 -> (pass 采用 / fail 带反馈返工，最多 max_iters 轮)。

好处：
  · 离线可测：测试和 OFFLINE 课程注入"确定性假角色"，秒跑、零花费、可复现；
  · 线上可换：把角色函数内部换成真实 Agent（甚至套 TeamPipeline）即可，
    编排逻辑、质量门禁、迭代预算完全不变。

这正是"多智能体"相对"单个大 Agent"的价值：用一个专职审校形成制衡，
把抽取员的臆测关系、错误 span、漏抽实体挡在入库之前。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable

from ..extraction.quality import verify_spans
from ..schema import ExtractionResult

# ---- 角色的类型：都是 async 可调用，方便注入真实 Agent 或假实现 -------------
ExtractorFn = Callable[[str, str | None], Awaitable[ExtractionResult]]


class ReviewVerdict:
    PASS = "pass"
    NEEDS_FIX = "needs_fix"


@dataclass
class ExtractionReview:
    """审校员的裁决。"""

    verdict: str                       # pass / needs_fix
    issues: list[str] = field(default_factory=list)   # 具体问题（给抽取员返工）
    span_ratio: float = 1.0            # 出处一致率（客观指标，非模型主观）
    iterations: int = 1


@dataclass
class ReviewedExtraction:
    """编排循环的最终产物。"""

    result: ExtractionResult
    accepted: bool                     # 是否在预算内通过审校
    review: ExtractionReview
    history: list[ExtractionReview] = field(default_factory=list)


ReviewerFn = Callable[[str, ExtractionResult], Awaitable[ExtractionReview]]


# ---------------------------------------------------------------------------
# 一个零成本的"规则审校员"：不调模型，只做能 100% 确定的客观检查。
# 真实系统里它是 LLM 审校 Agent 的第一道廉价闸门；这关不过就不必浪费模型。
# ---------------------------------------------------------------------------
def rule_review(text: str, result: ExtractionResult) -> ExtractionReview:
    issues: list[str] = []

    # ① span 客观核对（复用 L04 的质量探针）
    span_report = verify_spans(result, text)
    if not span_report.ok:
        for mm in span_report.mismatches:
            issues.append(f"出处错误·{mm['where']}：{mm['reason']}")

    # ② 关系端点必须在实体列表里（否则是指向空气的臆测关系）
    names = {e.name for e in result.entities}
    name_aliases = set()
    for e in result.entities:
        name_aliases.update(e.aliases)
    known = names | name_aliases
    for r in result.relations:
        if r.head not in names:
            issues.append(f"关系头实体「{r.head}」不在实体列表中（疑似臆测或漏抽实体）")
        if r.tail not in names:
            issues.append(f"关系尾实体「{r.tail}」不在实体列表中（疑似臆测或漏抽实体）")

    # ③ 重复实体（同名出现两次，抽取员没去重）
    if len(names) != len(result.entities):
        issues.append("实体列表存在同名重复项，未去重")

    # ④ 没有任何证据 span 的高置信关系（臆测风险）
    for r in result.relations:
        if r.evidence_span is None and r.confidence >= 0.8:
            issues.append(
                f"关系 {r.head}-[{r.predicate}]->{r.tail} 置信度高却无原文证据")

    verdict = ReviewVerdict.PASS if not issues else ReviewVerdict.NEEDS_FIX
    return ExtractionReview(verdict=verdict, issues=issues,
                            span_ratio=span_report.ratio)


async def run_extraction_team(
    text: str,
    *,
    extractor: ExtractorFn,
    reviewer: ReviewerFn | None = None,
    max_iters: int = 3,
) -> ReviewedExtraction:
    """抽取-审校-返工质量闭环。

    extractor: async (text, feedback) -> ExtractionResult
               第一轮 feedback=None；返工轮带上审校意见。
    reviewer:  async (text, result) -> ExtractionReview；默认用规则审校员。
    max_iters: 最多抽取轮数（含首轮），超出仍不通过则按最后结果返回并标记
               accepted=False，交给上层（通常转人工）。
    """
    async def _default_reviewer(t: str, r: ExtractionResult) -> ExtractionReview:
        return rule_review(t, r)

    reviewer = reviewer or _default_reviewer
    history: list[ExtractionReview] = []
    feedback: str | None = None
    last_result: ExtractionResult | None = None
    last_review: ExtractionReview | None = None

    for i in range(1, max_iters + 1):
        last_result = await extractor(text, feedback)
        last_review = await reviewer(text, last_result)
        last_review.iterations = i
        history.append(last_review)

        if last_review.verdict == ReviewVerdict.PASS:
            return ReviewedExtraction(
                result=last_result, accepted=True,
                review=last_review, history=history)

        # 把问题清单拼成下一轮反馈
        feedback = "请修正以下问题后重新输出完整结果：\n" + "\n".join(
            f"- {x}" for x in last_review.issues)

    # 预算耗尽仍未通过
    return ReviewedExtraction(
        result=last_result, accepted=False,
        review=last_review, history=history)


# ---------------------------------------------------------------------------
# 长文分块编排入口（波次 4 双路接线）
# ---------------------------------------------------------------------------
async def run_chunked_team(
    text: str,
    *,
    extractor: ExtractorFn,
    provider=None,
    domain_code: str = "general",
    max_iters: int = 3,
    chunk_threshold_units: int = 4000,
    chunk_max_units: int = 1500,
    chunk_overlap_units: int = 200,
    chunk_concurrency: int = 4,
    usage=None,
) -> ReviewedExtraction:
    """超过分块阈值且有真 Extractor 可取时，走「分块 map → 全局坐标归并 →
    单轮规则审校」；否则逐字节保持 :func:`run_extraction_team` 旧行为。

    为什么两条路径的审校形态不同：
      · 整篇路径的返工循环 = 带反馈**全文重抽**，代价可接受；
      · 分块路径若返工 = 全文重抽，代价不可接受——所以合并结果只做一轮
        客观审校，PASS/FAIL 如实记录（accepted 语义不变，FAIL 转人工）。
    离线/测试注入的是闭包 extractor_fn（``provider=None``）：它给不出
    Extractor 对象（pipeline 要 .domain/.extract(Document)），不分块——
    这是刻意保守：宁可不拆，也不在假抽取上验证真语义。

    ``usage`` 传入 Usage 累加器时，分块路径把 pipeline 聚合的 token 用量
    累加进去（闭包路径的用量由调用方挂 provider.extract 的 usage_sink，两不冲突）。
    返回的 review.iterations 在分块路径恒为 1（单轮），整篇路径为实际轮数。
    """
    if provider is None:
        return await run_extraction_team(text, extractor=extractor, max_iters=max_iters)

    from ..extraction.chunking import Chunker
    from ..extraction.document import Document as _Doc

    chunker = Chunker(max_units=chunk_max_units, overlap_units=chunk_overlap_units)
    if chunker.count(text) <= chunk_threshold_units:
        return await run_extraction_team(text, extractor=extractor, max_iters=max_iters)

    real, _domain = await provider.get_extractor(domain_code)
    from ..extraction.pipeline import ChunkedExtractionPipeline

    pipeline = ChunkedExtractionPipeline(real, concurrency=chunk_concurrency)
    chunks = chunker.chunk_text(text)
    output = await pipeline.run(
        _Doc(id="chunked", text=text, source="chunked", domain_code=domain_code), chunks)
    if usage is not None:
        usage.add(output.usage)

    review = rule_review(text, output.result)
    review.iterations = 1
    return ReviewedExtraction(
        result=output.result,
        accepted=(review.verdict == ReviewVerdict.PASS),
        review=review,
        history=[review],
    )
