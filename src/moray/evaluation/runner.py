"""
评测运行器（L15）
=================

把一个抽取器放到整批黄金用例上跑，逐篇算 P/R/F1，再汇总：
  · 宏平均 macro：每篇等权取平均（长文不会主导分数）；
  · 微平均 micro：把所有篇的 TP/FP/FN 相加后一次性算（受高频实体影响）。
还能并排比较多个抽取器，输出"改进前后"的分数差，体现评测驱动开发。

纯离线：被评对象只要实现 async extract(Document)->ExtractionResult 即可，
可以是规则基线，也可以是 LLM Extractor（后者慢、贵，按需在 --live 里用）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..schema import ExtractionResult
from .gold import GoldCase, case_document, load_gold_dir
from .metrics import EvalReport, PRF, evaluate


class Extractable(Protocol):
    async def extract(self, document) -> ExtractionResult: ...


@dataclass
class CaseResult:
    doc_id: str
    report: EvalReport


@dataclass
class RunResult:
    extractor: str
    cases: list[CaseResult]

    def macro(self) -> dict:
        reports = [c.report for c in self.cases]
        out = {"documents": len(reports)}
        for attr, label in (
                ("entity_strict", "entity_strict"),
                ("entity_relaxed", "entity_relaxed"),
                ("relation", "relation")):
            out[label] = {
                "precision": round(
                    sum(getattr(r, attr).precision for r in reports)
                    / len(reports), 4),
                "recall": round(
                    sum(getattr(r, attr).recall for r in reports)
                    / len(reports), 4),
                "f1": round(
                    sum(getattr(r, attr).f1 for r in reports)
                    / len(reports), 4),
            }
        return out

    def micro(self) -> dict:
        out = {}
        for attr, label in (
                ("entity_strict", "entity_strict"),
                ("entity_relaxed", "entity_relaxed"),
                ("relation", "relation")):
            agg = PRF()
            for c in self.cases:
                prf = getattr(c.report, attr)
                agg.tp += prf.tp
                agg.fp += prf.fp
                agg.fn += prf.fn
            out[label] = agg.as_dict()
        return out


async def run_evaluation(
    extractor: Extractable, cases: list[GoldCase], *, name: str = ""
) -> RunResult:
    """在黄金集上评测一个抽取器。"""
    results: list[CaseResult] = []
    for case in cases:
        doc = case_document(case)
        pred = await extractor.extract(doc)
        results.append(CaseResult(
            doc_id=case.doc_id, report=evaluate(pred, case.gold)))
    return RunResult(extractor=name or type(extractor).__name__, cases=results)


def compare(before: RunResult, after: RunResult) -> dict:
    """两档结果在 macro F1 上的差值（改进是否有效，一眼可见）。"""
    b, a = before.macro(), after.macro()
    return {
        metric: {
            "baseline_f1": b[metric]["f1"],
            "improved_f1": a[metric]["f1"],
            "delta": round(a[metric]["f1"] - b[metric]["f1"], 4),
        }
        for metric in ("entity_strict", "entity_relaxed", "relation")
    }


def save_report(path: str | Path, runs: list[RunResult]) -> None:
    payload = {
        r.extractor: {"macro": r.macro(), "micro": r.micro()} for r in runs}
    if len(runs) == 2:
        payload["_comparison_macro_f1"] = compare(runs[0], runs[1])
    Path(path).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
