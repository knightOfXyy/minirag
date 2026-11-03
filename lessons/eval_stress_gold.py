#!/usr/bin/env python3
"""
压力语料离线分块 A/B 评测（工单 02）
====================================

对 ``data/gold/stress`` 下已生成的压力案卷，用确定性
:class:`StressRuleExtractor` 按多组 (块预算/重叠) 参数离线复跑同一语料，
输出实体（strict/relaxed + 按类型）与关系的 P/R/F1，并把对比结果落盘为 JSON，
供后续「输出瘦身」「调大块预算 1500→3500」工单直接做参数 A/B。

不发任何真实 LLM 请求。

用法（仓根）::

    PYTHONPATH=src python3 lessons/eval_stress_gold.py
    PYTHONPATH=src python3 lessons/eval_stress_gold.py \\
        --grids 1500:0 1500:200 3500:200 --report data/gold/stress/eval_grid_report.json
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from moray.evaluation.stress import (  # noqa: E402
    DEFAULT_GRIDS,
    aggregate_grid,
    load_stress_dir,
    save_grid_report,
)
from moray.evaluation.stress.runner import run_grid  # noqa: E402


def parse_grids(raw: list[str]) -> list[tuple[int, int]]:
    out = []
    for item in raw:
        m = re.fullmatch(r"(\d+):(\d+)", item.strip())
        if not m:
            raise SystemExit(f"--grids 项应为 max:overlap，收到：{item!r}")
        out.append((int(m.group(1)), int(m.group(2))))
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    p.add_argument("--gold-dir", type=Path,
                   default=Path("data/gold/stress"))
    p.add_argument("--grids", nargs="+", default=None,
                   help="多组 max:overlap；默认覆盖生产档与候选大预算")
    p.add_argument("--report", type=Path,
                   default=Path("data/gold/stress/eval_grid_report.json"))
    args = p.parse_args(argv)

    cases = load_stress_dir(args.gold_dir)
    if not cases:
        raise SystemExit(f"{args.gold_dir} 下没有压力案卷（先跑 gen_stress_gold.py）")
    grids = parse_grids(args.grids) if args.grids else list(DEFAULT_GRIDS)

    grid_results = run_grid(cases, grids)
    seeds = [c.seed for c in cases]
    save_grid_report(args.report, grid_results, seeds=seeds)

    # 控制台摘要：每档实体 relaxed 与关系的 P/R/F1
    print(f"语料 {len(cases)} 篇，种子 {sorted(seeds)}\n")
    header = (f"{'max:ovlp':>10} {'ent_P':>7} {'ent_R':>7} {'ent_F1':>7}"
              f" {'rel_P':>7} {'rel_R':>7} {'rel_F1':>7}")
    print(header)
    print("-" * len(header))
    for (mx, ov), evals in sorted(grid_results.items()):
        agg = aggregate_grid(evals)
        er, rr = agg["entity_relaxed"], agg["relation"]
        print(f"{mx}:{ov:<4} {er['precision']:>7.4f} {er['recall']:>7.4f} "
              f"{er['f1']:>7.4f} {rr['precision']:>7.4f} "
              f"{rr['recall']:>7.4f} {rr['f1']:>7.4f}")
    print(f"\n按实体类型与完整指标已写入：{args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
