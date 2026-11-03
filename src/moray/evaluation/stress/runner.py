"""
分块参数网格复跑（工单 02）
==========================

同一份压力语料，按多组 ``(max_units, overlap_units)`` 块参数各离线跑一遍
确定性 :class:`StressRuleExtractor`，逐篇评分并微平均，产出可留存、可 A/B 的
对比结果。后续「调大块预算 1500→3500」工单直接拿本报告对比 F1/关系召回，
不需要再发明评测口径。

不发任何真实 LLM 请求。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ...extraction.document import Document
from .extractor import StressRuleExtractor
from .loader import StressGoldCase
from .metrics import CaseEvaluation, aggregate_cases, evaluate_case

# 报告自带身份信息：几个月后拿到一份 eval_grid_report.json 也能知道是哪个
# 抽取器、哪版规则跑出来的。规则/口径有实质变化时递增 revision。
_EXTRACTOR_IDENTITY: dict[str, object] = {
    "name": "StressRuleExtractor",
    "revision": 2,
}
_CORPUS_VERSION = 2

# 默认参数网格：生产档 + overlap 档位 + 候选更大预算（含 04 单目标 3500）
DEFAULT_GRIDS: tuple[tuple[int, int], ...] = (
    (1500, 0),
    (1500, 200),     # 当前生产默认
    (2500, 200),
    (3500, 200),     # 工单 04 目标预算
)


def _doc(case: StressGoldCase) -> Document:
    return Document(id=case.doc_id, text=case.text,
                    source="stress-gold", domain_code="general")


async def run_one_grid(
    cases: list[StressGoldCase], max_units: int, overlap_units: int
) -> list[CaseEvaluation]:
    extractor = StressRuleExtractor(
        max_units=max_units, overlap_units=overlap_units)
    out: list[CaseEvaluation] = []
    for case in cases:
        pred = await extractor.extract(_doc(case))
        out.append(evaluate_case(case.name, case.seed, pred, case.gold))
    return out


async def run_grid_async(
    cases: list[StressGoldCase],
    grids: list[tuple[int, int]] | tuple[tuple[int, int], ...] = DEFAULT_GRIDS,
) -> dict[tuple[int, int], list[CaseEvaluation]]:
    """对每组块参数在同一语料上复跑，返回 grid -> 逐篇评分。"""
    result = {}
    for grid in grids:
        result[grid] = await run_one_grid(cases, grid[0], grid[1])
    return result


def run_grid(
    cases: list[StressGoldCase],
    grids: list[tuple[int, int]] | tuple[tuple[int, int], ...] = DEFAULT_GRIDS,
) -> dict[tuple[int, int], list[CaseEvaluation]]:
    """同步封装（CLI/普通脚本直接用，无需自己起事件循环）。

    异步调用方（如 pytest-asyncio 测试）请直接 ``await run_grid_async``。
    """
    return asyncio.run(run_grid_async(cases, grids))


def aggregate_grid(evaluations: list[CaseEvaluation]) -> dict:
    return aggregate_cases(evaluations)


def build_report(
    grid_results: dict[tuple[int, int], list[CaseEvaluation]],
) -> dict:
    """组装可落盘 JSON：每档块参数的微平均指标，含按实体类型拆分。"""
    out: dict = {
        "extractor": dict(_EXTRACTOR_IDENTITY),
        "corpus_version": _CORPUS_VERSION,
        "grids": {},
    }
    for (max_units, overlap_units), evals in sorted(grid_results.items()):
        label = f"{max_units}:{overlap_units}"
        agg = aggregate_grid(evals)
        agg["max_units"] = max_units
        agg["overlap_units"] = overlap_units
        out["grids"][label] = agg
    return out


def save_grid_report(
    path: str | Path,
    grid_results: dict[tuple[int, int], list[CaseEvaluation]],
    *,
    seeds: list[int] | None = None,
) -> None:
    payload = build_report(grid_results)
    payload["seeds"] = sorted(seeds) if seeds is not None else []
    Path(path).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
