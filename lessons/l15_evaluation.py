"""
================================================================================
L15 · 评测与收尾：用精确率/召回率/F1 衡量抽取质量（收官课）
================================================================================

前面 14 课一直在"把系统做出来"，这一课回答一个更重要的问题：
**怎么知道它抽得到底好不好？改一版之后，是变好了还是变差了？**

核心方法：准备一批带【标准答案(ground truth)】的黄金语料（data/gold/），
让抽取器只看干净原文、产出预测，再把预测与标准答案逐条比对：

    精确率 Precision = 抽对的 / 你抽出来的总数   （高 P = 不乱报）
    召回率 Recall    = 抽对的 / 标准答案总数     （高 R = 不遗漏）
    F1               = 二者调和平均，综合分

本脚本做一个完整的"评测驱动改进"演示（全程离线、确定性、零 token）：
    1) BaselineExtractor：后缀/姓氏启发式 NER，不抽关系；
    2) ImprovedExtractor：加领域词典 + 中文模式关系抽取；
    3) 在同一批黄金集上评测两者，打印 P/R/F1 与提升差，导出 JSON 报告。

可选：
    python lessons/l15_evaluation.py --live
        用真实 LLM Extractor 评测（慢、耗 token；需要 .env 配好 ARK）。

    RUN_DB_TESTS=1 pytest tests/test_evaluation.py -q   # 评测测试
================================================================================
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

LIVE = "--live" in sys.argv
GOLD_DIR = ROOT / "data" / "gold"
REPORT = ROOT / "data" / "gold" / "eval_report.json"


def _print_run(title: str, run) -> None:
    micro = run.micro()
    macro = run.macro()
    print(f"\n{'=' * 70}\n{title}：{run.extractor}（{macro['documents']} 篇）\n{'=' * 70}")
    print(f"  {'指标':<16}{'精确率':>10}{'召回率':>10}{'F1':>10}")
    for key, label in (
            ("entity_strict", "实体(严格/含span)"),
            ("entity_relaxed", "实体(宽松/同名)"),
            ("relation", "关系三元组")):
        v = micro[key]
        print(f"  {label:<16}{v['precision']:>10.3f}{v['recall']:>10.3f}"
              f"{v['f1']:>10.3f}   (tp={v['tp']} fp={v['fp']} fn={v['fn']})")


async def main() -> None:
    from moray.evaluation import (
        compare, load_gold_dir, run_evaluation, save_report)
    from moray.extraction.baseline import BaselineExtractor, ImprovedExtractor

    cases = load_gold_dir(GOLD_DIR)
    print(f"载入黄金用例 {len(cases)} 个（{GOLD_DIR}）")
    print("标准答案实体(去重前含多次提及) / 关系：",
          sum(len(c.gold.entities) for c in cases), "/",
          sum(len(c.gold.relations) for c in cases))

    base = await run_evaluation(BaselineExtractor(), cases, name="baseline规则")
    imp = await run_evaluation(ImprovedExtractor(), cases, name="improved规则+词典")
    _print_run("基线", base)
    _print_run("改进", imp)

    print(f"\n{'=' * 70}\n改进前后 macro-F1 对比（评测驱动开发的核心闭环）\n{'=' * 70}")
    for key, label in (
            ("entity_strict", "实体(严格)"),
            ("entity_relaxed", "实体(宽松)"),
            ("relation", "关系")):
        d = compare(base, imp)[key]
        arrow = "↑" if d["delta"] > 0 else "↓"
        print(f"  {label:<12} {d['baseline_f1']:.3f} -> {d['improved_f1']:.3f}"
              f"   {arrow} {d['delta']:+.3f}")

    runs = [base, imp]

    if LIVE:
        print("\n--live：开始评测真实 LLM（可能耗时数十秒）...")
        try:
            from moray.extraction import Extractor
            from moray.extraction.document import Document
            from moray.evaluation.gold import case_document

            llm = Extractor("general", max_retries=1)

            class LLMAdapter:
                async def extract(self, doc: Document):
                    return (await llm.extract(doc)).result

            llm_run = await run_evaluation(LLMAdapter(), cases, name="LLM(ARK)")
            _print_run("真实模型", llm_run)
            runs.append(llm_run)
        except Exception as exc:  # noqa: BLE001
            print("  LLM 评测跳过：", repr(exc)[:160])

    save_report(REPORT, runs)
    print(f"\n报告已写入：{REPORT.relative_to(ROOT)}")

    print("\n解读：")
    print("  · 基线实体 F1 低，主要栽在公司名边界（把'阿里巴巴集团'截断、")
    print("    跨小句吞字）和完全不抽关系；")
    print("  · 改进版实体满分，关系 F1 仍 <1——'马云是创始人''在深圳创立了'")
    print("    '创立之'这几种句式模式覆盖不到，评测如实暴露了规则的天花板；")
    print("  · 这正是评测的价值：改之前先有分数，改完用同一份黄金集回归，")
    print("    用数字而不是感觉决定取舍。")


if __name__ == "__main__":
    asyncio.run(main())
