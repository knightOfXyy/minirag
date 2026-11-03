"""
压力语料评测口径：L15 同款 P/R/F1 + 按实体类型拆分（工单 02）
=============================================================

严格/宽松与关系三元组的匹配口径**完全复用** :mod:`moray.evaluation.metrics`
（strict=type+surface+span；relaxed=type+规范名；关系=(头,谓词归一,尾)），
本模块只额外提供：

· ``evaluate_case``  单篇产出标准 :class:`EvalReport`；
· ``per_type_prf``   实体按类型分别报 P/R/F1；
· ``aggregate_*``    微平均聚合（跨文档把 TP/FP/FN 相加后一次算分）。

纯计算、不调模型、不连库。
"""

from __future__ import annotations

from dataclasses import dataclass

from ...schema import ExtractionResult
from ..metrics import EvalReport, PRF, entity_keys


@dataclass
class CaseEvaluation:
    name: str
    seed: int
    pred: ExtractionResult
    gold: ExtractionResult
    report: EvalReport


def evaluate_case(name: str, seed: int,
                  pred: ExtractionResult, gold: ExtractionResult) -> CaseEvaluation:
    from ..metrics import evaluate
    return CaseEvaluation(
        name=name, seed=seed, pred=pred, gold=gold,
        report=evaluate(pred, gold))


def _count_keys(pred: ExtractionResult, gold: ExtractionResult,
                etype: str, *, strict: bool) -> tuple[int, int, int]:
    """某一实体类型的 (tp, fp, fn)。strict 用带 span 的 mention key。

    键构造直接复用 L15 的 :func:`entity_keys`，只按类型过滤；避免在此复刻
    strict/relaxed 的键形状，否则换口径时顶层数与按类型数会悄悄分叉。
    """
    def keys_of(result: ExtractionResult) -> set:
        return {key for key in entity_keys(result, strict=strict)
                if key[0] == etype}

    pk, gk = keys_of(pred), keys_of(gold)
    return len(pk & gk), len(pk - gk), len(gk - pk)


def _prf_dict(tp: int, fp: int, fn: int) -> dict:
    return PRF(tp=tp, fp=fp, fn=fn).as_dict()


def aggregate_cases(evaluations: list[CaseEvaluation]) -> dict:
    """跨文档微平均：实体 strict/relaxed + 按类型 relaxed + 关系。"""
    strict = PRF()
    relaxed = PRF()
    relation = PRF()
    for ev in evaluations:
        strict.tp += ev.report.entity_strict.tp
        strict.fp += ev.report.entity_strict.fp
        strict.fn += ev.report.entity_strict.fn
        relaxed.tp += ev.report.entity_relaxed.tp
        relaxed.fp += ev.report.entity_relaxed.fp
        relaxed.fn += ev.report.entity_relaxed.fn
        relation.tp += ev.report.relation.tp
        relation.fp += ev.report.relation.fp
        relation.fn += ev.report.relation.fn

    # 同时收金标与预测的类型：只遍历金标会把"金标里没有的类型"的 FP
    # 丢出按类型表（顶层 entity_relaxed 仍计入），两张表对不上账。
    types = sorted({e.type for ev in evaluations
                    for e in (*ev.gold.entities, *ev.pred.entities)})
    by_type: dict[str, dict] = {}
    for etype in types:
        tp = fp = fn = 0
        for ev in evaluations:
            t, f, miss = _count_keys(
                ev.pred, ev.gold, etype, strict=False)
            tp += t
            fp += f
            fn += miss
        by_type[etype] = _prf_dict(tp, fp, fn)

    return {
        "documents": len(evaluations),
        "entity_strict": strict.as_dict(),
        "entity_relaxed": relaxed.as_dict(),
        "entity_by_type": by_type,
        "relation": relation.as_dict(),
    }


def relation_recall_for_predicates(
    evaluations: list[CaseEvaluation], predicates: set[str]
) -> dict:
    """按谓词子集计算关系召回（供参数 A/B 只看"可被分块影响"的近端关系）。

    谓词按 metrics 的归一表归一后比较；返回 {tp,fn,recall}。
    """
    from ..metrics import norm_name, predicate_norm

    wanted = {predicate_norm(p) for p in predicates}
    tp = fn = 0
    for ev in evaluations:
        pred_keys = {
            (norm_name(r.head), predicate_norm(r.predicate),
             norm_name(r.tail))
            for r in ev.pred.relations
        }
        for r in ev.gold.relations:
            if predicate_norm(r.predicate) not in wanted:
                continue
            key = (norm_name(r.head), predicate_norm(r.predicate),
                   norm_name(r.tail))
            if key in pred_keys:
                tp += 1
            else:
                fn += 1
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    return {"tp": tp, "fn": fn, "recall": round(recall, 4)}


# 便于外部直接取标准 keys（runner 复用）
__all__ = [
    "CaseEvaluation",
    "evaluate_case",
    "aggregate_cases",
    "relation_recall_for_predicates",
]
