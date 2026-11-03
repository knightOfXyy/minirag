"""
分块参数网格复跑（工单 02）
==========================

同一份压力语料，按多组 ``(max_units, overlap_units)`` 块参数各离线跑一遍
被评对象（默认 :class:`StressRuleExtractor`，可用 ``extractor_factory`` 注入
真实抽取路径），逐篇评分并微平均，产出可留存、可 A/B 的对比结果。后续
「调大块预算 1500→3500」工单直接拿本报告对比 F1/关系召回，不需要再发明
评测口径；报告里的 ``extractor`` 身份随注入的工厂自动更新。

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
# 抽取器、哪版规则跑出来的。身份从被评对象类上取（``identity`` 类属性），
# 注入自定义抽取器时报告会自动跟着换，不会永远写着演示用规则抽取器。
_CORPUS_VERSION = 3

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


def extractor_identity(factory: type) -> dict[str, object]:
    """被评对象身份：优先取类上的 ``identity`` 声明，否则用类名兜底。"""
    ident = getattr(factory, "identity", None)
    if ident:
        return dict(ident)
    return {"name": getattr(factory, "__name__", "extractor"), "revision": 0}


async def run_one_grid(
    cases: list[StressGoldCase], max_units: int, overlap_units: int, *,
    extractor_factory: type = StressRuleExtractor,
) -> list[CaseEvaluation]:
    extractor = extractor_factory(
        max_units=max_units, overlap_units=overlap_units)
    out: list[CaseEvaluation] = []
    for case in cases:
        pred = await extractor.extract(_doc(case))
        out.append(evaluate_case(case.name, case.seed, pred, case.gold))
    return out


async def run_grid_async(
    cases: list[StressGoldCase],
    grids: list[tuple[int, int]] | tuple[tuple[int, int], ...] = DEFAULT_GRIDS,
    *,
    extractor_factory: type = StressRuleExtractor,
) -> dict[tuple[int, int], list[CaseEvaluation]]:
    """对每组块参数在同一语料上复跑，返回 grid -> 逐篇评分。

    ``extractor_factory`` 必须接受 ``max_units``/``overlap_units`` 关键字，
    并提供 ``async extract(Document)``——后续工单可直接注入真实抽取路径。
    """
    result = {}
    for grid in grids:
        result[grid] = await run_one_grid(
            cases, grid[0], grid[1], extractor_factory=extractor_factory)
    return result


def run_grid(
    cases: list[StressGoldCase],
    grids: list[tuple[int, int]] | tuple[tuple[int, int], ...] = DEFAULT_GRIDS,
    *,
    extractor_factory: type = StressRuleExtractor,
) -> dict[tuple[int, int], list[CaseEvaluation]]:
    """同步封装（CLI/普通脚本直接用，无需自己起事件循环）。

    异步调用方（如 pytest-asyncio 测试）请直接 ``await run_grid_async``。
    """
    return asyncio.run(run_grid_async(
        cases, grids, extractor_factory=extractor_factory))


def aggregate_grid(evaluations: list[CaseEvaluation]) -> dict:
    return aggregate_cases(evaluations)


def build_report(
    grid_results: dict[tuple[int, int], list[CaseEvaluation]],
    *,
    extractor_factory: type = StressRuleExtractor,
) -> dict:
    """组装可落盘 JSON：每档块参数的微平均指标，含按实体类型拆分。"""
    out: dict = {
        "extractor": extractor_identity(extractor_factory),
        "corpus_version": _CORPUS_VERSION,
        "overlap_note": (
            "overlap 目前是记录维度：当前语料没有只在重叠窗口下才可恢复的"
            "构造，因此同一块预算下不同 overlap 的指标相同；它不构成判别力，"
            "调 overlap 的工单请先补可观测构造。"),
        "entity_axis_note": (
            "实体轴只证明分块/回填接线：被评规则抽取器与语料共用词典，"
            "实体召回恒为 1.0 属预期；块预算判据看关系轴。"),
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
    extractor_factory: type = StressRuleExtractor,
) -> None:
    payload = build_report(
        grid_results, extractor_factory=extractor_factory)
    payload["seeds"] = sorted(seeds) if seeds is not None else []
    Path(path).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
