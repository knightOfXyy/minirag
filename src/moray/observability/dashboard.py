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

from dataclasses import dataclass
from pathlib import Path

_SCHEMA_SQL = Path(__file__).with_name("schema.sql")

pipeline_runs_schema = _SCHEMA_SQL.read_text(encoding="utf-8")


@dataclass
class RunRecorder:
    """组装一次文档运行的指标（像 L06 那样逐步填充，最后一次性落库）。"""

    document_id: str
    domain_code: str = "general"
    # 这条 run 从哪条路径来：api（实时 /extract）/ job（批量作业）。
    # 此前只有批量写 pipeline_runs；若不区分，API 流量会悄悄改变历史看板口径。
    source: str = "job"
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

    # ---- 工单 01：分块级度量（文档级聚合）--------------------------------
    # 整篇旧闭环（无分块）时 chunk_calls=0；分块聚合按该列自行判断是否有效。
    chunk_calls: int = 0              # 分块 LLM 调用块数
    total_attempts: int = 0           # 含校验返工的实际尝试总次数
    truncated_chunks: int = 0         # 最终输出被长度上限截断的块数
    truncation_signals: int = 0       # 出现过截断信号的块数（含救回）
    reworked_chunks: int = 0          # 发生校验返工的块数
    rework_rate: float = 0.0          # reworked_chunks / chunk_calls
    wait_ms: int = 0                  # 并发/配额排队总耗时
    # 生产默认非流式时网关拿不到首 token 时间点：None=不可测，不是 0。
    prefill_ms: int | None = None     # 首 token（prefill）总耗时
    decode_ms: int | None = None      # 解码总耗时
    # 分块明细 JSON（每块一条 ChunkCallMetrics），供下钻排查，不做 SQL 聚合
    chunk_metrics_json: list | None = None

    @staticmethod
    def _chunk_fields(dm) -> dict:
        """把 DocumentMetrics 映射成看板列；None/未测到如实保持 None。"""
        s = dm.summary() if dm is not None else {}

        def _ms(key):
            value = s.get(key)
            return None if value is None else int(value * 1000)

        return {
            "chunk_calls": int(s.get("chunk_calls", 0)),
            "total_attempts": int(s.get("total_attempts", 0)),
            "truncated_chunks": int(s.get("truncated_chunks", 0)),
            "truncation_signals": int(s.get("truncation_signals", 0)),
            "reworked_chunks": int(s.get("reworked_chunks", 0)),
            "rework_rate": float(s.get("rework_rate", 0.0)),
            "wait_ms": (int(s["wait_s"] * 1000)
                        if s.get("wait_s") is not None else 0),
            "prefill_ms": _ms("prefill_s"),
            "decode_ms": _ms("decode_s"),
            "chunk_metrics_json": (
                [c.to_dict() for c in dm.chunks] if dm is not None else None),
        }

    @classmethod
    def from_pipeline(
        cls,
        *,
        document_id: str,
        reviewed,
        ingest_report,
        span_ratio: float | None = None,
        domain_code: str = "general",
        source: str = "job",
        input_tokens: int = 0,
        output_tokens: int = 0,
        cached_input_tokens: int = 0,
        latency_ms: int = 0,
        model_name: str = "",
        trace_id: str | None = None,
    ) -> RunRecorder:
        """从 L10 ReviewedExtraction + L08/09 IngestReport 便捷构造。"""
        dm = getattr(reviewed, "document_metrics", None)
        chunk_fields = cls._chunk_fields(dm)
        return cls(
            document_id=document_id,
            domain_code=domain_code,
            source=source,
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
            **chunk_fields,
        )

    @classmethod
    def from_failure(
        cls,
        *,
        document_id: str,
        domain_code: str = "general",
        document_metrics=None,
        error: str = "",
        source: str = "job",
        latency_ms: int = 0,
        model_name: str = "",
    ) -> RunRecorder:
        """抽取整篇失败时仍落一行：截断/返工信号不能随异常蒸发（F5）。"""
        return cls(
            document_id=document_id,
            domain_code=domain_code,
            source=source,
            review_passed=False,
            open_issues=1 if error else 0,
            latency_ms=latency_ms,
            model_name=model_name,
            **cls._chunk_fields(document_metrics),
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
              (document_id, domain_code, source, n_entities, n_relations,
               span_ratio, review_passed, review_iters, open_issues,
               n_auto_merged, n_queued, n_created,
               input_tokens, output_tokens, cached_input_tokens,
               latency_ms, model_name, trace_id,
               chunk_calls, total_attempts, truncated_chunks, truncation_signals,
               reworked_chunks, rework_rate,
               wait_ms, prefill_ms, decode_ms, chunk_metrics)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,
                    $18,$19,$20,$21,$22,$23,$24,$25,$26,$27,$28)
            RETURNING id
            """,
            r.document_id, r.domain_code, r.source,
            r.n_entities, r.n_relations, r.span_ratio,
            r.review_passed, r.review_iters, r.open_issues,
            r.n_auto_merged, r.n_queued, r.n_created,
            r.input_tokens, r.output_tokens, r.cached_input_tokens,
            r.latency_ms, r.model_name, r.trace_id,
            r.chunk_calls, r.total_attempts, r.truncated_chunks,
            r.truncation_signals, r.reworked_chunks, r.rework_rate,
            r.wait_ms, r.prefill_ms, r.decode_ms,
            # jsonb 由连接 codec 自动编解码（见 kb.store._init_connection）
            r.chunk_metrics_json,
        )
        return row["id"]

    async def summary(
        self, domain_code: str | None = None, *, source: str | None = None,
    ) -> dict:
        """聚合看板：质量、返工、自动化率、成本，全部一条 SQL 算出。

        source 可按来源（api/job）过滤；不传则维持历史口径（全部行）。
        """
        conditions: list[str] = []
        params: list = []
        if domain_code:
            params.append(domain_code)
            conditions.append(f"domain_code=${len(params)}")
        if source:
            params.append(source)
            conditions.append(f"source=${len(params)}")
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
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
              coalesce(avg(latency_ms),0)                            AS avg_latency_ms,
              -- 工单 01：分块级度量聚合（chunk_calls=0 的整篇旧闭环自然不计入）
              coalesce(sum(chunk_calls),0)                           AS chunk_calls,
              coalesce(sum(total_attempts),0)                        AS total_attempts,
              coalesce(sum(truncated_chunks),0)                      AS truncated_chunks,
              coalesce(sum(truncation_signals),0)                    AS truncation_signals,
              coalesce(sum(reworked_chunks),0)                       AS reworked_chunks,
              coalesce(sum(wait_ms),0)                               AS wait_ms,
              coalesce(sum(prefill_ms),0)                            AS prefill_ms,
              coalesce(sum(decode_ms),0)                             AS decode_ms,
              -- 真实测到分段耗时的行数：0 表示这些 run 全是非流式、不可测
              count(prefill_ms)                                      AS prefill_measured_runs,
              count(decode_ms)                                       AS decode_measured_runs
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
        # 分块返工率：返工块 / 分块调用块（只在走过分块路径的 run 上有意义）
        chunk_calls = d["chunk_calls"] or 0
        d["chunk_rework_rate"] = (
            round((d["reworked_chunks"] or 0) / chunk_calls, 6)
            if chunk_calls else 0.0)
        # 分段耗时：没有任何一行测到时返回 None（不可测），不拿 0 冒充。
        if not d.get("prefill_measured_runs"):
            d["prefill_ms"] = None
        if not d.get("decode_measured_runs"):
            d["decode_ms"] = None
        # 数值规整
        for k in ("avg_span_ratio", "avg_review_iters", "avg_latency_ms"):
            d[k] = round(float(d[k]), 3)
        return d

    async def recent(self, limit: int = 10) -> list[dict]:
        rows = await self.pool.fetch(
            "SELECT document_id, domain_code, source, n_entities, n_relations, "
            "span_ratio, review_passed, review_iters, n_auto_merged, n_queued, "
            "input_tokens, output_tokens, cached_input_tokens, "
            "latency_ms, created_at, "
            "chunk_calls, truncated_chunks, truncation_signals, rework_rate, "
            "prefill_ms, decode_ms "
            "FROM pipeline_runs ORDER BY id DESC LIMIT $1", limit)
        return [dict(r) for r in rows]
