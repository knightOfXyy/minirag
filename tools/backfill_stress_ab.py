"""工单 03 验收 8/9：02 压力语料上的真实 LLM full vs slim A/B。

同一份 02 压力语料、同一个模型、同一组分块预算，分别跑：

* ``full``：全量 schema（模型自报 mentions/偏移/置信度/证据）；
* ``slim``：瘦身 schema + 本地确定性回填。

同时产出两类证据：

1. 验收 8：实体 macro/strict F1、关系 F1、span 一致率（预测 span 对原文切片）；
2. 验收 9：逐块 output token（工单 01 的 ``DocumentMetrics``）与截断块数。

用法（密钥从 --env-file 读取，不写进仓库）::

    PYTHONPATH=src python tools/backfill_stress_ab.py \
      --gold data/gold/stress --env-file ../moray/.env \
      --json-out docs/evidence/03-r3-stress-ab.json

脚本会真实调用模型；没有密钥时直接失败，不会用规则模式冒充。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

from moray.evaluation.metrics import macro_average
from moray.evaluation.stress import (
    aggregate_cases,
    evaluate_case,
    load_stress_dir,
)
from moray.extraction import Chunker, Document, Extractor
from moray.extraction.pipeline import ChunkedExtractionPipeline
from moray.extraction.quality import verify_spans


@dataclass
class ModeResult:
    mode: str
    cases: list[dict]
    evaluations: list
    macro: dict
    micro: dict
    usage: dict
    chunk_summary: dict
    chunk_details: list[dict]
    span_ratios: list[float]


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True).strip()
    except Exception:  # noqa: BLE001 取证脚本，拿不到就留空
        return ""


def _model_identity() -> dict:
    from moray.config import get_settings

    s = get_settings()
    return {
        "provider": s.get("provider"),
        "chat_model": s.get("chat_model"),
        # 只记 host，避免把密钥/查询串写进证据
        "base_url_host": urlparse(s.get("base_url") or "").netloc,
    }


async def _run_mode(mode: str, cases, chunks_by_case, *,
                    concurrency: int) -> ModeResult:
    extractor = Extractor(
        "general", max_retries=2, local_backfill=(mode == "slim"))
    pipeline = ChunkedExtractionPipeline(extractor, concurrency=concurrency)

    evals, case_rows, chunk_details = [], [], []
    usage = {"input_tokens": 0, "output_tokens": 0,
             "cached_input_tokens": 0, "cache_hits": 0}
    totals = {"n_chunks": 0, "chunk_calls": 0, "output_tokens": 0,
              "input_tokens": 0, "truncated_chunks": 0,
              "truncation_signals": 0, "reworked_chunks": 0,
              "failed_chunks": 0}

    for case in cases:
        doc = Document(id=case.name, text=case.text, source="stress-gold",
                       domain_code="general")
        out = await pipeline.run(
            doc, chunks_by_case[case.name], raise_on_chunk_error=False)

        u = out.usage.model_dump()
        for key in usage:
            usage[key] += int(u.get(key, 0) or 0)

        metrics = out.document_metrics
        summary = metrics.summary() if metrics is not None else {}
        for key in ("n_chunks", "chunk_calls", "output_tokens", "input_tokens",
                    "truncated_chunks", "truncation_signals",
                    "reworked_chunks"):
            totals[key] += int(summary.get(key, 0) or 0)
        totals["failed_chunks"] += sum(
            1 for c in (metrics.chunks if metrics else []) if c.error)

        span = verify_spans(out.result, case.text)
        ev = evaluate_case(case.name, case.seed, out.result, case.gold)
        evals.append(ev)
        case_rows.append({
            "name": case.name,
            "seed": case.seed,
            "n_chunks": len(chunks_by_case[case.name]),
            "failed_chunks": sum(
                1 for c in (metrics.chunks if metrics else []) if c.error),
            "entity_relaxed_f1": ev.report.entity_relaxed.f1,
            "entity_strict_f1": ev.report.entity_strict.f1,
            "relation_f1": ev.report.relation.f1,
            "span_checked": span.total,
            "span_ratio": round(span.ratio, 4) if span.total else None,
            "backfill_block_confidence": (
                out.backfill.get("block_confidence")
                if out.backfill is not None else None),
            "output_tokens": int(summary.get("output_tokens", 0) or 0),
            "chunk_calls": int(summary.get("chunk_calls", 0) or 0),
            "truncated_chunks": int(summary.get("truncated_chunks", 0) or 0),
        })
        for c in (metrics.chunks if metrics else []):
            chunk_details.append({
                "mode": mode, "case": case.name, **c.to_dict(),
            })

    checked = [r for r in case_rows if r["span_ratio"] is not None]
    chunk_summary = dict(totals)
    if totals["chunk_calls"]:
        chunk_summary["mean_output_tokens_per_chunk"] = round(
            totals["output_tokens"] / totals["chunk_calls"], 2)
    return ModeResult(
        mode=mode,
        cases=case_rows,
        evaluations=evals,
        macro=macro_average([e.report for e in evals]),
        micro=aggregate_cases(evals),
        usage=usage,
        chunk_summary=chunk_summary,
        chunk_details=chunk_details,
        span_ratios=[r["span_ratio"] for r in checked],
    )


def _delta(before: ModeResult, after: ModeResult) -> dict:
    e_before = before.macro.get("entity_relaxed", {}).get("f1", 0.0)
    e_after = after.macro.get("entity_relaxed", {}).get("f1", 0.0)
    r_before = before.macro.get("relation", {}).get("f1", 0.0)
    r_after = after.macro.get("relation", {}).get("f1", 0.0)
    tok_before = before.chunk_summary.get("mean_output_tokens_per_chunk", 0.0)
    tok_after = after.chunk_summary.get("mean_output_tokens_per_chunk", 0.0)
    return {
        "entity_macro_f1_before": e_before,
        "entity_macro_f1_after": e_after,
        "entity_macro_f1_delta_pt": round((e_after - e_before) * 100, 4),
        "relation_f1_before": r_before,
        "relation_f1_after": r_after,
        "relation_f1_delta_pt": round((r_after - r_before) * 100, 4),
        "span_ratio_min_before": min(before.span_ratios) if before.span_ratios else None,
        "span_ratio_min_after": min(after.span_ratios) if after.span_ratios else None,
        "span_ratio_mean_before": (
            round(sum(before.span_ratios) / len(before.span_ratios), 4)
            if before.span_ratios else None),
        "span_ratio_mean_after": (
            round(sum(after.span_ratios) / len(after.span_ratios), 4)
            if after.span_ratios else None),
        "mean_output_tokens_per_chunk_before": tok_before,
        "mean_output_tokens_per_chunk_after": tok_after,
        "output_token_drop_pct": (
            round((1 - tok_after / tok_before) * 100, 2)
            if tok_before else None),
        "truncated_chunks_before": before.chunk_summary.get("truncated_chunks", 0),
        "truncated_chunks_after": after.chunk_summary.get("truncated_chunks", 0),
        "truncation_signals_before": before.chunk_summary.get("truncation_signals", 0),
        "truncation_signals_after": after.chunk_summary.get("truncation_signals", 0),
    }


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gold", default="data/gold/stress")
    ap.add_argument("--env-file", default="")
    ap.add_argument("--max-units", type=int, default=1500)
    ap.add_argument("--overlap-units", type=int, default=200)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    if args.env_file:
        load_dotenv(args.env_file, override=False)

    cases = load_stress_dir(args.gold)
    if args.limit:
        cases = cases[:args.limit]
    if not cases:
        print(f"没有压力语料：{args.gold}", file=sys.stderr)
        return 2

    chunks_by_case = {
        c.name: Chunker(args.max_units, args.overlap_units).chunk_text(c.text)
        for c in cases
    }
    full = await _run_mode("full", cases, chunks_by_case,
                           concurrency=args.concurrency)
    slim = await _run_mode("slim", cases, chunks_by_case,
                           concurrency=args.concurrency)
    payload = {
        "baseline_commit": "262319b",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git_sha": _git_sha(),
        "model": _model_identity(),
        "corpus": {
            "path": args.gold,
            "cases": [c.name for c in cases],
            "seeds": [c.seed for c in cases],
            "total_chars": sum(len(c.text) for c in cases),
        },
        "chunking": {"max_units": args.max_units,
                     "overlap_units": args.overlap_units},
        "cost_note": "只记录真实 token；金额以 provider 账单为准，脚本不估算价格",
        "full": {
            "cases": full.cases, "macro": full.macro, "micro": full.micro,
            "usage": full.usage, "chunk_summary": full.chunk_summary,
        },
        "slim": {
            "cases": slim.cases, "macro": slim.macro, "micro": slim.micro,
            "usage": slim.usage, "chunk_summary": slim.chunk_summary,
        },
        "delta": _delta(full, slim),
        "chunk_details": full.chunk_details + slim.chunk_details,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.json_out:
        Path(args.json_out).write_text(text, encoding="utf-8")
        print(f"wrote {args.json_out}")
    print(json.dumps({
        "delta": payload["delta"],
        "full_usage": full.usage,
        "slim_usage": slim.usage,
        "corpus_sha256": hashlib.sha256(
            "".join(c.text for c in cases).encode()).hexdigest()[:16],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
