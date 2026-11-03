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

import inspect
from dataclasses import dataclass, field, replace
from typing import Awaitable, Callable

from ..extraction.chunk_metrics import DocumentMetrics, merge_call_metrics
from ..extraction.extractor import ExtractionError
from ..extraction.local_backfill import literal_index
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
    span_ratio: float | None = None      # 出处一致率；没有可核对 span 时为 None（未测）
    iterations: int = 1


@dataclass
class ReviewedExtraction:
    """编排循环的最终产物。"""

    result: ExtractionResult
    accepted: bool                     # 是否在预算内通过审校
    review: ExtractionReview
    history: list[ExtractionReview] = field(default_factory=list)
    # 分块路径的文档级度量（工单 01）：每块一条调用记录 + 聚合摘要。
    # 整篇旧闭环（含离线闭包路径）为 None，不伪造分块数据。
    document_metrics: object | None = None
    # 瘦身回填的确定性质量信号（仅分块且开关打开时有值；其余为 None）。
    # 它是旁路可观测信息，不属于稳定内核 Entity/Relation。
    backfill: dict | None = None


ReviewerFn = Callable[[str, ExtractionResult], Awaitable[ExtractionReview]]


# ---------------------------------------------------------------------------
# 一个零成本的"规则审校员"：不调模型，只做能 100% 确定的客观检查。
# 真实系统里它是 LLM 审校 Agent 的第一道廉价闸门；这关不过就不必浪费模型。
# ---------------------------------------------------------------------------
def rule_review(
    text: str, result: ExtractionResult, *, backfill: dict | None = None
) -> ExtractionReview:
    """规则审校：只做客观可判的事（span/端点/重复/证据/回填命中）。

    backfill 是瘦身回填的确定性信号（``BackfillReport.to_dict()``）：模型
    不再自报 confidence 时，"有没有正文出处"就是比 0.0/1.0 更可信的质量依据。
    """
    issues: list[str] = []

    # ① span 客观核对（复用 L04 的质量探针）
    span_report = verify_spans(result, text)
    if not span_report.ok:
        for mm in span_report.mismatches:
            issues.append(f"出处错误·{mm['where']}：{mm['reason']}")

    # ② 关系端点必须在实体列表里（否则是指向空气的臆测关系）。
    #    与 local_backfill 的关系回填共用同一个 literal 索引（r5 F8）：
    #    标准名永远算已知；「唯一归属」的别名仅在瘦身模式（有 backfill 信号）
    #    下放宽（r4 N1）。关闭态/full 模式保持改造前的严格口径（r5 F1）。
    names = {e.name.strip() for e in result.entities}
    index = literal_index(result.entities)
    # 瘦身/回填模式才放宽「唯一归属别名即端点合法」（03 r5 F1）：backfill 信号
    # 只在 MORAY_LOCAL_BACKFILL 打开（slim）时由调用方透传，关闭态保持严格口径。
    slim = backfill is not None
    literal_owners: dict[str, set[tuple[str, str]]] = {
        literal: {(result.entities[i].type, result.entities[i].name)
                  for i in idxs}
        for literal, idxs in index.items()
    }

    def _endpoint_issue(role: str, literal: str) -> str | None:
        stripped = literal.strip()
        if stripped in names:
            return None
        owners = literal_owners.get(stripped, set())
        if slim and len(owners) == 1:
            return None
        if len(owners) > 1:
            # 字面确实在实体列表里，只是归属不唯一；不要误导模型去“补实体”
            # （r5 F4）。
            return (f"关系{role}实体「{literal}」字面归属不唯一或未知"
                    f"（疑似臆测）")
        return f"关系{role}实体「{literal}」不在实体列表中（疑似臆测或漏抽实体）"

    for r in result.relations:
        for role, literal in (("头", r.head), ("尾", r.tail)):
            issue = _endpoint_issue(role, literal)
            if issue:
                issues.append(issue)

    # ③ 重复实体（同名出现两次，抽取员没去重）
    if len(names) != len(result.entities):
        issues.append("实体列表存在同名重复项，未去重")

    # ④ 没有任何证据 span 的关系（臆测风险）。
    #    · full 模式（无 backfill 信号）：旧语义仍只看「未知 + 高置信」；
    #    · slim 模式：模型不输出证据，证据为空本身不是错误。用回填信号区分：
    #      两端都在正文中被命中（同句/跨句/跨块均可）→ 合法，证据留空；
    #      端点根本没在正文出现 → 才是臆测，要拦。
    def _endpoint_grounded(name: str) -> bool:
        name = name.strip()
        for ent in result.entities:
            aliases = {a.strip() for a in ent.aliases}
            if ent.name.strip() == name or name in aliases:
                return bool(ent.mentions)
        return False

    for r in result.relations:
        if r.evidence_span is not None:
            continue
        if backfill is not None:
            if _endpoint_grounded(r.head) and _endpoint_grounded(r.tail):
                continue
            issues.append(
                f"关系 {r.head}-[{r.predicate}]->{r.tail} 无原文证据"
                f"（端点在正文中未命中）")
        elif r.confidence is None or r.confidence >= 0.8:
            issues.append(
                f"关系 {r.head}-[{r.predicate}]->{r.tail} 无原文证据"
                f"（置信度{'未知' if r.confidence is None else r.confidence}）")

    # ⑤ 确定性回填信号：块级命中率 + 正文不存在的别名。
    #    歧义未归属（ambiguous_mentions）只作为质量信号保留在 backfill 里，
    #    **不**进 issues/feedback：这版不做模型补问（r5 F2），歧义不是模型
    #    靠返工能解决的问题，写进 feedback 只会多调模型且仍然 accepted=False。
    if backfill:
        total = int(backfill.get("entities_total") or 0)
        grounded = int(backfill.get("entities_grounded") or 0)
        confidence = backfill.get("block_confidence")
        # 歧义命中的候选实体没有被归属（entities_grounded 少算），但它们
        # 确实在正文里出现过，不能因此触发“未找到出处”的模型返工。
        matched = backfill.get("matched_literals") or {}
        grounded_keys = {key for key, n in matched.items() if n}
        ambiguous = backfill.get("ambiguous_mentions") or []
        ambiguous_only = {
            key for am in ambiguous for key in (am.get("candidates") or [])
        } - grounded_keys
        covered = grounded + len(ambiguous_only)
        if confidence is not None and covered < total:
            issues.append(
                f"回填命中率 {grounded}/{total}"
                f"（块级置信度 {confidence}）：有实体未在正文中找到出处")
        phantom = backfill.get("unmatched_aliases") or []
        if phantom:
            issues.append(f"{len(phantom)} 个模型给出的别名在正文中不存在（疑似幻觉）")

    verdict = ReviewVerdict.PASS if not issues else ReviewVerdict.NEEDS_FIX
    # 没有可核对的 span（slim 模型不输出）时记「未测」(None)，不要写空转的 1.0，
    # 否则看板 pipeline_runs.span_ratio 会伪装成 100% 一致；回填信号单独走
    # block_confidence，两者不混。
    return ExtractionReview(
        verdict=verdict, issues=issues,
        span_ratio=span_report.ratio if span_report.total else None)


async def run_extraction_team(
    text: str,
    *,
    extractor: ExtractorFn,
    reviewer: ReviewerFn | None = None,
    max_iters: int = 3,
    backfill_sink: dict | None = None,
) -> ReviewedExtraction:
    """抽取-审校-返工质量闭环。

    extractor: async (text, feedback) -> ExtractionResult
               第一轮 feedback=None；返工轮带上审校意见。
    reviewer:  async (text, result) -> ExtractionReview；默认用规则审校员。
    max_iters: 最多抽取轮数（含首轮），超出仍不通过则按最后结果返回并标记
               accepted=False，交给上层（通常转人工）。
    backfill_sink: 抽取器把本次确定性回填信号写进 ``sink["backfill"]``；
               默认审校员在每轮审校时读取，用于判定块级命中率/歧义。
               返回的 ReviewedExtraction 也带上，避免主链丢信号。
    """
    async def _default_reviewer(t: str, r: ExtractionResult) -> ExtractionReview:
        # 每轮都现读 sink：抽取器刚写进去的块级信号要能被当轮审校消费。
        current = (backfill_sink or {}).get("backfill")
        return rule_review(t, r, backfill=current)

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
                review=last_review, history=history,
                backfill=(backfill_sink or {}).get("backfill"))

        # 把问题清单拼成下一轮反馈
        feedback = "请修正以下问题后重新输出完整结果：\n" + "\n".join(
            f"- {x}" for x in last_review.issues)

    # 预算耗尽仍未通过
    return ReviewedExtraction(
        result=last_result, accepted=False,
        review=last_review, history=history,
        backfill=(backfill_sink or {}).get("backfill"))


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
    result_cache=None,
    document_id: str | None = None,
    metrics_sink: list | None = None,
    backfill_sink: dict | None = None,
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

    ``result_cache`` 非 None 时启用分块结果【内容级缓存】（纯加速层）：键=领域
    配置指纹+模型身份（provider+base_url+模型名）+块正文，命中不调模型、
    用量记 cache_hits 而非真实 token。
    调用方应在【同一篇文档的多次运行/多个作业】间复用同一个缓存实例，断点续跑、
    失败重试、重复提交才能命中；缺省 None，行为与无缓存逐字节一致。

    ``document_id`` 是写进度量记录的真实文档 id（缺省占位 "chunked"，
    便于看板把度量行对回具体文档）；``metrics_sink`` 由调用方**显式**传入时
    才为短文档路径收集调用度量——不传就完全用调用方自己的 extractor，
    不隐式换实现（nit4），因此 document_metrics 保持 None。短文档**永久失败**
    时同样把已花的度量挂到 ``ExtractionError.document_metrics`` 上（S1），
    失败看板行不会是 token=0 / truncation_signals=0。
    """
    if provider is None:
        return await run_extraction_team(
            text, extractor=extractor, max_iters=max_iters,
            backfill_sink=backfill_sink)

    from ..extraction.chunking import Chunker
    from ..extraction.document import Document as _Doc

    chunker = Chunker(max_units=chunk_max_units, overlap_units=chunk_overlap_units)
    if chunker.count(text) <= chunk_threshold_units:
        # 短文档虽然不走分块 pipeline，仍可能有 finish_reason=length：
        # 调用方**显式**传 metrics_sink 时，才向 provider 要"带度量回传"的闭包，
        # 收集本轮（含审校返工轮）的 call_metrics，包成"1 块"的文档级度量，
        # 截断信号才能进看板（F4）。不隐式替换调用方传入的 extractor：
        # 换实现必须由调用方显式请求（nit4）。
        # 回填信号必须落回【调用方的】sink：短文档路径若另开一个中间 dict，
        # 抽取闭包写的是调用方那份，最后再用中间值覆盖就会把信号冲成 None。
        short_backfill: dict = backfill_sink if backfill_sink is not None else {}
        short_extractor = extractor
        if metrics_sink is not None:
            provider_extract = getattr(provider, "extract", None)
            if provider_extract is not None:
                params = inspect.signature(provider_extract).parameters
                if "metrics_sink" in params:
                    extra_kwargs: dict = {}
                    # 回填信号同样不能因为换短文档闭包而丢（03 特性）。
                    if "backfill_sink" in params:
                        extra_kwargs["backfill_sink"] = short_backfill
                    short_extractor = await provider_extract(
                        domain_code, usage_sink=usage,
                        metrics_sink=metrics_sink, **extra_kwargs)

        def _short_document_metrics() -> DocumentMetrics | None:
            if not metrics_sink:
                return None
            merged = merge_call_metrics(metrics_sink)
            # nit5：provider 闭包里的 Document 只有占位 id（<domain>-dyn），
            # 短文档明细的 chunk_id 要用调用方传进来的真实 document_id。
            if document_id and merged.chunk_id != document_id:
                merged = replace(merged, chunk_id=document_id)
            dm = DocumentMetrics(
                document_id=document_id or "chunked", n_chunks=1)
            dm.add(merged)
            return dm

        try:
            reviewed = await run_extraction_team(
                text, extractor=short_extractor, max_iters=max_iters,
                # 短文档同样要让规则审校消费回填信号（命中率/歧义）
                backfill_sink=short_backfill)
        except ExtractionError as exc:
            # S1：短文档也会永久失败（三次尝试后抛错），provider 闭包已把
            # 失败块真实的 token/截断信号放进 metrics_sink；把这块度量挂回
            # 异常，API/作业的失败看板行才不会 token=0、truncation_signals=0。
            dm = _short_document_metrics()
            if dm is not None:
                exc.document_metrics = dm
            raise
        reviewed.document_metrics = _short_document_metrics()
        # 03 的回填信号：闭包直接写调用方 sink，这里再兜一层（闭包未写时用
        # 审校路径拿到的值回填），保证返回值与调用方 sink 一致、不互相覆盖。
        if reviewed.backfill is None:
            reviewed.backfill = short_backfill.get("backfill")
        if backfill_sink is not None:
            backfill_sink["backfill"] = reviewed.backfill
        return reviewed

    real, _domain = await provider.get_extractor(domain_code)
    from ..extraction.pipeline import ChunkedExtractionPipeline

    pipeline = ChunkedExtractionPipeline(
        real, concurrency=chunk_concurrency, result_cache=result_cache)
    chunks = chunker.chunk_text(text)
    # 只加观测：某块重试耗尽时仍像改动前一样整篇抛错（不把失败吞成部分结果）。
    # pipeline 会把文档级度量挂到异常上，截断/返工信号不会随异常蒸发。
    output = await pipeline.run(
        _Doc(id=document_id or "chunked", text=text, source="chunked",
             domain_code=domain_code),
        chunks)
    if usage is not None:
        usage.add(output.usage)

    # 回填信号既进 ReviewedExtraction，也写给上层 sink（API/作业复用同一份）。
    if backfill_sink is not None and output.backfill is not None:
        backfill_sink["backfill"] = output.backfill
    review = rule_review(text, output.result, backfill=output.backfill)
    review.iterations = 1
    return ReviewedExtraction(
        result=output.result,
        accepted=(review.verdict == ReviewVerdict.PASS),
        review=review,
        history=[review],
        document_metrics=output.document_metrics,
        backfill=output.backfill,
    )
