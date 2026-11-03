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
      --repeat 3 --temperature 0 \
      --json-out docs/evidence/03-r3-stress-ab.json

默认 ``--domain stress``：在 general 类型表上并入 02 gold 的规范谓词，
让关系轴 baseline 可判（非零）。每轮 full/slim 各跑一遍，共 ``--repeat``
轮，证据里同时给逐轮明细与均值/最小值；``git_sha/git_describe/git_dirty``
在开跑时采集，避免用未提交工作树的旧 sha 冒充。脚本会真实调用模型；
没有密钥时直接失败，不会用规则模式冒充。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

from moray.config import get_settings
from moray.evaluation.metrics import macro_average
from moray.evaluation.stress import (
    aggregate_cases,
    evaluate_case,
    load_stress_dir,
    relation_recall_for_predicates,
)
from moray.extraction import Chunker, Document, Extractor
from moray.extraction.pipeline import ChunkedExtractionPipeline
from moray.extraction.quality import verify_spans
from moray.schema import GENERAL_DOMAIN, DomainSpec

# 02 压力语料 gold 的规范谓词。验收 8 必须在一个「模型有机会答对」的域里做
# A/B：general 域的谓词表不含它们，full 与 slim 的关系 F1 都会接近 0，
# 「不下降」就成了 0 对 0 的空转结论（r4 S2）。
STRESS_RELATION_PREDICATES = ("供职于", "户籍在", "注册地位于", "涉案关联")


def _stress_domain() -> DomainSpec:
    """把 02 压力语料的谓词并进 general 的类型表，供验收 8 的 A/B 使用。

    只扩 relation_predicates，不改实体类型/属性提示；两种模式用同一个域，
    baseline 因此是可判（非零）的关系抽取任务。
    """
    predicates = tuple(GENERAL_DOMAIN.relation_predicates) + tuple(
        p for p in STRESS_RELATION_PREDICATES
        if p not in GENERAL_DOMAIN.relation_predicates)
    return DomainSpec(
        code="stress_eval",
        name=f"{GENERAL_DOMAIN.name}（02 压力语料评测）",
        types=GENERAL_DOMAIN.types,
        relation_predicates=predicates,
        extra_instruction=GENERAL_DOMAIN.extra_instruction,
    )


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
    predicate_recall: dict


def _git_identity() -> dict:
    """记录证据对应的提交；工作树脏时也如实标注，避免再出现旧 sha 冒充。"""
    def _run(*args: str) -> str:
        try:
            return subprocess.check_output(
                ["git", *args], text=True, stderr=subprocess.DEVNULL).strip()
        except Exception:  # noqa: BLE001 取证脚本，拿不到就留空
            return ""

    status = _run("status", "--porcelain")
    return {
        "git_sha": _run("rev-parse", "HEAD"),
        "git_describe": _run("describe", "--always", "--dirty"),
        "git_dirty": bool(status),
        "git_status_porcelain": status,
    }


def _model_identity() -> dict:
    s = get_settings()
    return {
        "provider": s.get("provider"),
        "chat_model": s.get("chat_model"),
        # 只记 host，避免把密钥/查询串写进证据
        "base_url_host": urlparse(s.get("base_url") or "").netloc,
        # 采样参数：temperature 显式固定；DeepSeek 的模型参数不暴露 seed，
        # 无法固定随机种子，只能多轮重复报均值/最小值（r4 N3）。
        "temperature": s.get("temperature"),
        "seed": None,
        "seed_note": "provider 的 ChatModel.Parameters 不暴露 seed；用 repeat 采样统计",
    }


async def _run_mode(mode: str, cases, chunks_by_case, *, concurrency: int,
                    domain: DomainSpec) -> ModeResult:
    extractor = Extractor(
        domain, max_retries=2, local_backfill=(mode == "slim"))
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
                       domain_code=domain.code)
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
        # 关系轴可判性：只报 02 gold 的谓词召回，看 baseline 是否非零。
        predicate_recall=relation_recall_for_predicates(
            evals, set(STRESS_RELATION_PREDICATES)),
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


def _mean(values: list) -> float | None:
    vals = [v for v in values if isinstance(v, (int, float))]
    return round(sum(vals) / len(vals), 4) if vals else None


def _min(values: list) -> float | None:
    vals = [v for v in values if isinstance(v, (int, float))]
    return min(vals) if vals else None


def _mode_payload(result: ModeResult) -> dict:
    return {
        "cases": result.cases, "macro": result.macro, "micro": result.micro,
        "usage": result.usage, "chunk_summary": result.chunk_summary,
        "predicate_recall": result.predicate_recall,
    }


def _aggregate_mode(repeats: list[dict], mode: str) -> dict:
    """多轮重复的均值/最小值：单次 3 样本的 macro 结论太脆（r4 N3）。"""
    payloads = [r[mode] for r in repeats]
    macro = {}
    for family in ("entity_relaxed", "entity_strict", "relation"):
        f1s = [p["macro"].get(family, {}).get("f1", 0.0) for p in payloads]
        macro[family] = {
            "f1_mean": _mean(f1s), "f1_min": _min(f1s),
            "f1_max": max(f1s) if f1s else None,
        }
    recalls = [p.get("predicate_recall", {}).get("recall", 0.0)
               for p in payloads]
    names = [c["name"] for c in payloads[0]["cases"]]
    case_rows = []
    for name in names:
        rows = [next(c for c in p["cases"] if c["name"] == name)
                for p in payloads]
        case_rows.append({
            "name": name,
            "entity_relaxed_f1_mean": _mean(
                [r["entity_relaxed_f1"] for r in rows]),
            "entity_relaxed_f1_min": _min(
                [r["entity_relaxed_f1"] for r in rows]),
            "entity_strict_f1_mean": _mean(
                [r["entity_strict_f1"] for r in rows]),
            "relation_f1_mean": _mean([r["relation_f1"] for r in rows]),
            "span_ratio_min": _min([r["span_ratio"] for r in rows]),
            "span_ratio_mean": _mean([r["span_ratio"] for r in rows]),
        })
    chunk_keys = ("mean_output_tokens_per_chunk", "truncated_chunks",
                  "truncation_signals", "output_tokens", "input_tokens",
                  "chunk_calls")
    return {
        "macro": macro,
        "predicate_recall": {
            "recall_mean": _mean(recalls), "recall_min": _min(recalls),
            "recall_max": max(recalls) if recalls else None,
            "predicates": list(STRESS_RELATION_PREDICATES),
        },
        "cases": case_rows,
        "chunk_summary_mean": {
            key: _mean([p["chunk_summary"].get(key, 0) for p in payloads])
            for key in chunk_keys
        },
        "usage_mean": {
            key: _mean([p["usage"].get(key, 0) for p in payloads])
            for key in payloads[0]["usage"]
        },
    }


def _aggregate_delta(deltas: list[dict]) -> dict:
    """多轮 delta 的均值；截断类指标取最坏值（max）而不是平均。"""
    keys = sorted({key for d in deltas for key in d})
    out: dict = {}
    for key in keys:
        values = [d.get(key) for d in deltas
                  if isinstance(d.get(key), (int, float))
                  and not isinstance(d.get(key), bool)]
        if not values:
            out[key] = None
        elif key.startswith("truncat"):
            out[key] = max(values)
        else:
            out[key] = _mean(values)
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gold", default="data/gold/stress")
    ap.add_argument("--env-file", default="")
    ap.add_argument("--max-units", type=int, default=1500)
    ap.add_argument("--overlap-units", type=int, default=200)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--repeat", type=int, default=3,
                    help="重复轮数（≥3 才够报均值/最小值）")
    ap.add_argument("--temperature", type=float, default=0.0,
                    help="显式固定采样温度；传负数表示不设置（provider 默认）")
    ap.add_argument("--domain", choices=("stress", "general"), default="stress",
                    help="stress=含 02 gold 谓词的评测域（可判 baseline）")
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    if args.env_file:
        load_dotenv(args.env_file, override=False)
    if args.temperature >= 0:
        os.environ["LLM_TEMPERATURE"] = str(args.temperature)
    else:
        os.environ.pop("LLM_TEMPERATURE", None)
    get_settings.cache_clear()      # env 变更后重读，保证证据里的参数是实际值

    cases = load_stress_dir(args.gold)
    if args.limit:
        cases = cases[:args.limit]
    if not cases:
        print(f"没有压力语料：{args.gold}", file=sys.stderr)
        return 2

    domain = _stress_domain() if args.domain == "stress" else GENERAL_DOMAIN
    chunks_by_case = {
        c.name: Chunker(args.max_units, args.overlap_units).chunk_text(c.text)
        for c in cases
    }
    git_identity = _git_identity()
    repeats: list[dict] = []
    chunk_details: list[dict] = []
    for i in range(max(1, args.repeat)):
        full = await _run_mode("full", cases, chunks_by_case,
                               concurrency=args.concurrency, domain=domain)
        slim = await _run_mode("slim", cases, chunks_by_case,
                               concurrency=args.concurrency, domain=domain)
        if i == 0:
            chunk_details = full.chunk_details + slim.chunk_details
        repeats.append({
            "repeat": i + 1,
            "full": _mode_payload(full),
            "slim": _mode_payload(slim),
            "delta": _delta(full, slim),
        })

    first = repeats[0]
    payload = {
        "experiment": "03 验收 8/9：full vs slim 真实 LLM A/B（r4 重跑）",
        "baseline_commit": "262319b",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        **git_identity,
        "model": _model_identity(),
        "sampling": {
            "repeat": len(repeats),
            "temperature": args.temperature if args.temperature >= 0 else None,
            "seed": None,
            "note": ("temperature 显式固定；provider 参数不暴露 seed，"
                     "因此用多轮重复报告均值/最小值，而不是声称单次可复现"),
        },
        "domain": {
            "code": domain.code,
            "relation_predicates": list(domain.relation_predicates),
            "source": ("general + 02 stress predicates" if args.domain == "stress"
                       else "general"),
        },
        "corpus": {
            "path": args.gold,
            "cases": [c.name for c in cases],
            "seeds": [c.seed for c in cases],
            "total_chars": sum(len(c.text) for c in cases),
        },
        "chunking": {"max_units": args.max_units,
                     "overlap_units": args.overlap_units},
        "cost_note": "只记录真实 token；金额以 provider 账单为准，脚本不估算价格",
        # 顶层 full/slim 是第 1 轮明细；多轮统计见 aggregate。
        "full": first["full"],
        "slim": first["slim"],
        "repeats": repeats,
        "aggregate": {
            "full": _aggregate_mode(repeats, "full"),
            "slim": _aggregate_mode(repeats, "slim"),
            "delta": _aggregate_delta([r["delta"] for r in repeats]),
        },
        # 兼容既有证据读取方：delta 是各轮 delta 的均值（截断取最坏值）。
        "delta": _aggregate_delta([r["delta"] for r in repeats]),
        "chunk_details": chunk_details,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.json_out:
        Path(args.json_out).write_text(text, encoding="utf-8")
        print(f"wrote {args.json_out}")
    print(json.dumps({
        "aggregate": payload["aggregate"],
        "git_sha": payload["git_sha"],
        "git_dirty": payload["git_dirty"],
        "model": payload["model"],
        "corpus_sha256": hashlib.sha256(
            "".join(c.text for c in cases).encode()).hexdigest()[:16],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
