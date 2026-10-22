"""
质量看板（L13 下）
==================

OTEL 追踪给的是技术调用链（Agent/模型/工具、各自耗时）；但抽取系统真正要盯的
是一组【业务指标】，这些 OTEL 不知道：

  · 质量：span 一致率、审校通过率、返工轮数、遗留问题数；
  · 自动化：消歧自动合并了多少、多少不得不转人工（自动率 = 自动/总判定）；
  · 成本：每篇文档 token 与耗时。

做法：每次文档流水线运行结束写一行 pipeline_runs，Dashboard 再做 SQL 聚合，
得到一段时间/一个领域的整体质量画像。指标随抽取/审校/入库结果组装（RunRecorder），
不侵入各模块内部——它们只负责产出结果，看板只读结果计数。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

_SCHEMA_SQL = Path(__file__).with_name("schema.sql")

pipeline_runs_schema = _SCHEMA_SQL.read_text(encoding="utf-8")


@dataclass
class RunRecorder:
    """组装一次文档运行的指标（像 L06 那样逐步填充，最后一次性落库）。"""

    document_id: str
    domain_code: str = "general"
    n_entities: int = 0
    n_relations: int = 0
    span_ratio: float | None = None
    review_passed: bool | None = None
    review_iters: int = 1
    open_issues: int = 0
    n_auto_merged: int = 0
    n_queued: int = 0
    n_created: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0   # 命中提示缓存的前缀（成本折扣 + 优化依据）
    latency_ms: int = 0
    model_name: str = ""
    trace_id: str | None = None

    @classmethod
    def from_pipeline(
        cls,
        *,
        document_id: str,
        reviewed,
        ingest_report,
        span_ratio: float | None = None,
        domain_code: str = "general",
        input_tokens: int = 0,
        output_tokens: int = 0,
        cached_input_tokens: int = 0,
        latency_ms: int = 0,
        model_name: str = "",
        trace_id: str | None = None,
    ) -> "RunRecorder":
        """从 L10 ReviewedExtraction + L08/09 IngestReport 便捷构造。"""
        return cls(
            document_id=document_id,
            domain_code=domain_code,
            n_entities=len(reviewed.result.entities),
            n_relations=len(reviewed.result.relations),
            span_ratio=span_ratio if span_ratio is not None
            else reviewed.review.span_ratio,
            review_passed=reviewed.accepted,
            review_iters=reviewed.review.iterations,
            open_issues=0 if reviewed.accepted else len(reviewed.review.issues),
            n_auto_merged=len(ingest_report.merged),
            n_queued=len(ingest_report.queued),
            n_created=len(ingest_report.created),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_input_tokens,
            latency_ms=latency_ms,
            model_name=model_name,
            trace_id=trace_id,
        )


class Dashboard:
    def __init__(self, pool) -> None:
        self.pool = pool

    async def init_schema(self) -> None:
        await self.pool.execute(pipeline_runs_schema)

    async def record(self, r: RunRecorder) -> int:
        row = await self.pool.fetchrow(
            """
            INSERT INTO pipeline_runs
              (document_id, domain_code, n_entities, n_relations, span_ratio,
               review_passed, review_iters, open_issues,
               n_auto_merged, n_queued, n_created,
               input_tokens, output_tokens, cached_input_tokens,
               latency_ms, model_name, trace_id)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17)
            RETURNING id
            """,
            r.document_id, r.domain_code, r.n_entities, r.n_relations, r.span_ratio,
            r.review_passed, r.review_iters, r.open_issues,
            r.n_auto_merged, r.n_queued, r.n_created,
            r.input_tokens, r.output_tokens, r.cached_input_tokens,
            r.latency_ms, r.model_name, r.trace_id,
        )
        return row["id"]

    async def summary(self, domain_code: str | None = None) -> dict:
        """聚合看板：质量、返工、自动化率、成本，全部一条 SQL 算出。"""
        where = "WHERE domain_code=$1" if domain_code else ""
        params = (domain_code,) if domain_code else ()
        row = await self.pool.fetchrow(
            f"""
            SELECT
              count(*)                                              AS runs,
              count(*) FILTER (WHERE review_passed)                  AS passed,
              coalesce(avg(span_ratio),0)                            AS avg_span_ratio,
              coalesce(avg(review_iters),0)                          AS avg_review_iters,
              coalesce(sum(open_issues),0)                           AS total_open_issues,
              coalesce(sum(n_entities),0)                            AS total_entities,
              coalesce(sum(n_relations),0)                           AS total_relations,
              coalesce(sum(n_auto_merged),0)                         AS auto_merged,
              coalesce(sum(n_queued),0)                              AS queued,
              coalesce(sum(n_created),0)                            AS created,
              coalesce(sum(input_tokens),0)                          AS input_tokens,
              coalesce(sum(output_tokens),0)                         AS output_tokens,
              coalesce(sum(cached_input_tokens),0)                   AS cached_input_tokens,
              coalesce(avg(latency_ms),0)                            AS avg_latency_ms
            FROM pipeline_runs {where}
            """,
            *params,
        )
        d = dict(row)
        # 派生比率
        runs = d.get("runs", 0) or 0
        d["pass_rate"] = round(d["passed"] / runs, 3) if runs else 0.0
        decisions = (d["auto_merged"] or 0) + (d["queued"] or 0)
        d["auto_merge_rate"] = (
            round(d["auto_merged"] / decisions, 3) if decisions else None)
        # 提示缓存命中率：cached / input（0~1）。持续偏低=前缀不稳定或未命中，
        # 是"值不值得做预热/前缀对齐"的直接依据。
        inp = d["input_tokens"] or 0
        cached = d["cached_input_tokens"] or 0
        d["cache_hit_rate"] = round(cached / inp, 3) if inp else 0.0
        # 数值规整
        for k in ("avg_span_ratio", "avg_review_iters", "avg_latency_ms"):
            d[k] = round(float(d[k]), 3)
        return d

    async def recent(self, limit: int = 10) -> list[dict]:
        rows = await self.pool.fetch(
            "SELECT document_id, n_entities, n_relations, span_ratio, "
            "review_passed, review_iters, n_auto_merged, n_queued, "
            "input_tokens, output_tokens, cached_input_tokens, "
            "latency_ms, created_at "
            "FROM pipeline_runs ORDER BY id DESC LIMIT $1", limit)
        return [dict(r) for r in rows]
