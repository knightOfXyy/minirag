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
import math
import os
import statistics
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
from moray.evaluation.metrics import PRF, evaluate, macro_average
from moray.evaluation.stress import (
    MAX_ENTITY_F1_DROP_PT,
    MAX_RELATION_PRECISION_DROP_PT,
    aggregate_cases,
    entity_type_counts,
    evaluate_budget_gate,
    evaluate_case,
    load_stress_dir,
    relation_chunk_profile,
)
from moray.extraction import Chunker, Document, Extractor, estimate_tokens
from moray.extraction.pipeline import ChunkedExtractionPipeline
from moray.schema import Entity, ExtractionResult

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


# 双侧 95% t 临界值（小样本重复次数；n>10 用正态 1.96 近似）。
_T95 = {2: 12.706, 3: 4.303, 4: 3.182, 5: 2.776, 6: 2.571,
        7: 2.447, 8: 2.365, 9: 2.306, 10: 2.262}


def _delta_summary_pt(values: list[float]) -> dict:
    """N 轮独立重复的均值差 + 95% CI + 最坏轮。

    评审 r1 S5：两档是**先后独立跑**的，没有 seed，不能叫“配对”；这里
    保留 round-aligned 的最坏轮（worst-of-N），同时明确标注 unpaired。
    """
    vals = [float(v) for v in values]
    n = len(vals)
    if not n:
        return {"values": [], "n": 0, "mean_pt": None, "sd_pt": None,
                "ci95_low_pt": None, "ci95_high_pt": None,
                "min_pt": None, "max_pt": None,
                "label": "unpaired independent repeats"}
    mean = round(statistics.mean(vals), 4)
    sd = round(statistics.stdev(vals), 4) if n > 1 else 0.0
    half = (_T95.get(n, 1.96) * sd / math.sqrt(n)) if n > 1 else None
    return {
        "values": [round(v, 4) for v in vals],
        "n": n,
        "mean_pt": mean,
        "sd_pt": sd,
        "ci95_low_pt": round(mean - half, 4) if half is not None else None,
        "ci95_high_pt": round(mean + half, 4) if half is not None else None,
        "min_pt": round(min(vals), 4),
        "max_pt": round(max(vals), 4),
        "label": ("unpaired independent repeats; worst-of-N = min over "
                  "round-aligned repeats (no seed, not a paired test)"),
    }


def _overlap_stats(result: dict) -> dict:
    """一档预算下相邻块实际重叠对数 / 可重叠对数（评审 r5 S2）。

    ``overlap_pairs`` 只统计“后块起点早于前块终点”的相邻对；当某句长于
    overlap units 时 ``overlap_seed`` 为空，该对不会重叠。因此不能把
    “最小值 >0”写成“逐对重叠”。这里取**实际重叠比例最小**的那篇，
    同时给出分母，报告中按 ``实际对数/可重叠对数`` 列报。
    """
    rows = [row for rnd in result.get("rounds", [])
            for row in rnd.get("cases", [])]
    pairs = []
    for row in rows:
        possible = int(row.get("overlap_pairs_possible")
                       or max(0, int(row.get("n_chunks", 1) or 1) - 1))
        if possible <= 0:
            continue
        actual = int(row.get("overlap_pairs", 0) or 0)
        pairs.append((actual, possible))
    if not pairs:
        return {
            "min_overlap_pairs": 0,
            # 评审 r6 N3：这是“重叠比例最小那一篇”的分母，不是全局最小
            # 分母；字段名必须说清楚，避免与全局 min_overlap_pairs 混淆。
            "min_ratio_document_possible_pairs": 0,
            "min_pair_ratio": None,
            "documents_with_multiple_chunks": 0,
        }
    actual, possible = min(pairs, key=lambda item: item[0] / item[1])
    return {
        "min_overlap_pairs": actual,
        "min_ratio_document_possible_pairs": possible,
        "min_pair_ratio": round(actual / possible, 4),
        "documents_with_multiple_chunks": len(pairs),
    }


def _entity_by_type_for_case(gold, pred) -> dict:
    """单篇 raw 按类型 P/R（评审 r6 F4）。"""
    return {
        etype: PRF(tp=tp, fp=fp, fn=fn).as_dict()
        for etype, (tp, fp, fn) in entity_type_counts(gold, pred).items()
    }


def _raw_entity_by_type_from_rows(rows, cases=None) -> dict:
    """从逐篇行聚合 raw 验收口径的按类型 P/R。

    优先读行里已持久化的 ``raw_entity_by_type``；旧证据没有该字段时，可用
    ``cases``（金标语料）与 ``predicted_entities`` 重建，并把结果写回行，
    让后续 ``--from-json`` 不再依赖语料目录。两边都没有时返回空 dict，
    不伪造数字。
    """
    totals: dict[str, list[int]] = {}
    for index, row in enumerate(rows):
        per_type = row.get("raw_entity_by_type")
        if not per_type and cases:
            # rows 按轮次展开；同一轮内与 cases 顺序一一对应。
            case = cases[index % len(cases)]
            pred = ExtractionResult(entities=[
                Entity(type=e.get("type", ""), name=e.get("name", ""))
                for e in row.get("predicted_entities", [])
            ])
            per_type = _entity_by_type_for_case(case.gold, pred)
            row["raw_entity_by_type"] = per_type
        for etype, prf in (per_type or {}).items():
            acc = totals.setdefault(etype, [0, 0, 0])
            acc[0] += int(prf["tp"])
            acc[1] += int(prf["fp"])
            acc[2] += int(prf["fn"])
    return {
        etype: PRF(tp=tp, fp=fp, fn=fn).as_dict()
        for etype, (tp, fp, fn) in sorted(totals.items())
    }


def _enrich_result_derived(result: dict, *, cases=None) -> None:
    """给既有 JSON 的逐轮数据补齐本轮新增的派生字段（``--from-json`` 用）。

    只从 ``rounds`` 复算，不调用 LLM；live 运行的 ``_run_corpus`` 已经
    直接写入同样的字段，这里保证旧证据也能被无副作用地重生成。
    """
    rounds = result.get("rounds") or []
    if "entity_strict_f1_worst" not in result:
        values = [
            rnd["macro"]["entity_strict"]["f1"] for rnd in rounds
            if rnd.get("macro", {}).get("entity_strict", {}).get("f1")
            is not None
        ]
        if values:
            result["entity_strict_f1_mean"] = _mean(values)
            result["entity_strict_f1_worst"] = _min(values)
    if "raw_relation_f1_mean" not in result:
        values = [
            rnd["raw_macro"]["relation"]["f1"] for rnd in rounds
            if rnd.get("raw_macro", {}).get("relation", {}).get("f1")
            is not None
        ]
        if values:
            result["raw_relation_f1_mean"] = _mean(values)
            result["raw_relation_f1_worst"] = _min(values)
    # 评审 r6 N3/N4：旧证据里这两个字段只写不读，重生成时清掉；分母
    # 统一由 ``_overlap_stats`` 的 ``min_ratio_document_possible_pairs`` 给出。
    result.pop("overlap_pairs_possible_min", None)
    result.pop("overlap_pairs_ratio_min", None)
    rows = [row for rnd in rounds for row in rnd.get("cases", [])]
    if rows and not result.get("raw_entity_by_type"):
        result["raw_entity_by_type"] = _raw_entity_by_type_from_rows(
            rows, cases)


def _real_doc_coverage(real_docs: dict, *, candidate_budget: int) -> dict:
    """真实文档批次是否真的把候选预算压到了（评审 r1 S2 / r2 F3）。

    判据用**文档总规模**（保守 units）而不是单块上限：一篇 5,360 units 的长文
    在 3500 档会被切成 3,483 + 剩余块，单块不必然刚好达到 3500，但它确实
    经历了 3500 档分块；反过来，≤341 units 的短桩不管切几档都只有 1 块。
    """
    rows = real_docs.get("rows", [])
    max_chunk_units = max(
        (int(r.get("max_chunk_units", 0) or 0) for r in rows),
        default=int(real_docs.get("max_chunk_units", 0) or 0),
    )
    max_document_units = max(
        (int(r.get("document_units", 0) or 0) for r in rows),
        default=0,
    )
    exercises = max_document_units > candidate_budget
    note = (
        f"真实文档批次包含超过候选预算 {candidate_budget} 的长文"
        f"（最大文档 {max_document_units} units，最大块 {max_chunk_units} units），"
        f"候选档分块已被真实压到。"
        if exercises else
        f"真实文档批次未包含超过候选预算 {candidate_budget} 的文档："
        f"最大文档仅 {max_document_units} units / 最大块 {max_chunk_units} units，"
        f"本批次只验证短文档的截断与最大真实 token，"
        f"{candidate_budget} 档在真实文档上的分块未被压到。"
    )
    return {
        "documents": len(rows),
        "max_document_units": max_document_units,
        "max_chunk_units": max_chunk_units,
        "candidate_budget": candidate_budget,
        "exercises_candidate_budget": exercises,
        "note": note,
    }


def _doc_row(doc, chunks, out, wall_s: float) -> dict:
    """压力语料与真实文档共用的逐篇行（评审 r1 N2：只此一份）。"""
    metrics = out.document_metrics
    summary = metrics.summary() if metrics is not None else {}
    records = list(metrics.chunks) if metrics is not None else []
    return {
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
        "failed_chunks": sum(1 for c in records if c.error),
        "max_chunk_input_tokens": max(
            (c.input_tokens for c in records), default=0),
        "max_chunk_output_tokens": max(
            (c.output_tokens for c in records), default=0),
        "max_chunk_units": max((c.n_units for c in chunks), default=0),
        "max_chunk_chars": max((len(c.text) for c in chunks), default=0),
        # 文档总规模（保守 units）：判断这批是否真有 >候选预算的长文。
        "document_units": estimate_tokens(doc.text),
        "overlap_pairs": _chunk_pair_overlaps(chunks),
        # 可重叠的相邻对数 = n_chunks - 1；长句使 overlap seed 为空时，
        # 实际重叠对数会小于它（评审 r5 S2，不能宣称“逐对重叠”）。
        "overlap_pairs_possible": max(0, len(chunks) - 1),
        "wall_s": round(wall_s, 3),
    }


def _chunk_cases(cases, *, budget: int, overlap: int) -> list[list]:
    """逐篇切块，按序号返回（评审 r2 F6：重名文档不能当 dict key 互相覆盖）。"""
    return [
        Chunker(budget, overlap).chunk_text(case.text) for case in cases
    ]


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
    chunks_by_index = _chunk_cases(cases, budget=budget, overlap=overlap)
    rounds: list[dict] = []
    all_evals = []
    all_raw_evals = []
    all_raw_reports = []

    extractor = Extractor(domain, max_retries=2, local_backfill=True)
    pipeline = ChunkedExtractionPipeline(extractor, concurrency=concurrency)

    for r in range(max(1, repeat)):
        rows = []
        evals = []
        raw_reports = []
        for index, case in enumerate(cases):
            doc = Document(id=case.name, text=case.text,
                           source="stress-gold", domain_code=domain.code)
            chunks = chunks_by_index[index]
            started = time.perf_counter()
            out = await pipeline.run(
                doc, chunks, raise_on_chunk_error=False)
            wall_s = time.perf_counter() - started

            # 工单 04 的 A/B 显式 opt-in 归一化；02/03 调用方保持默认口径（F4）。
            # 评审 r4 N2：evaluate_case 已经把归一化副本放进 ``ev.pred``，
            # 不再在工具层重复 apply / 逐实体重算。
            ev = evaluate_case(
                case.name, case.seed, out.result, case.gold, text=case.text,
                normalize_type_labels=True)
            raw_ev = evaluate_case(
                case.name, case.seed, out.result, case.gold,
                normalize_type_labels=False)
            raw_report = raw_ev.report
            evals.append(ev)
            all_evals.append(ev)
            all_raw_evals.append(raw_ev)
            raw_reports.append(raw_report)
            all_raw_reports.append(raw_report)

            row = {
                "name": case.name,
                "seed": case.seed,
                **_doc_row(doc, chunks, out, wall_s),
                "entity_relaxed_f1": ev.report.entity_relaxed.f1,
                "entity_relaxed_prf": ev.report.entity_relaxed.as_dict(),
                # 未归一化的原始口径也留下，报告双口径并列
                "raw_entity_relaxed_f1": raw_report.entity_relaxed.f1,
                "raw_entity_relaxed_prf": raw_report.entity_relaxed.as_dict(),
                "entity_strict_f1": ev.report.entity_strict.f1,
                "relation_f1": ev.report.relation.f1,
                "relation_prf": ev.report.relation.as_dict(),
                "relation_covered_prf": (
                    ev.relation_covered.as_dict()
                    if ev.relation_covered is not None else None),
                "gold_covered_sentences": ev.gold_covered_sentences,
                # F13：跨句 gold 关系不进 covered 子集，但必须显式计数。
                "uncovered_gold_relations": ev.uncovered_gold_relations,
                # 评审 r6 F4：raw 验收口径的按类型 P/R（不是归一化辅助口径）。
                "raw_entity_by_type": _entity_by_type_for_case(
                    case.gold, out.result),
                # S1：逐篇预测面落盘，FP 归因可审计（不只给聚合 PRF）
                "predicted_entities": [
                    {"type": raw.type, "name": raw.name,
                     "normalized_name": norm.name}
                    for raw, norm in zip(
                        out.result.entities, ev.pred.entities)
                ],
                # F1：同时保留原始端点与归一化端点，关系错配可逐条审计。
                "predicted_relations": [
                    {"head": raw.head, "predicate": raw.predicate,
                     "tail": raw.tail,
                     "normalized_head": norm.head,
                     "normalized_tail": norm.tail}
                    for raw, norm in zip(
                        out.result.relations, ev.pred.relations)
                ],
            }
            rows.append(row)

        agg = aggregate_cases(evals)
        mac = macro_average([e.report for e in evals])
        raw_mac = macro_average(raw_reports)
        covered = agg.get("relation_covered", {})
        rounds.append({
            "round": r + 1,
            "cases": rows,
            "micro": agg,
            "macro": mac,
            "raw_macro": raw_mac,
            "relation_covered_recall": covered.get("recall"),
            "relation_covered_precision": covered.get("precision"),
            "chunk_calls_total": sum(row["chunk_calls"] for row in rows),
            "input_tokens_total": sum(row["input_tokens"] for row in rows),
            "output_tokens_total": sum(row["output_tokens"] for row in rows),
            "truncated_chunks_total": sum(
                row["truncated_chunks"] for row in rows),
            "wall_s_total": round(sum(row["wall_s"] for row in rows), 3),
        })

    def _round_value(round_, key):
        return round_["macro"].get(key, {}).get("f1", 0.0)

    def _round_raw_value(round_, key):
        return round_["raw_macro"].get(key, {}).get("f1", 0.0)

    entity_means = [_round_value(r, "entity_relaxed") for r in rounds]
    raw_entity_means = [_round_raw_value(r, "entity_relaxed") for r in rounds]
    entity_stricts = [_round_value(r, "entity_strict") for r in rounds]
    relation_means = [_round_value(r, "relation") for r in rounds]
    raw_relation_means = [_round_raw_value(r, "relation") for r in rounds]
    covered_recalls = [r["relation_covered_recall"] for r in rounds]
    covered_precisions = [r["relation_covered_precision"] for r in rounds]
    n_docs = len(cases)
    n_rounds = len(rounds)
    all_rows = [row for r in rounds for row in r["cases"]]
    pooled = aggregate_cases(all_evals)
    raw_pooled = aggregate_cases(all_raw_evals)

    return {
        "budget": budget,
        "overlap_units": overlap,
        "repeat": n_rounds,
        "documents": n_docs,
        "rounds": rounds,
        "entity_macro_f1_mean": _mean(entity_means),
        "entity_macro_f1_worst": _min(entity_means),
        "raw_entity_macro_f1_mean": _mean(raw_entity_means),
        "raw_entity_macro_f1_worst": _min(raw_entity_means),
        "entity_strict_f1_mean": _mean(entity_stricts),
        "entity_strict_f1_worst": _min(entity_stricts),
        "relation_f1_mean": _mean(relation_means),
        "relation_f1_worst": _min(relation_means),
        # 评审 r5 S1：归一化只是辅助口径，原始关系 F1 也落盘并进 gate。
        "raw_relation_f1_mean": _mean(raw_relation_means),
        "raw_relation_f1_worst": _min(raw_relation_means),
        "relation_covered_recall_mean": _mean(covered_recalls),
        "relation_covered_recall_worst": _min(covered_recalls),
        "relation_covered_precision_mean": _mean(covered_precisions),
        "relation_covered_precision_worst": _min(covered_precisions),
        "relation_covered_tp_total": sum(
            row["relation_covered_prf"]["tp"] for row in all_rows
            if row["relation_covered_prf"] is not None),
        "relation_covered_fp_total": sum(
            row["relation_covered_prf"]["fp"] for row in all_rows
            if row["relation_covered_prf"] is not None),
        "relation_covered_fn_total": sum(
            row["relation_covered_prf"]["fn"] for row in all_rows
            if row["relation_covered_prf"] is not None),
        "gold_covered_sentences_total": sum(
            row["gold_covered_sentences"] for row in all_rows),
        "uncovered_gold_relations_total": sum(
            row["uncovered_gold_relations"] for row in all_rows),
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
        # 评审 r6 F4：raw 验收口径的按类型 P/R，与归一化辅助口径并列。
        "raw_entity_by_type": raw_pooled["entity_by_type"],
        "micro_pooled": {
            "entity_relaxed": pooled["entity_relaxed"],
            "entity_strict": pooled["entity_strict"],
            "relation": pooled["relation"],
            "relation_covered": pooled.get("relation_covered"),
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


def _load_real_docs(
    gold_dir: str,
    samples_dir: str,
    *,
    extra_docs: list[str] | tuple[str, ...] = (),
) -> list[Document]:
    """真实文档批次：基线黄金集正文 + samples 原始文本 + 显式追加的长文。

    评审 r2 F3：仓库内置真实文档都 ≤341 units，3500 档从未被压到；``--real-doc``
    允许传入至少一篇超过候选预算的真实/脱敏长文，避免“真实文档截断证据”空转。
    """
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
    for raw_path in extra_docs:
        path = Path(raw_path)
        if not path.exists():
            raise FileNotFoundError(f"--real-doc 不存在: {path}")
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            continue
        docs.append(Document(
            id=f"real-doc:{path}", text=text,
            source="real-doc", domain_code="general"))
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
        rows.append({"id": doc.id, **_doc_row(doc, chunks, out, wall_s)})
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
        "max_chunk_units": max(
            (r["max_chunk_units"] for r in rows), default=0),
        "chunk_calls_total": sum(r["chunk_calls"] for r in rows),
    }


def _round_aligned_delta_pt(baseline: dict, candidate: dict,
                            family: str) -> list[float]:
    """逐轮对齐差（百分点）：worst-of-N 取最坏一轮。

    评审 r1 S5：两档是先后独立跑、无 seed，**不是配对检验**；这里只做
    轮次对齐，供 worst-of-N 与“同一轮次最坏”展示，CI 在报告里按独立重复
    （unpaired）计算。
    """
    out = []
    for base_round, cand_round in zip(baseline["rounds"], candidate["rounds"]):
        base_f1 = base_round["macro"][family]["f1"]
        cand_f1 = cand_round["macro"][family]["f1"]
        out.append(round((cand_f1 - base_f1) * 100, 4))
    return out


def _round_aligned_raw_delta_pt(baseline: dict, candidate: dict) -> list[float]:
    """原始未归一化实体 macro-F1 的逐轮对齐差（百分点）。"""
    out = []
    for base_round, cand_round in zip(baseline["rounds"], candidate["rounds"]):
        base_f1 = base_round["raw_macro"]["entity_relaxed"]["f1"]
        cand_f1 = cand_round["raw_macro"]["entity_relaxed"]["f1"]
        out.append(round((cand_f1 - base_f1) * 100, 4))
    return out


def _round_aligned_strict_delta_pt(baseline: dict,
                                   candidate: dict) -> list[float]:
    """实体 strict-F1 的逐轮对齐差（百分点，评审 r6 F3）。"""
    out = []
    for base_round, cand_round in zip(baseline["rounds"], candidate["rounds"]):
        base_f1 = base_round["macro"]["entity_strict"]["f1"]
        cand_f1 = cand_round["macro"]["entity_strict"]["f1"]
        out.append(round((cand_f1 - base_f1) * 100, 4))
    return out


def _round_aligned_raw_relation_delta_pt(
    baseline: dict, candidate: dict
) -> list[float]:
    """原始未归一化全量关系 F1 的逐轮对齐差（百分点，评审 r5 S1）。"""
    out = []
    for base_round, cand_round in zip(baseline["rounds"], candidate["rounds"]):
        base_f1 = base_round["raw_macro"]["relation"]["f1"]
        cand_f1 = cand_round["raw_macro"]["relation"]["f1"]
        out.append(round((cand_f1 - base_f1) * 100, 4))
    return out


def _round_aligned_covered_delta_pt(baseline: dict, candidate: dict,
                                    field: str) -> list[float]:
    out = []
    for base_round, cand_round in zip(baseline["rounds"], candidate["rounds"]):
        base_v = base_round.get(field)
        cand_v = cand_round.get(field)
        if base_v is None or cand_v is None:
            continue
        out.append(round((cand_v - base_v) * 100, 4))
    return out


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


def gate_thresholds() -> dict:
    """gate 的公开阈值说明（写进 evidence JSON，必须与实现一致）。

    评审 r4 S1：旧文本写“全量口径只列报，不参与放行”，与 ``checks`` 里的
    ``relation_full_f1_*`` 正好相反；这里改成实现事实，并补充实体双口径。
    """
    return {
        # 评审 r6 N5：直接复用 budget 模块的常量，避免报告/工具各写一份。
        "entity_macro_f1_max_drop_pt": MAX_ENTITY_F1_DROP_PT,
        "relation_covered_precision_max_drop_pt": (
            MAX_RELATION_PRECISION_DROP_PT),
        "entity_macro_f1_note": (
            "raw（原始未归一化，验收口径）与 normalized（类型前缀标签归一化，"
            "辅助口径）两套 macro-F1 的 mean/worst 都进 checks，任一跌幅 "
            f">{MAX_ENTITY_F1_DROP_PT}pt 即 fail；辅助口径不能替代验收口径"),
        "entity_strict_note": (
            "strict 实体 F1（比 mention.surface + span，不做前缀归一化）的 "
            f"mean/worst 也进 checks，门槛同为 {MAX_ENTITY_F1_DROP_PT}pt"
            "（评审 r6 F3）"),
        "context_margin": (
            "候选档 provider 真实 tokenizer 的 "
            "max_chunk_input_tokens + max_chunk_output_tokens 必须 "
            "<= /models context_window；能力缺失（None/0）时 "
            "context_within_window fail closed（评审 r6 F1，验收 #5）"),
        "relation_gate": ("金标覆盖子集 recall mean/worst 不下降；"
                          f"precision mean/worst 跌幅 <="
                          f"{MAX_RELATION_PRECISION_DROP_PT}pt"),
        "relation_full_scope": ("全量口径（含 02 金标刻意未标注句面的 FP）"
                                "的 raw（原始未归一化，验收口径）与 "
                                "normalized（类型前缀标签归一化，辅助口径）"
                                "两套 mean/worst 都参与放行（gate.checks），"
                                "与覆盖子集并列列报；辅助口径不能替代验收口径"),
        "relation_note": ("评审 r5 S1：raw_relation_f1_* 必填并进 checks，"
                          "不再只列报"),
        "truncated_chunks": "候选档必须为 0（压力语料 + 真实文档）",
        "overlap": ("冻结基线 200 不变；overlap_observed 只做 >0 冒烟检查，"
                    "实际重叠对数/可重叠对数见 overlap_observed（长句可致"
                    "并非逐对重叠）"),
        "chunk_calls": "候选档必须下降",
        "budgets": "两档预算必须不同（1500-vs-1500 不能 passed）",
    }


def _gate_rounds(result: dict) -> list[dict[str, float]]:
    """把逐轮 macro 值整理成 gate 复算 explicit worst delta 的输入。"""
    return [
        {
            "entity_macro_f1": r["macro"]["entity_relaxed"]["f1"],
            "raw_entity_macro_f1": r["raw_macro"]["entity_relaxed"]["f1"],
            "entity_strict_f1": r["macro"]["entity_strict"]["f1"],
            "relation_f1": r["macro"]["relation"]["f1"],
            "raw_relation_f1": r["raw_macro"]["relation"]["f1"],
            "relation_covered_recall": r["relation_covered_recall"],
            "relation_covered_precision": r["relation_covered_precision"],
        }
        for r in result["rounds"]
    ]


def _gate_input(
    result: dict,
    *,
    budget: int,
    real: dict,
    overlap_units: int,
    safety: dict | None = None,
) -> dict:
    """一份 gate 输入（评审 r5 N4：live / ``--from-json`` 共用）。

    此前 baseline/candidate 各手写一份 17 键 dict，新增 raw 关系口径要在
    两处同步；收敛到这里后，新增口径只改这一个函数。
    """
    safety = safety or {}
    return {
        "budget": budget,
        # 评审 r6 F1：上下文余量能力。缺失时保持 None，由 gate fail closed。
        "context_window": safety.get("context_window"),
        "max_context_input_tokens": safety.get("max_chunk_input_tokens"),
        "max_context_output_tokens": safety.get("max_chunk_output_tokens"),
        "overlap_units": overlap_units,
        "overlap_pairs_min": result["overlap_pairs_min"],
        "chunk_calls": result["chunk_calls_total_mean"],
        "truncated_chunks": (
            result["truncated_chunks_max"]
            + real["truncated_chunks_total"]),
        "entity_macro_f1_mean": result["entity_macro_f1_mean"],
        "entity_macro_f1_worst": result["entity_macro_f1_worst"],
        "raw_entity_macro_f1_mean": result["raw_entity_macro_f1_mean"],
        "raw_entity_macro_f1_worst": result["raw_entity_macro_f1_worst"],
        # 评审 r6 F3：strict 与 relaxed 并列，两者都必填。
        "entity_strict_f1_mean": result["entity_strict_f1_mean"],
        "entity_strict_f1_worst": result["entity_strict_f1_worst"],
        "relation_covered_recall_mean": result[
            "relation_covered_recall_mean"],
        "relation_covered_recall_worst": result[
            "relation_covered_recall_worst"],
        "relation_covered_precision_mean": result[
            "relation_covered_precision_mean"],
        "relation_covered_precision_worst": result[
            "relation_covered_precision_worst"],
        "relation_f1_mean": result["relation_f1_mean"],
        "relation_f1_worst": result["relation_f1_worst"],
        "raw_relation_f1_mean": result["raw_relation_f1_mean"],
        "raw_relation_f1_worst": result["raw_relation_f1_worst"],
    }


def _derive_comparison(
    baseline: dict,
    candidate: dict,
    base_real: dict,
    cand_real: dict,
    *,
    budgets: list[int],
    capabilities: dict | None = None,
) -> dict:
    """由两档逐轮结果生成 gate 与所有派生对比字段（live / --from-json 共用）。"""
    capabilities = capabilities or {}
    safety = {
        str(budgets[0]): _safety_margin(baseline, base_real, capabilities),
        str(budgets[-1]): _safety_margin(candidate, cand_real, capabilities),
    }
    # 评审 r1 S3：基线 overlap 用**冻结常量**，不再两档都从同一个
    # args.overlap_units 取值自证；候选档必须仍然等于 200。
    gate_baseline = _gate_input(
        baseline, budget=budgets[0], real=base_real,
        overlap_units=DEFAULT_OVERLAP_UNITS, safety=safety[str(budgets[0])])
    gate_candidate = _gate_input(
        candidate, budget=budgets[-1], real=cand_real,
        overlap_units=candidate["overlap_units"],
        safety=safety[str(budgets[-1])])
    # worst-of-N 用逐轮对齐差（更严格）：两档各自的最差值相减会漏掉
    # “某一轮单独崩、另一轮恰好很好”的情况。
    entity_aligned = _round_aligned_delta_pt(
        baseline, candidate, "entity_relaxed")
    raw_entity_aligned = _round_aligned_raw_delta_pt(baseline, candidate)
    strict_entity_aligned = _round_aligned_strict_delta_pt(baseline, candidate)
    relation_full_aligned = _round_aligned_delta_pt(
        baseline, candidate, "relation")
    raw_relation_full_aligned = _round_aligned_raw_relation_delta_pt(
        baseline, candidate)
    covered_recall_aligned = _round_aligned_covered_delta_pt(
        baseline, candidate, "relation_covered_recall")
    covered_precision_aligned = _round_aligned_covered_delta_pt(
        baseline, candidate, "relation_covered_precision")
    gate_candidate["entity_macro_f1_worst_delta_pt"] = min(entity_aligned)
    gate_candidate["raw_entity_macro_f1_worst_delta_pt"] = min(
        raw_entity_aligned)
    gate_candidate["entity_strict_f1_worst_delta_pt"] = min(
        strict_entity_aligned)
    gate_candidate["relation_f1_worst_delta_pt"] = min(relation_full_aligned)
    gate_candidate["raw_relation_f1_worst_delta_pt"] = min(
        raw_relation_full_aligned)
    gate_candidate["relation_covered_recall_worst_delta_pt"] = min(
        covered_recall_aligned)
    gate_candidate["relation_covered_precision_worst_delta_pt"] = min(
        covered_precision_aligned)
    # 评审 r4 N3：gate 用 rounds 复算并校验 explicit worst delta，不采信自证。
    gate = evaluate_budget_gate(
        gate_baseline, gate_candidate,
        baseline_rounds=_gate_rounds(baseline),
        candidate_rounds=_gate_rounds(candidate))
    gate["real_docs_truncated_chunks"] = cand_real["truncated_chunks_total"]
    gate["stress_truncated_chunks"] = candidate["truncated_chunks_max"]
    gate["real_doc_coverage"] = _real_doc_coverage(
        cand_real, candidate_budget=budgets[-1])

    delta_summary = {
        "entity_macro_f1": _delta_summary_pt(entity_aligned),
        "raw_entity_macro_f1": _delta_summary_pt(raw_entity_aligned),
        "entity_strict_f1": _delta_summary_pt(strict_entity_aligned),
        "relation_full": _delta_summary_pt(relation_full_aligned),
        "relation_raw_full": _delta_summary_pt(raw_relation_full_aligned),
        "relation_covered_recall": _delta_summary_pt(covered_recall_aligned),
        "relation_covered_precision": _delta_summary_pt(
            covered_precision_aligned),
        "note": ("mean/95% CI 按 N 轮独立重复计算（无 seed，unpaired）；"
                 "min_pt 是 round-aligned worst-of-N，不冒充配对检验"),
    }
    gate_frozen_overlap = {
        "baseline_frozen_overlap_units": DEFAULT_OVERLAP_UNITS,
        "candidate_config_overlap_units": candidate["overlap_units"],
        "baseline_observed_overlap_pairs_min": baseline["overlap_pairs_min"],
        "candidate_observed_overlap_pairs_min": candidate["overlap_pairs_min"],
        "unchanged": (candidate["overlap_units"] == DEFAULT_OVERLAP_UNITS),
    }
    calls_drop_pct = None
    if baseline["chunk_calls_total_mean"]:
        calls_drop_pct = round(
            (1 - candidate["chunk_calls_total_mean"]
             / baseline["chunk_calls_total_mean"]) * 100, 2)
    return {
        "gate": gate,
        "safety_margin": safety,
        "calls_drop_pct": calls_drop_pct,
        "overlap_observed": {
            "baseline": {
                "budget": budgets[0], **_overlap_stats(baseline)},
            "candidate": {
                "budget": budgets[-1], **_overlap_stats(candidate)},
            "gate_check": ("gate.overlap_observed 只做 min_overlap_pairs > 0 "
                           "的冒烟检查；是否每对都重叠不是放行条件"),
            "note": ("长句（> overlap units）会让 overlap_seed 为空，因此"
                     "不是每对相邻块都重叠；这里列报实际重叠对数 / "
                     "可重叠对数（n_chunks-1），合成正文的逐对断言由 "
                     "tests/test_chunk_budget.py 的配置测试覆盖"),
        },
        "delta_summary": delta_summary,
        "round_aligned_deltas_pt": {
            "entity_macro_f1": entity_aligned,
            "raw_entity_macro_f1": raw_entity_aligned,
            "entity_strict_f1": strict_entity_aligned,
            "relation_full": relation_full_aligned,
            "relation_raw_full": raw_relation_full_aligned,
            "relation_covered_recall": covered_recall_aligned,
            "relation_covered_precision": covered_precision_aligned,
            "note": ("候选减基线，仅按轮次对齐；两档独立跑、无 seed，"
                     "不构成配对检验"),
        },
        "overlap_frozen_baseline": gate_frozen_overlap,
    }


def _write_report(path: str | Path, payload: dict) -> None:
    Path(path).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _recompute_from_payload(
    payload: dict, *, source_path: str | Path, json_out: str
) -> int:
    """从既有证据 JSON 重算 gate/阈值派生字段，不调用 LLM。

    评审 r4 B1/S1：修判据后必须重生成 gate 段；原始逐轮/逐篇数据已经在
    JSON 里，``--from-json`` 保证派生字段仍由工具生成而不是手改。
    """
    budgets = [
        int(b) for b in (
            payload.get("chunking", {}).get("budgets")
            or sorted(payload.get("budgets", {}), key=int))
    ]
    if len(budgets) < 2:
        print("--from-json 需要至少两档预算", file=sys.stderr)
        return 2
    results = payload["budgets"]
    real_results = payload["real_docs"]
    # 评审 r6 F4：旧 JSON 没有 raw_entity_by_type 时，从 corpus 金标与
    # 已持久化的 predicted_entities 重建；拿不到 corpus 就保持缺失，
    # 不伪造数字（gate 不依赖该字段）。
    enrich_cases = None
    stress_path = (payload.get("corpus") or {}).get("stress_path")
    if stress_path:
        try:
            enrich_cases = load_stress_dir(stress_path)
        except Exception:  # noqa: BLE001 仅影响报告字段，不让重算失败
            enrich_cases = None
    for budget in budgets:
        _enrich_result_derived(results[str(budget)], cases=enrich_cases)
    baseline = results[str(budgets[0])]
    candidate = results[str(budgets[-1])]
    base_real = real_results[str(budgets[0])]
    cand_real = real_results[str(budgets[-1])]
    derived = _derive_comparison(
        baseline, candidate, base_real, cand_real, budgets=budgets,
        capabilities=payload.get("model_capabilities") or {})
    payload["thresholds"] = gate_thresholds()
    payload["safety_margin"] = derived["safety_margin"]
    payload["calls_drop_pct"] = derived["calls_drop_pct"]
    payload["delta_summary"] = derived["delta_summary"]
    payload["round_aligned_deltas_pt"] = derived["round_aligned_deltas_pt"]
    payload["overlap_frozen_baseline"] = derived["overlap_frozen_baseline"]
    payload["overlap_observed"] = derived["overlap_observed"]
    # 评审 r5 N2：每档用自己的预算判断“真实文档是否压到该档”。
    payload["real_doc_coverage"] = {
        str(b): _real_doc_coverage(real_results[str(b)], candidate_budget=b)
        for b in budgets
    }
    payload["gate"] = derived["gate"]
    payload["gate_recompute"] = {
        "source_json": str(source_path),
        "source_git_sha": payload.get("git_sha"),
        "source_generated_at": payload.get("generated_at"),
        "regenerated_at": datetime.now(timezone.utc).isoformat(),
        "regenerator": _git_identity(),
        "note": ("原始逐轮/逐篇数据未重跑 LLM；gate 与 thresholds 由当前"
                 "实现从既有 JSON 复算，gate 用 rounds 校验 explicit deltas"),
    }
    _write_report(json_out, payload)
    print(f"wrote {json_out}")
    print(json.dumps({
        "source_git_sha": payload.get("git_sha"),
        "gate": derived["gate"],
        "calls_drop_pct": derived["calls_drop_pct"],
    }, ensure_ascii=False, indent=2))
    return 0


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gold", default="data/gold/stress")
    ap.add_argument("--real-gold", default="data/gold")
    ap.add_argument("--samples", default="data/samples")
    ap.add_argument(
        "--real-doc", action="append", default=[],
        help="追加真实/脱敏长文路径；可重复，用于把 3500 档真正压到")
    ap.add_argument("--env-file", default="")
    ap.add_argument("--budgets", default="1500,3500")
    ap.add_argument("--overlap-units", type=int, default=DEFAULT_OVERLAP_UNITS)
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--repeat", type=int, default=5)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--json-out", default="")
    ap.add_argument(
        "--from-json", default="",
        help="从既有证据 JSON 重算 gate/thresholds（不调用 LLM，需配 --json-out）")
    args = ap.parse_args()

    if args.from_json:
        if not args.json_out:
            print("--from-json 必须同时给 --json-out", file=sys.stderr)
            return 2
        payload = json.loads(
            Path(args.from_json).read_text(encoding="utf-8"))
        return _recompute_from_payload(
            payload, source_path=args.from_json, json_out=args.json_out)

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
    real_docs = _load_real_docs(
        args.real_gold, args.samples, extra_docs=args.real_doc)

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

    derived = _derive_comparison(
        baseline, candidate, base_real, cand_real, budgets=budgets,
        capabilities=capabilities)
    gate = derived["gate"]
    delta_summary = derived["delta_summary"]
    gate_frozen_overlap = derived["overlap_frozen_baseline"]
    calls_drop_pct = derived["calls_drop_pct"]
    safety = derived["safety_margin"]
    real_doc_coverage = {
        str(b): _real_doc_coverage(real_results[str(b)], candidate_budget=b)
        for b in budgets
    }

    residual = {
        str(b): relation_chunk_profile(
            cases, max_units=b, overlap_units=args.overlap_units)
        for b in budgets
    }
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
            "note": ("temperature 固定；provider 不暴露 seed，用多轮独立重复报 "
                     "mean/95% CI/worst；两档先后独立跑，不是配对检验"),
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
        "thresholds": gate_thresholds(),
        "budgets": results,
        "real_docs": real_results,
        "real_doc_coverage": real_doc_coverage,
        "calls_drop_pct": calls_drop_pct,
        "delta_summary": delta_summary,
        "round_aligned_deltas_pt": derived["round_aligned_deltas_pt"],
        "overlap_frozen_baseline": gate_frozen_overlap,
        "overlap_observed": derived["overlap_observed"],
        "residual_cross_chunk": residual,
        "safety_margin": safety,
        "gate": gate,
        "gold_gap": {
            "note": ("02 压力集是刻意 gold 子集：3500 档读到更多真实句面时，"
                     "未标注的涉案人/法定代表人关系会被记全量 FP；本单不改 "
                     "02 金标，建议后续单独开工单扩标。"),
            "full_relation_fp_increase": (
                int(candidate["relation_fp_total"] or 0)
                - int(baseline["relation_fp_total"] or 0)),
            "full_entity_fp_increase": (
                int(candidate["entity_fp_total"] or 0)
                - int(baseline["entity_fp_total"] or 0)),
        },
        "cost_note": ("只记录 provider 真实 token；金额以账单为准。"
                      "token 为抽取请求本身，不含本地评测计算开销"),
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
