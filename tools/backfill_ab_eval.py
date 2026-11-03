"""工单 03 A/B 评测：结构化输出瘦身 + 本地回填（可复现脚本）。

两种模式，口径刻意分开，避免拿规则抽取器自证：

* ``--mode llm``（验收 8/9 的真实证据）：
  同一份黄金语料、同一个模型，分别跑 ``local_backfill=False``（full schema，
  模型自报 mentions/偏移/置信度）与 ``local_backfill=True``（slim schema，
  模型只出逻辑实体/关系，本地回填），比较 macro 实体/关系 F1、span 一致率，
  并从 **真实 usage.output_tokens** 统计单块输出 token 变化。
* ``--mode rule``（仅离线冒烟，不是验收 8 证据）：
  用确定性 ``ImprovedExtractor`` 的逻辑集合做「保留规则 span」vs「剥掉 span
  再本地回填」的结构对照，只证明回填不改变实体/关系集合且 span 可还原。

用法::

    PYTHONPATH=src python tools/backfill_ab_eval.py --mode llm
    PYTHONPATH=src python tools/backfill_ab_eval.py --mode rule

    # 工单 02 扩展压力语料（需 02 的 moray.evaluation.stress loader 已合入；
    # 本分支尚未合入，所以这条命令当前不可执行，只作为合入后的复跑口径）：
    PYTHONPATH=src python tools/backfill_ab_eval.py \
      --gold data/gold/stress --mode llm \
      --json-out docs/evidence/03-stress-ab-llm.json

真实 LLM 模式需要项目 .env 里的模型密钥（默认读取当前环境）。

口径注意：本脚本按「每篇文档一次抽取」统计 output token，``attempts`` 只是
校验重试次数；它们**不是**工单 01 的「单块 output token / 截断块数」指标。
验收 9 必须接 01 的 chunk_metrics（本分支未合入），不能用本脚本的数字冒充。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from moray.evaluation.gold import case_document, load_gold_dir
from moray.evaluation.metrics import EvalReport, evaluate, macro_average
from moray.extraction import Extractor
from moray.extraction.quality import verify_spans


@dataclass
class RunStats:
    name: str
    reports: list[EvalReport] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    attempts: int = 0
    span_ratios: list[float] = field(default_factory=list)
    backfill_grounded: int = 0
    backfill_entities: int = 0

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "macro": macro_average(self.reports),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "attempts": self.attempts,
            "mean_output_tokens_per_case": (
                round(self.output_tokens / len(self.reports), 2)
                if self.reports else 0.0),
            "span_ratio_min": round(min(self.span_ratios), 4) if self.span_ratios else 1.0,
            "span_ratio_mean": (
                round(sum(self.span_ratios) / len(self.span_ratios), 4)
                if self.span_ratios else 1.0),
            "backfill_entities_grounded": self.backfill_grounded,
            "backfill_entities_total": self.backfill_entities,
        }


async def _run_llm(corpus: Path, limit: int | None) -> tuple[RunStats, RunStats]:
    cases = load_gold_dir(corpus)[:limit]
    full = RunStats("full-schema-llm")
    slim = RunStats("slim+local-backfill-llm")
    for case in cases:
        for stats, flag in ((full, False), (slim, True)):
            extractor = Extractor("general", max_retries=1, local_backfill=flag)
            out = await extractor.extract(case_document(case))
            stats.reports.append(evaluate(out.result, case.gold))
            stats.input_tokens += out.usage.input_tokens
            stats.output_tokens += out.usage.output_tokens
            stats.cached_input_tokens += out.usage.cached_input_tokens
            stats.attempts += out.attempts
            stats.span_ratios.append(verify_spans(out.result, case.clean_text).ratio)
            if out.backfill:
                stats.backfill_grounded += out.backfill["entities_grounded"]
                stats.backfill_entities += out.backfill["entities_total"]
    return full, slim


async def _run_rule(corpus: Path, limit: int | None) -> tuple[RunStats, RunStats]:
    """离线结构对照：不是验收 8 的 LLM 证据，脚本里显式标注。"""
    from moray.extraction.local_backfill import backfill_provenance
    from moray.schema import Entity, ExtractionResult, Relation

    cases = load_gold_dir(corpus)[:limit]
    full = RunStats("rule-with-spans")
    slim = RunStats("rule-logical+backfill")
    for case in cases:
        from moray.extraction.baseline import ImprovedExtractor

        logical = await ImprovedExtractor().extract(case_document(case))
        full.reports.append(evaluate(logical, case.gold))
        full.span_ratios.append(
            verify_spans(logical, case.clean_text).ratio)

        stripped = ExtractionResult(
            entities=[
                Entity(name=e.name, type=e.type, aliases=list(e.aliases),
                       attributes=dict(e.attributes))
                for e in logical.entities
            ],
            relations=[
                Relation(head=r.head, predicate=r.predicate, tail=r.tail)
                for r in logical.relations
            ],
        )
        backfilled, report = backfill_provenance(stripped, case.clean_text)
        slim.reports.append(evaluate(backfilled, case.gold))
        slim.span_ratios.append(
            verify_spans(backfilled, case.clean_text).ratio)
        slim.backfill_grounded += report.entities_grounded
        slim.backfill_entities += report.entities_total
    return full, slim


def _print(before: RunStats, after: RunStats, *, mode: str) -> None:
    b, a = before.as_dict(), after.as_dict()
    label = "真实 LLM" if mode == "llm" else "离线规则（仅结构冒烟，非验收 8 证据）"
    print(f"\n===== 工单03 A/B · {label} · documents={b['macro'].get('documents')} =====")
    for metric in ("entity_relaxed", "entity_strict", "relation"):
        bf = b["macro"][metric]["f1"]
        af = a["macro"][metric]["f1"]
        print(f"{metric:15s} before={bf:.4f} after={af:.4f} delta={af - bf:+.4f}")
    print(f"span 一致率 full min/mean={b['span_ratio_min']:.4f}/{b['span_ratio_mean']:.4f}"
          f"  slim min/mean={a['span_ratio_min']:.4f}/{a['span_ratio_mean']:.4f}")
    if mode == "llm":
        print(f"output tokens full={b['output_tokens']} slim={a['output_tokens']}"
              f"  delta={a['output_tokens'] - b['output_tokens']:+d}"
              f" ({(1 - a['output_tokens'] / b['output_tokens']) * 100 if b['output_tokens'] else 0:.1f}% 降)")
        print(f"每篇平均 output tokens full={b['mean_output_tokens_per_case']}"
              f" slim={a['mean_output_tokens_per_case']}"
              "（每篇文档口径，非工单单块口径）")
        print(f"模型尝试次数 full={b['attempts']} slim={a['attempts']}"
              "（校验重试次数，不能代表截断块数；验收 9 需接工单 01 chunk_metrics）")
        print(f"回填命中 full={b['backfill_entities_grounded']}/{b['backfill_entities_total']}"
              f" slim={a['backfill_entities_grounded']}/{a['backfill_entities_total']}")
    else:
        print("说明：before 是规则抽取器自己的 span，after 是同一逻辑集合剥掉 span 后"
              "本地回填；只验证回填不改变实体/关系集合，不能代表 LLM 语义质量。")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("llm", "rule"), default="rule")
    parser.add_argument("--corpus", "--gold", dest="corpus", type=Path,
                        default=Path("data/gold"))
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--json-out", type=Path, default=None)
    args = parser.parse_args()

    if args.mode == "llm":
        before, after = await _run_llm(args.corpus, args.limit)
    else:
        before, after = await _run_rule(args.corpus, args.limit)

    _print(before, after, mode=args.mode)
    if args.json_out:
        payload = {
            "mode": args.mode,
            "corpus": str(args.corpus),
            "full": before.as_dict(),
            "slim": after.as_dict(),
            "delta": {
                metric: round(
                    after.as_dict()["macro"][metric]["f1"]
                    - before.as_dict()["macro"][metric]["f1"], 4)
                for metric in ("entity_relaxed", "entity_strict", "relation")
            },
        }
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nJSON 证据已写入 {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
