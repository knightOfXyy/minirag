"""
AgentScope 2.0 中间件实战（L06）
================================

2.0 用 Middleware 取代了 1.x 的 Hook。核心是【洋葱模型】：

    on_reply
      └─ on_reasoning
           └─ on_model_call          ← 最贴近真正 HTTP 调用
      └─ on_acting (每个工具一次)

每一层都能在 next_handler() 之前/之后插入逻辑：之前做计时/改写/拦截，
之后做统计/审计/质量评估。注意：
  · 生成器型钩子（on_reply/on_reasoning/on_acting）必须把事件【继续 yield 出去】，
    否则下游（调用方/控制台）就收不到事件；
  · on_model_call 是非生成器（stream=False 时直接返回 ChatResponse）；
  · 框架以关键字 next_handler= 传参，参数名必须写对。

本模块提供三个企业级中间件：
  1. AuditMiddleware            审计每次模型调用与工具执行
  2. CostAccountingMiddleware   汇总 token 与耗时
  3. ExtractionMetricsMiddleware 从结构化结果算抽取质量分
三者共享同一个 MetricsCollector，最终每条 ExtractionMetrics 就是一次抽取的
"可观测档案"。
"""

from __future__ import annotations

import json
from typing import Callable

from agentscope.message import Msg
from agentscope.middleware import MiddlewareBase

from ..extraction.quality import verify_spans
from .metrics import (
    ExtractionMetrics,
    MetricsCollector,
    ModelCallRecord,
    ToolCallRecord,
)

# 结构化输出内置工具名（见 agentscope.agent._structured_output_tool）
_STRUCTURED_TOOL = "GenerateStructuredOutput"
# 低置信度阈值：低于它的实体计入"需人工复核"
LOW_CONF_THRESHOLD = 0.75


class _RecorderMiddleware(MiddlewareBase):
    """持有 collector + 当前记录的基类，避免三个类重复取记录。"""

    def __init__(self, collector: MetricsCollector) -> None:
        self.collector = collector

    def _rec(self, agent) -> ExtractionMetrics:
        # 所有中间件共享本次 reply 的同一条记录
        return self.collector.current(getattr(agent, "name", "agent"))


class AuditMiddleware(_RecorderMiddleware):
    """审计：记录每次模型调用与每次工具执行（谁、何时、调了什么、成败）。"""

    async def on_model_call(self, agent, input_kwargs, next_handler):
        rec = self._rec(agent)
        import time

        t0 = time.perf_counter()
        record = ModelCallRecord(
            messages_in=len(input_kwargs.get("messages", [])),
            tools_offered=len(input_kwargs.get("tools", [])),
        )
        rec.model_calls.append(record)
        try:
            resp = await next_handler()
            usage = getattr(resp, "usage", None)
            if usage is not None:
                record.input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
                record.output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
                record.cached_input_tokens = int(
                    getattr(usage, "cache_input_tokens", 0) or 0)
            return resp
        except Exception as exc:  # noqa: BLE001 审计要吞不下异常，先记再抛
            record.ok = False
            record.error = repr(exc)
            raise
        finally:
            record.latency_s = time.perf_counter() - t0

    async def on_acting(
        self,
        agent,
        input_kwargs: dict,
        next_handler: Callable,
    ):
        rec = self._rec(agent)
        import time

        tool_call = input_kwargs.get("tool_call")
        name = getattr(tool_call, "name", "unknown")
        t0 = time.perf_counter()
        trec = ToolCallRecord(name=name, ok=True, latency_s=0.0)
        rec.tool_calls.append(trec)
        try:
            async for chunk in next_handler():
                yield chunk
        except Exception as exc:  # noqa: BLE001
            trec.ok = False
            raise
        finally:
            trec.latency_s = time.perf_counter() - t0


class CostAccountingMiddleware(_RecorderMiddleware):
    """成本/耗时：包住整个 reply，负责开启/收尾这一次抽取的共享指标记录。"""

    async def on_reply(self, agent, input_kwargs, next_handler):
        import time

        rec = self.collector.start(getattr(agent, "name", "agent"))
        t0 = time.perf_counter()
        try:
            async for event in next_handler():
                yield event
        finally:
            rec.duration_s = time.perf_counter() - t0
            self.collector.finish()


class ExtractionMetricsMiddleware(_RecorderMiddleware):
    """质量：从最终结构化 Msg 中统计实体/关系数、低置信度、span 一致率与质量分。

    它需要知道原文才能核对 span。通过 agent 上挂载的 _audit_source_text
    传入（抽取服务在调用前设置）；没有原文时跳过 span 核对。
    """

    async def on_reply(self, agent, input_kwargs, next_handler):
        async for event in next_handler():
            # 最终的 assistant Msg 携带 structured_output
            if isinstance(event, Msg) and getattr(event, "structured_output", None):
                rec = self._rec(agent)
                data = event.structured_output or {}
                entities = data.get("entities", [])
                relations = data.get("relations", [])
                rec.n_entities = len(entities)
                rec.n_relations = len(relations)
                # 瘦身后模型不再输出 confidence：缺失 = 未知，不是「低置信」。
                # 未知不应把整批实体误判为需人工复核；确定性块级信号另行透传
                # （ExtractionOutput.backfill），不在这里伪造 0/1。
                confidences = [
                    float(e["confidence"]) for e in entities
                    if e.get("confidence") is not None
                ]
                rec.low_confidence_entities = sum(
                    1 for c in confidences if c < LOW_CONF_THRESHOLD
                )
                rec.known_confidence_entities = len(confidences)

                source = getattr(agent, "_audit_source_text", None)
                if source:
                    # 复用现成的稳定内核模型做核对，避免重复实现
                    from ..schema import ExtractionResult

                    try:
                        result = ExtractionResult.model_validate(data)
                        report = verify_spans(result, source)
                        rec.span_ratio = report.ratio
                        # 瘦身输出没有 mention：用本地确定性回填算「实体在正文
                        # 里有没有出处」，替代模型自报置信度做质量依据。
                        grounding = self._grounding(rec, result, source)
                        rec.quality_score = self._score(rec, report, grounding)
                    except Exception:  # noqa: BLE001 质量计算绝不能影响主流程
                        pass
                else:
                    rec.quality_score = self._score(rec, None)
            yield event

    @staticmethod
    def _grounding(rec: ExtractionMetrics, result, source: str) -> float | None:
        """用确定性本地回填算块级接地率，并写入指标（失败时不影响主流程）。"""
        from ..extraction.local_backfill import backfill_provenance

        try:
            _local, report = backfill_provenance(result, source)
        except Exception:  # noqa: BLE001
            return None
        rec.entities_total = report.entities_total
        rec.entities_grounded = report.entities_grounded
        rec.block_confidence = report.block_confidence
        rec.relation_evidence_ratio = report.relation_evidence_ratio
        rec.ambiguous_mentions = len(report.ambiguous_mentions)
        return rec.block_confidence

    @staticmethod
    def _score(rec: ExtractionMetrics, report, grounding: float | None = None) -> float:
        """一个透明、可解释的启发式质量分（0~1），L15 会换成评测体系。

        构成：出处/接地率 50% + 高置信实体占比 30% + 产出非空 20%。
        有确定性回填信号时用块级接地率（实体是否真在正文里），否则退回
        span 一致率——slim 输出没有 mention，span 检查会空转给满分。
        """
        if grounding is not None:
            span_part = grounding
        else:
            span_part = report.ratio if report is not None else 1.0
        known = rec.known_confidence_entities
        if known:
            conf_part = 1.0 - rec.low_confidence_entities / known
        elif rec.n_entities:
            conf_part = 1.0   # 全部未知：不奖不罚，按中性满分处理
        else:
            conf_part = 0.0
        nonempty_part = 1.0 if rec.n_entities > 0 else 0.0
        return round(0.5 * span_part + 0.3 * conf_part + 0.2 * nonempty_part, 3)


def build_observability_stack(collector: MetricsCollector | None = None):
    """一次性构造一组中间件 + 共享 collector（顺序：成本→审计→质量）。"""
    collector = collector or MetricsCollector()
    return (
        collector,
        [
            CostAccountingMiddleware(collector),
            AuditMiddleware(collector),
            ExtractionMetricsMiddleware(collector),
        ],
    )
