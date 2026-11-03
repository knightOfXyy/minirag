"""工单 04：分块预算 1500 vs 3500 的真实 LLM A/B（slim+本地回填口径）。

同一份工单 02 压力语料、同一个模型、同一组采样参数，只改分块预算
（``max_units``），overlap 固定 200 不动，逐篇/逐轮记录：

* 准确率：实体 macro/strict F1、关系 F1、按实体类型 P/R；
* 成本/时延：单文档分块调用数、真实 input/output token、端到端墙钟；
* 护栏：工单 01 的截断块数/截断信号（压力语料 + 一批真实文档）；
* 真实 tokenizer 口径：provider 返回的 ``usage.input_tokens``（含正文+
  系统提示+结构化 schema）的最大单块值，与 ``/models`` 的 context_window
  算安全余量；**不使用**本地 ``estimate_tokens`` 冒充真实 tokenizer；
* 残余：每档预算下跨块关系比例 + 文档级远距首现比例，逐条列出。

用法（密钥从 --env-file 读取，不写进仓库）::

    PYTHONPATH=src python tools/chunk_budget_ab.py \
      --gold data/gold/stress --env-file ../moray/.env \
      --budgets 1500,3500 --overlap-units 200 --repeat 3 \
      --concurrency 6 --temperature 0 \
      --json-out docs/evidence/04-budget-ab.json

没有密钥时直接失败，不会用规则模式冒充真实 LLM 证据。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

from moray.config import get_settings
from moray.evaluation.gold import load_gold_dir
from moray.evaluation.metrics import macro_average
from moray.evaluation.stress import (
    aggregate_cases,
    evaluate_budget_gate,
    evaluate_case,
    load_stress_dir,
    relation_chunk_profile,
)
from moray.extraction import Chunker, Document, Extractor
from moray.extraction.pipeline import ChunkedExtractionPipeline

# 默认 200：工单 04 明确不动 overlap；A/B 两档必须使用同一个值，报告里
# 用配置值和“相邻块实际重叠对数”双证据锁定。
DEFAULT_OVERLAP_UNITS = 200
DEFAULT_BUDGETS = (1500, 3500)


def _git_identity() -> dict:
    def _run(*args: str) -> str:
        try:
            return subprocess.check_output(
                ["git", *args], text=True, stderr=subprocess.DEVNULL).strip()
        except Exception:  # noqa: BLE001 取证脚本拿不到就留空
            return ""

    status = _run("status", "--porcelain", "--untracked-files=no")
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
        "base_url_host": urlparse(s.get("base_url") or "").netloc,
        "temperature": s.get("temperature"),
        "seed": None,
        "seed_note": "provider 的 ChatModel.Parameters 不暴露 seed；用 repeat 采样统计",
    }


def _model_capabilities(timeout: float = 20.0) -> dict:
    """从 provider 的 /models 取上下文窗口与输出上限（真实能力，不硬编码）。"""
    s = get_settings()
    base = (s.get("base_url") or "").rstrip("/")
    model = s.get("chat_model") or ""
    url = f"{base}/models"
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {s.get('api_key')}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 取证失败要让调用方显式处理
        raise RuntimeError(f"无法读取 {url} 的模型能力: {exc!r}") from exc
    for item in payload.get("data", []):
        if item.get("id") == model:
            return {
                "source": url,
                "model_id": model,
                "context_window": item.get("context_window"),
                "max_output_tokens": item.get("max_output_tokens"),
            }
    raise RuntimeError(f"/models 未返回当前模型 {model!r} 的能力")


def _chunk_pair_overlaps(chunks) -> int:
    """相邻块里后块起点早于前块终点 = 实际发生了 overlap。"""
    return sum(1 for a, b in zip(chunks, chunks[1:]) if b.start < a.end)


def _mean(values) -> float | None:
    vals = [v for v in values if isinstance(v, (int, float))]
    return round(sum(vals) / len(vals), 4) if vals else None


def _min(values) -> float | None:
    vals = [v for v in values if isinstance(v, (int, float))]
    return min(vals) if vals else None


def _max(values) -> float | None:
    vals = [v for v in values if isinstance(v, (int, float))]
    return max(vals) if vals else None


async def _run_corpus(
    cases,
    *,
    budget: int,
    overlap: int,
    repeat: int,
    concurrency: int,
    domain,
) -> dict:
    """在压力语料上按一档预算跑 ``repeat`` 轮，返回逐轮明细与聚合。"""
    chunks_by_case = {
        c.name: Chunker(budget, overlap).chunk_text(c.text) for c in cases
    }
    rounds: list[dict] = []
    all_evals = []

    extractor = Extractor(domain, max_retries=2, local_backfill=True)
    pipeline = ChunkedExtractionPipeline(extractor, concurrency=concurrency)

    for r in range(max(1, repeat)):
        rows = []
        evals = []
        for case in cases:
            doc = Document(id=case.name, text=case.text,
                           source="stress-gold", domain_code=domain.code)
            chunks = chunks_by_case[case.name]
            started = time.perf_counter()
            out = await pipeline.run(
                doc, chunks, raise_on_chunk_error=False)
            wall_s = time.perf_counter() - started

            metrics = out.document_metrics
            summary = metrics.summary() if metrics is not None else {}
            chunk_records = list(metrics.chunks) if metrics is not None else []
            ev = evaluate_case(case.name, case.seed, out.result, case.gold)
            evals.append(ev)
            all_evals.append(ev)

            rows.append({
                "name": case.name,
                "seed": case.seed,
                "n_chunks": len(chunks),
                "chunk_calls": int(summary.get("chunk_calls", 0) or 0),
                "input_tokens": int(summary.get("input_tokens", 0) or 0),
                "output_tokens": int(summary.get("output_tokens", 0) or 0),
                "cached_input_tokens": int(
                    summary.get("cached_input_tokens", 0) or 0),
                "truncated_chunks": int(
                    summary.get("truncated_chunks", 0) or 0),
                "truncation_signals": int(
                    summary.get("truncation_signals", 0) or 0),
                "reworked_chunks": int(
                    summary.get("reworked_chunks", 0) or 0),
                "failed_chunks": sum(1 for c in chunk_records if c.error),
                "max_chunk_input_tokens": max(
                    (c.input_tokens for c in chunk_records), default=0),
                "max_chunk_output_tokens": max(
                    (c.output_tokens for c in chunk_records), default=0),
                "max_chunk_units": max((c.n_units for c in chunks), default=0),
                "max_chunk_chars": max(
                    (len(c.text) for c in chunks), default=0),
                "overlap_pairs": _chunk_pair_overlaps(chunks),
                "wall_s": round(wall_s, 3),
                "entity_relaxed_f1": ev.report.entity_relaxed.f1,
                "entity_relaxed_prf": ev.report.entity_relaxed.as_dict(),
                "entity_strict_f1": ev.report.entity_strict.f1,
                "relation_f1": ev.report.relation.f1,
                "relation_prf": ev.report.relation.as_dict(),
            })

        agg = aggregate_cases(evals)
        mac = macro_average([e.report for e in evals])
        rounds.append({
            "round": r + 1,
            "cases": rows,
            "micro": agg,
            "macro": mac,
            "chunk_calls_total": sum(row["chunk_calls"] for row in rows),
            "input_tokens_total": sum(row["input_tokens"] for row in rows),
            "output_tokens_total": sum(row["output_tokens"] for row in rows),
            "truncated_chunks_total": sum(
                row["truncated_chunks"] for row in rows),
            "wall_s_total": round(sum(row["wall_s"] for row in rows), 3),
        })

    def _round_value(round_, key):
        return round_["macro"].get(key, {}).get("f1", 0.0)

    entity_means = [_round_value(r, "entity_relaxed") for r in rounds]
    entity_stricts = [_round_value(r, "entity_strict") for r in rounds]
    relation_means = [_round_value(r, "relation") for r in rounds]
    n_docs = len(cases)
    n_rounds = len(rounds)
    all_rows = [row for r in rounds for row in r["cases"]]
    pooled = aggregate_cases(all_evals)

    return {
        "budget": budget,
        "overlap_units": overlap,
        "repeat": n_rounds,
        "documents": n_docs,
        "rounds": rounds,
        "entity_macro_f1_mean": _mean(entity_means),
        "entity_macro_f1_worst": _min(entity_means),
        "entity_strict_f1_mean": _mean(entity_stricts),
        "relation_f1_mean": _mean(relation_means),
        "relation_f1_worst": _min(relation_means),
        "relation_tp_total": sum(
            row["relation_prf"]["tp"] for row in all_rows),
        "relation_fp_total": sum(
            row["relation_prf"]["fp"] for row in all_rows),
        "relation_fn_total": sum(
            row["relation_prf"]["fn"] for row in all_rows),
        "relation_recall_mean": _mean([
            round(
                row["relation_prf"]["tp"]
                / max(1, row["relation_prf"]["tp"]
                      + row["relation_prf"]["fn"]), 6)
            for row in all_rows]),
        "entity_tp_total": sum(
            row["entity_relaxed_prf"]["tp"] for row in all_rows),
        "entity_fp_total": sum(
            row["entity_relaxed_prf"]["fp"] for row in all_rows),
        "entity_fn_total": sum(
            row["entity_relaxed_prf"]["fn"] for row in all_rows),
        "entity_by_type": pooled["entity_by_type"],
        "micro_pooled": {
            "entity_relaxed": pooled["entity_relaxed"],
            "entity_strict": pooled["entity_strict"],
            "relation": pooled["relation"],
        },
        "chunk_calls_total_mean": _mean(
            [r["chunk_calls_total"] for r in rounds]),
        "chunk_calls_per_document_mean": _mean(
            [row["chunk_calls"] for row in all_rows]),
        "input_tokens_total_mean": _mean(
            [r["input_tokens_total"] for r in rounds]),
        "output_tokens_total_mean": _mean(
            [r["output_tokens_total"] for r in rounds]),
        "input_tokens_per_document_mean": _mean(
            [row["input_tokens"] for row in all_rows]),
        "output_tokens_per_document_mean": _mean(
            [row["output_tokens"] for row in all_rows]),
        "output_tokens_per_chunk_mean": (
            round(
                sum(row["output_tokens"] for row in all_rows)
                / max(1, sum(row["chunk_calls"] for row in all_rows)), 2)),
        "wall_s_per_document_mean": _mean(
            [row["wall_s"] for row in all_rows]),
        "wall_s_total_mean": _mean([r["wall_s_total"] for r in rounds]),
        "truncated_chunks_max": max(
            (r["truncated_chunks_total"] for r in rounds), default=0),
        "truncation_signals_max": max(
            (sum(row["truncation_signals"] for row in r["cases"])
             for r in rounds), default=0),
        "failed_chunks_max": max(
            (sum(row["failed_chunks"] for row in r["cases"])
             for r in rounds), default=0),
        "max_chunk_input_tokens": max(
            (row["max_chunk_input_tokens"] for row in all_rows), default=0),
        "max_chunk_output_tokens": max(
            (row["max_chunk_output_tokens"] for row in all_rows), default=0),
        "max_chunk_units": max(
            (row["max_chunk_units"] for row in all_rows), default=0),
        "max_chunk_chars": max(
            (row["max_chunk_chars"] for row in all_rows), default=0),
        "overlap_pairs_min": min(
            (row["overlap_pairs"] for row in all_rows), default=0),
    }


def _load_real_docs(gold_dir: str, samples_dir: str) -> list[Document]:
    """真实文档批次：基线黄金集干净正文 + samples 原始文本。"""
    docs: list[Document] = []
    gold_path = Path(gold_dir)
    if gold_path.exists():
        for case in load_gold_dir(gold_path):
            docs.append(Document(
                id=f"gold:{case.doc_id}", text=case.clean_text,
                source="gold", domain_code="general"))
    samples_path = Path(samples_dir)
    if samples_path.exists():
        for path in sorted(samples_path.rglob("*.txt")):
            text = path.read_text(encoding="utf-8")
            if not text.strip():
                continue
            docs.append(Document(
                id=f"sample:{path.relative_to(samples_path)}", text=text,
                source="sample", domain_code="general"))
    return docs


async def _run_real_docs(
    docs: list[Document], *, budget: int, overlap: int,
    concurrency: int, domain,
) -> dict:
    """真实文档批次只跑一轮：验截断护栏与最大单块真实 token。"""
    if not docs:
        return {"documents": 0, "rows": [], "truncated_chunks_total": 0,
                "max_chunk_input_tokens": 0, "max_chunk_output_tokens": 0}
    extractor = Extractor(domain, max_retries=2, local_backfill=True)
    pipeline = ChunkedExtractionPipeline(extractor, concurrency=concurrency)
    rows = []
    for doc in docs:
        chunks = Chunker(budget, overlap).chunk_text(doc.text)
        started = time.perf_counter()
        out = await pipeline.run(doc, chunks, raise_on_chunk_error=False)
        wall_s = time.perf_counter() - started
        metrics = out.document_metrics
        summary = metrics.summary() if metrics is not None else {}
        records = list(metrics.chunks) if metrics is not None else []
        rows.append({
            "id": doc.id,
            "n_chunks": len(chunks),
            "chunk_calls": int(summary.get("chunk_calls", 0) or 0),
            "input_tokens": int(summary.get("input_tokens", 0) or 0),
            "output_tokens": int(summary.get("output_tokens", 0) or 0),
            "truncated_chunks": int(
                summary.get("truncated_chunks", 0) or 0),
            "truncation_signals": int(
                summary.get("truncation_signals", 0) or 0),
            "failed_chunks": sum(1 for c in records if c.error),
            "max_chunk_input_tokens": max(
                (c.input_tokens for c in records), default=0),
            "max_chunk_output_tokens": max(
                (c.output_tokens for c in records), default=0),
            "max_chunk_units": max((c.n_units for c in chunks), default=0),
            "wall_s": round(wall_s, 3),
        })
    return {
        "documents": len(rows),
        "rows": rows,
        "truncated_chunks_total": sum(r["truncated_chunks"] for r in rows),
        "truncation_signals_total": sum(
            r["truncation_signals"] for r in rows),
        "failed_chunks_total": sum(r["failed_chunks"] for r in rows),
        "max_chunk_input_tokens": max(
            (r["max_chunk_input_tokens"] for r in rows), default=0),
        "max_chunk_output_tokens": max(
            (r["max_chunk_output_tokens"] for r in rows), default=0),
        "chunk_calls_total": sum(r["chunk_calls"] for r in rows),
    }


def _safety_margin(budget_result: dict, real_docs: dict, capabilities: dict) -> dict:
    """真实 tokenizer 口径的上下文余量（input 含正文+系统提示+schema）。"""
    context = capabilities.get("context_window")
    output_cap = capabilities.get("max_output_tokens")
    max_input = max(
        int(budget_result.get("max_chunk_input_tokens") or 0),
        int(real_docs.get("max_chunk_input_tokens") or 0),
    )
    max_output = max(
        int(budget_result.get("max_chunk_output_tokens") or 0),
        int(real_docs.get("max_chunk_output_tokens") or 0),
    )
    out: dict = {
        "source": "provider usage.input_tokens (real tokenizer)",
        "note": ("input_tokens 是 provider 对完整请求的真实计数，已包含"
                 "正文 + 系统提示 + 结构化 schema；不是 estimate_tokens。"
                 "单块 token 为多次尝试之和（保守上界）。"),
        "max_chunk_input_tokens": max_input,
        "max_chunk_output_tokens": max_output,
        "context_window": context,
        "max_output_tokens": output_cap,
    }
    if context:
        out["input_margin_tokens"] = context - max_input
        out["input_margin_pct"] = round(
            (context - max_input) / context * 100, 4)
        out["input_plus_output_margin_tokens"] = context - max_input - max_output
        out["input_plus_output_margin_pct"] = round(
            (context - max_input - max_output) / context * 100, 4)
        out["within_context"] = (max_input + max_output) <= context
    if output_cap:
        out["output_cap_margin_tokens"] = output_cap - max_output
    return out


def _write_report(path: str | Path, payload: dict) -> None:
    Path(path).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gold", default="data/gold/stress")
    ap.add_argument("--real-gold", default="data/gold")
    ap.add_argument("--samples", default="data/samples")
    ap.add_argument("--env-file", default="")
    ap.add_argument("--budgets", default="1500,3500")
    ap.add_argument("--overlap-units", type=int, default=DEFAULT_OVERLAP_UNITS)
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    if args.env_file:
        load_dotenv(args.env_file, override=False)
    if args.temperature >= 0:
        os.environ["LLM_TEMPERATURE"] = str(args.temperature)
    else:
        os.environ.pop("LLM_TEMPERATURE", None)
    get_settings.cache_clear()  # env 变更后重读，保证证据参数是实际值

    budgets = [int(x) for x in args.budgets.split(",") if x.strip()]
    if not budgets:
        print("--budgets 不能为空", file=sys.stderr)
        return 2
    cases = load_stress_dir(args.gold)
    if args.limit:
        cases = cases[:args.limit]
    if not cases:
        print(f"没有压力语料：{args.gold}", file=sys.stderr)
        return 2

    # 只扩关系谓词，让 02 gold 的关系轴可判（与 03 验收 8 同口径）。
    # tools/ 不是安装包：把仓库根加入 sys.path，复用 03 工具的评测域定义，
    # 避免两处谓词表漂移。
    _root = str(Path(__file__).resolve().parent.parent)
    if _root not in sys.path:
        sys.path.insert(0, _root)
    from tools.backfill_stress_ab import _stress_domain  # noqa: PLC0415

    domain = _stress_domain()
    capabilities = _model_capabilities()
    real_docs = _load_real_docs(args.real_gold, args.samples)

    results: dict[str, dict] = {}
    real_results: dict[str, dict] = {}
    for budget in budgets:
        print(f"[budget={budget}] stress corpus ...", flush=True)
        results[str(budget)] = await _run_corpus(
            cases, budget=budget, overlap=args.overlap_units,
            repeat=args.repeat, concurrency=args.concurrency, domain=domain)
        print(f"[budget={budget}] real docs ...", flush=True)
        real_results[str(budget)] = await _run_real_docs(
            real_docs, budget=budget, overlap=args.overlap_units,
            concurrency=args.concurrency, domain=domain)

    baseline_key = str(budgets[0])
    candidate_key = str(budgets[-1])
    baseline = results[baseline_key]
    candidate = results[candidate_key]
    base_real = real_results[baseline_key]
    cand_real = real_results[candidate_key]

    gate_baseline = {
        "budget": budgets[0],
        "overlap_units": baseline["overlap_units"],
        "chunk_calls": baseline["chunk_calls_total_mean"],
        "truncated_chunks": (
            baseline["truncated_chunks_max"]
            + base_real["truncated_chunks_total"]),
        "entity_macro_f1_mean": baseline["entity_macro_f1_mean"],
        "entity_macro_f1_worst": baseline["entity_macro_f1_worst"],
        "relation_f1_mean": baseline["relation_f1_mean"],
        "relation_f1_worst": baseline["relation_f1_worst"],
    }
    gate_candidate = {
        "budget": budgets[-1],
        "overlap_units": candidate["overlap_units"],
        "chunk_calls": candidate["chunk_calls_total_mean"],
        "truncated_chunks": (
            candidate["truncated_chunks_max"]
            + cand_real["truncated_chunks_total"]),
        "entity_macro_f1_mean": candidate["entity_macro_f1_mean"],
        "entity_macro_f1_worst": candidate["entity_macro_f1_worst"],
        "relation_f1_mean": candidate["relation_f1_mean"],
        "relation_f1_worst": candidate["relation_f1_worst"],
    }
    gate = evaluate_budget_gate(gate_baseline, gate_candidate)
    gate["real_docs_truncated_chunks"] = cand_real["truncated_chunks_total"]
    gate["stress_truncated_chunks"] = candidate["truncated_chunks_max"]

    residual = {
        str(b): relation_chunk_profile(
            cases, max_units=b, overlap_units=args.overlap_units)
        for b in budgets
    }
    safety = {
        str(b): _safety_margin(results[str(b)], real_results[str(b)],
                               capabilities)
        for b in budgets
    }

    calls_drop_pct = None
    if baseline["chunk_calls_total_mean"]:
        calls_drop_pct = round(
            (1 - candidate["chunk_calls_total_mean"]
             / baseline["chunk_calls_total_mean"]) * 100, 2)

    payload = {
        "experiment": "工单 04：分块预算 1500 vs 3500 A/B（真实 LLM，slim+本地回填）",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        **_git_identity(),
        "model": _model_identity(),
        "model_capabilities": capabilities,
        "sampling": {
            "repeat": args.repeat,
            "temperature": args.temperature if args.temperature >= 0 else None,
            "seed": None,
            "note": ("temperature 固定；provider 不暴露 seed，用多轮重复报 mean/worst"),
        },
        "corpus": {
            "stress_path": args.gold,
            "cases": [c.name for c in cases],
            "seeds": [c.seed for c in cases],
            "total_chars": sum(len(c.text) for c in cases),
            "stress_sha256": hashlib.sha256(
                "".join(c.text for c in cases).encode()).hexdigest()[:16],
            "real_docs": [d.id for d in real_docs],
            "real_docs_count": len(real_docs),
        },
        "chunking": {
            "budgets": budgets,
            "overlap_units": args.overlap_units,
            "overlap_note": ("本单明确不动 overlap；两档都用同一个配置值，"
                             "overlap_pairs_min 是相邻块实际重叠对数的最小值。"),
        },
        "thresholds": {
            "entity_macro_f1_max_drop_pt": 0.5,
            "relation_f1": "mean/worst 都不下降",
            "truncated_chunks": "候选档必须为 0（压力语料 + 真实文档）",
            "overlap": "配置值与实际重叠行为都不变",
            "chunk_calls": "候选档必须下降",
        },
        "budgets": results,
        "real_docs": real_results,
        "calls_drop_pct": calls_drop_pct,
        "residual_cross_chunk": residual,
        "safety_margin": safety,
        "gate": gate,
        "cost_note": "只记录 provider 真实 token；金额以账单为准，脚本不估算价格",
    }
    if args.json_out:
        _write_report(args.json_out, payload)
        print(f"wrote {args.json_out}")
    print(json.dumps({
        "git_sha": payload["git_sha"],
        "git_dirty": payload["git_dirty"],
        "calls_drop_pct": calls_drop_pct,
        "gate": gate,
        "safety_margin": safety,
        "residual_pct": {
            k: v["cross_chunk_ratio_pct"] for k, v in residual.items()},
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
