#!/usr/bin/env python3
"""
确定性压力案卷黄金语料生成入口（工单 02）
==========================================

把固定种子（默认 STRESS_SEEDS 三个）各生成一篇超过生产块预算 5 倍的长案卷，
连同「带类型实体清单 + 关系三元组标准答案」写入评测数据目录
``data/gold/stress/``（不是临时目录），随仓提交做离线回归。

标准答案在生成时已经过两道程序过滤（正文未出现的实体/头尾不可定位的关系
一律剔除），装载时 validate_case 会再校验一遍。

用法（在仓根，PYTHONPATH 指向 src）::

    PYTHONPATH=src python3 lessons/gen_stress_gold.py
    PYTHONPATH=src python3 lessons/gen_stress_gold.py --out-dir data/gold/stress
    PYTHONPATH=src python3 lessons/gen_stress_gold.py --seed 20261102 --name custom

同一种子两次运行产物逐字节一致（确定性由 random.Random(seed) 与固定顺序保证）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from moray.evaluation.stress import (  # noqa: E402
    STRESS_SEEDS,
    generate_case,
    serialize_case,
    validate_case,
)
from moray.extraction.chunking import estimate_tokens  # noqa: E402


def write_case(out_dir: Path, seed: int, name: str | None,
               target_chars: int, max_units: int) -> None:
    case = generate_case(
        seed=seed, target_chars=target_chars,
        max_units=max_units, name=name)
    missing_e, missing_r = validate_case(case)
    if missing_e or missing_r:
        raise SystemExit(
            f"seed {seed} 答案自洽校验失败：{missing_e[:3]} {missing_r[:3]}")
    # 规模护栏：黄金语料必须显著超过生产块预算，否则块预算 A/B 无信号。
    units = estimate_tokens(case.text)
    if units <= 5 * max_units:
        raise SystemExit(
            f"seed {seed} 正文仅 {units} units，不足 {max_units} 的 5 倍"
            f"（{5 * max_units}）——请增大 --target-chars，"
            f"或确认 --max-units 是否设得过大")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{case.name}.md").write_text(case.text, encoding="utf-8")
    (out_dir / f"{case.name}.answer.json").write_text(
        json.dumps(serialize_case(case), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    print(f"  {case.name}.md（{len(case.text)} 字符）/ .answer.json"
          f"  实体 {len(case.entities)} 关系 {len(case.relations)}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    p.add_argument("--out-dir", type=Path,
                   default=Path("data/gold/stress"))
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--seeds", type=int, nargs="+",
                      default=list(STRESS_SEEDS),
                      help="一个或多个固定种子（默认 STRESS_SEEDS 三个）")
    mode.add_argument("--seed", type=int, default=None,
                      help="只生成单篇时用（等价于 --seeds seed，可配 --name）")
    p.add_argument("--name", default=None,
                   help="单篇模式的文件名前缀（必须与 --seed 一起用）")
    p.add_argument("--target-chars", type=int, default=12000)
    p.add_argument("--max-units", type=int, default=1500,
                   help="hard_split 压力区参考的生产块预算（默认 1500）")
    args = p.parse_args(argv)

    if args.name is not None and args.seed is None:
        p.error("--name 只在单篇模式（--seed）下生效，不能与 --seeds 混用")
    if args.seed is not None:
        seeds = [args.seed]
    else:
        seeds = args.seeds
    print(f"写入 {args.out_dir}（{len(seeds)} 篇，target_chars="
          f"{args.target_chars}）：")
    for seed in seeds:
        name = args.name if args.seed is not None else None
        write_case(args.out_dir, seed, name,
                   args.target_chars, args.max_units)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
