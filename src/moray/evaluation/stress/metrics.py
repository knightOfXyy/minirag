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
from ..metrics import EvalReport, PRF, entity_keys, norm_name, predicate_norm
from ._text import occurrences

# 评审 r1 B2：模型会把「类型前缀标签」并进实体名（`案件编号 A2024-942`），
# gold 只存规范名（`A2024-942`）。这是**类型前缀标签**问题，不是单个案号
# 特判：按实体类型声明受控标签，比较前统一剥掉。规则只影响 relaxed 口径
# 的 name；strict 仍比 mention.surface，不能用别名把原文差异抹平。
TYPE_LABEL_PREFIXES: dict[str, tuple[str, ...]] = {
    "EVENT": ("案件编号", "案号", "编号"),
}


def normalize_entity_name_aliases(name: str, etype: str) -> str:
    """剥掉属于该实体类型的受控前缀标签，返回规范化名字。"""
    for label in TYPE_LABEL_PREFIXES.get(etype, ()):
        if name.startswith(label):
            rest = name[len(label):].lstrip(" \t:：-—")
            if rest:
                return rest
    return name


def _normalize_relation_endpoint(
    name: str, entity_types: dict[str, str]
) -> str:
    """按端点解析到的实体类型剥前缀；类型查不到时只回退到**文档中实际
    出现过的声明类型**。

    评审 r4 N6：旧实现对前缀表做全表回退，会把任何非 EVENT 端点的
    ``编号 X`` 也剥成 ``X``，未来加类型/换语料时可能造出假 TP。限定为
    文档里真正出现过的类型后，没有 EVENT 实体的文档不会被 EVENT 前缀规则
    误伤。
    """
    etype = entity_types.get(name)
    if etype is not None:
        return normalize_entity_name_aliases(name, etype)
    present_types = set(entity_types.values())
    for etype, labels in TYPE_LABEL_PREFIXES.items():
        if etype not in present_types:
            continue
        for label in labels:
            if name.startswith(label):
                rest = name[len(label):].lstrip(" \t:：-—")
                if rest:
                    return rest
    return name


def apply_type_label_aliases(
    result: ExtractionResult, *, normalize_relations: bool = True
) -> ExtractionResult:
    """返回实体名（以及关系头/尾）已按类型前缀标签归一化的副本。

    评审 r2 F1：3500 档模型把案号写成 ``案件编号 A2024-942`` 时，实体侧
    剥了前缀而关系侧没剥，导致 37.6% 的预测关系永远匹配不上 gold。这里用
    关系端点解析到的实体类型归一化；端点不在实体列表时按 ``TYPE_LABEL_PREFIXES``
    全表回退（评审给出的最小修法）。

    ``mentions`` 原样保留：strict 口径比 ``mention.surface``，不能用别名把
    原文差异抹平。
    """
    clone = result.model_copy(deep=True)
    for entity in clone.entities:
        entity.name = normalize_entity_name_aliases(entity.name, entity.type)
    if normalize_relations:
        entity_types: dict[str, str] = {}
        for entity in result.entities:
            entity_types.setdefault(entity.name, entity.type)
        for relation in clone.relations:
            relation.head = _normalize_relation_endpoint(
                relation.head, entity_types)
            relation.tail = _normalize_relation_endpoint(
                relation.tail, entity_types)
    return clone


_SENTENCE_DELIMS = "。！？!?"


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    """句末标点/换行为界，返回非空句子在原文中的字符区间。"""
    spans: list[tuple[int, int]] = []
    start = 0
    for idx, ch in enumerate(text):
        if ch in _SENTENCE_DELIMS:
            end = idx + 1
            if text[start:end].strip():
                spans.append((start, end))
            start = end
        elif ch == "\n":
            if text[start:idx].strip():
                spans.append((start, idx))
            start = idx + 1
    if text[start:].strip():
        spans.append((start, len(text)))
    return spans


def gold_covered_sentence_spans(
    text: str, gold: ExtractionResult
) -> list[tuple[int, int]]:
    """gold 标注覆盖到的句子：至少一条 gold 关系的头与尾同时出现在该句内。"""
    sentences = _sentence_spans(text)
    covered: list[tuple[int, int]] = []
    for relation in gold.relations:
        heads = occurrences(text, relation.head)
        tails = occurrences(text, relation.tail)
        for start, end in sentences:
            if (any(start <= h < end for h in heads)
                    and any(start <= t < end for t in tails)):
                if (start, end) not in covered:
                    covered.append((start, end))
    return covered


def _relation_in_spans(relation, text: str,
                       spans: list[tuple[int, int]]) -> bool:
    """预测关系是否落在给定句面内（优先证据 span，其次头/尾同句共现）。"""
    evidence = getattr(relation, "evidence_span", None)
    if evidence is not None:
        for start, end in spans:
            if evidence.start < end and evidence.end > start:
                return True
    heads = occurrences(text, relation.head)
    tails = occurrences(text, relation.tail)
    return any(
        any(start <= h < end for h in heads)
        and any(start <= t < end for t in tails)
        for start, end in spans
    )


def _relation_key(relation) -> tuple[str, str, str]:
    return (norm_name(relation.head), predicate_norm(relation.predicate),
            norm_name(relation.tail))


@dataclass
class RelationCoverage:
    """覆盖子集口径的完整统计（评审 r2 F13：未覆盖 gold 关系不能静默消失）。"""

    prf: PRF
    covered_sentences: int
    gold_relations_total: int
    uncovered_gold_relations: int


def relation_covered_stats(
    text: str, pred: ExtractionResult, gold: ExtractionResult
) -> RelationCoverage:
    """关系「金标覆盖子集」口径：只统计 gold 标注覆盖句上的 TP/FP/FN。

    未标注句面上的预测不进入子集（它们仍是全量口径的 FP，两个口径并列
    列报）。金标关系的召回分母仍是**全部**被覆盖的 gold 关系——预测落在
    标注句之外不能把漏报“过滤掉”。

    评审 r2 F13：跨句 gold 关系既不在 covered spans，也不进 recall 分母；
    这里单独返回 ``uncovered_gold_relations``，聚合层会把它列报出来，不再
    静默丢失。
    """
    spans = gold_covered_sentence_spans(text, gold)
    covered_gold_relations = [
        r for r in gold.relations if _relation_in_spans(r, text, spans)
    ]
    gold_keys = {_relation_key(r) for r in covered_gold_relations}
    pred_keys = {
        _relation_key(r) for r in pred.relations
        if _relation_in_spans(r, text, spans)
    }
    tp = len(pred_keys & gold_keys)
    return RelationCoverage(
        prf=PRF(tp=tp, fp=len(pred_keys - gold_keys),
                fn=len(gold_keys - pred_keys)),
        covered_sentences=len(spans),
        gold_relations_total=len(gold.relations),
        uncovered_gold_relations=(
            len(gold.relations) - len(covered_gold_relations)),
    )


@dataclass
class CaseEvaluation:
    name: str
    seed: int
    pred: ExtractionResult
    gold: ExtractionResult
    report: EvalReport
    relation_covered: PRF | None = None
    gold_covered_sentences: int = 0
    uncovered_gold_relations: int = 0


def evaluate_case(name: str, seed: int,
                  pred: ExtractionResult, gold: ExtractionResult,
                  *, text: str | None = None,
                  normalize_type_labels: bool = False) -> CaseEvaluation:
    """单篇评测。

    ``normalize_type_labels`` 默认 **False**：保持工单 02/03 的既有口径。
    只有工单 04 的预算 A/B 显式传 True，才启用类型前缀标签归一化（评审
    r2 F4：不能让评测口径静默改变历史基线）。
    """
    from ..metrics import evaluate

    if normalize_type_labels:
        pred = apply_type_label_aliases(pred)
        gold = apply_type_label_aliases(gold)
    report = evaluate(pred, gold)
    covered = None
    covered_sentences = 0
    uncovered_gold_relations = 0
    if text is not None:
        stats = relation_covered_stats(text, pred, gold)
        covered = stats.prf
        covered_sentences = stats.covered_sentences
        uncovered_gold_relations = stats.uncovered_gold_relations
    return CaseEvaluation(
        name=name, seed=seed, pred=pred, gold=gold, report=report,
        relation_covered=covered,
        gold_covered_sentences=covered_sentences,
        uncovered_gold_relations=uncovered_gold_relations)


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
    covered = PRF()
    covered_documents = 0
    gold_covered_sentences = 0
    uncovered_gold_relations = 0
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
        gold_covered_sentences += ev.gold_covered_sentences
        uncovered_gold_relations += ev.uncovered_gold_relations
        if ev.relation_covered is not None:
            covered_documents += 1
            covered.tp += ev.relation_covered.tp
            covered.fp += ev.relation_covered.fp
            covered.fn += ev.relation_covered.fn

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

    out = {
        "documents": len(evaluations),
        "entity_strict": strict.as_dict(),
        "entity_relaxed": relaxed.as_dict(),
        "entity_by_type": by_type,
        "relation": relation.as_dict(),
    }
    if covered_documents:
        out["relation_covered"] = covered.as_dict()
        out["gold_covered_sentences"] = gold_covered_sentences
        out["uncovered_gold_relations"] = uncovered_gold_relations
    return out


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
    "TYPE_LABEL_PREFIXES",
    "CaseEvaluation",
    "RelationCoverage",
    "apply_type_label_aliases",
    "evaluate_case",
    "aggregate_cases",
    "gold_covered_sentence_spans",
    "normalize_entity_name_aliases",
    "relation_covered_stats",
    "relation_recall_for_predicates",
]
