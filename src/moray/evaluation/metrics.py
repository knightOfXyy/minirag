"""
评测：精确率 / 召回率 / F1（L15）
=================================

为什么需要评测？
  "感觉抽得还行"无法支撑改进。必须有一批【带标准答案（ground truth）】的
  黄金语料，把任意抽取器的输出与标准答案逐条比对，用精确率/召回率/F1 量化：

    精确率 Precision = 抽对的 / 抽取器输出的总数     （宁滥毋缺？高 P=不乱报）
    召回率 Recall    = 抽对的 / 标准答案总数         （高 R=不遗漏）
    F1               = 2PR/(P+R)，两者的调和平均

实体匹配口径（两级，由严到宽）：
  · strict  (type, surface, span 起止) 全相等 —— 连位置都对才算对，最严格；
  · relaxed (type, 规范化名字) 相等 —— 允许位置/写法差异，只看"找没找到这个实体"。
  关系匹配：(头规范名, 谓词归一, 尾规范名) 三元组相等（strict 还要求证据 span 对）。

本模块只做纯计算，不调模型、不连库，因此黄金集与预测集都能离线确定性评测。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..schema import ExtractionResult


@dataclass
class PRF:
    tp: int = 0
    fp: int = 0
    fn: int = 0

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if (self.tp + self.fp) else 0.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if (self.tp + self.fn) else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    def as_dict(self) -> dict:
        return {
            "tp": self.tp, "fp": self.fp, "fn": self.fn,
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
        }


def norm_name(s: str) -> str:
    """名字归一：去空白/标点、转小写，弱化「有限公司」等写法差异。"""
    s = re.sub(r"[\s，。、“”\"'()（）]", "", s).lower()
    return s


def predicate_norm(p: str) -> str:
    """谓词归一：把近义谓词收敛到少数规范关系，便于跨抽取器比较。

    公开 API：压力语料评测按谓词子集算召回时复用它，避免跨模块依赖私有名。
    """
    p = norm_name(p)
    syn = {
        "坐落于": "总部位于", "位于": "总部位于", "总部在": "总部位于",
        "在": "总部位于", "创办": "创立", "创建": "创立", "成立": "创立",
        "创始人是": "创立",
    }
    return syn.get(p, p)


def entity_keys(result: ExtractionResult, *, strict: bool) -> set:
    keys = set()
    for e in result.entities:
        if strict:
            # 用每个 mention 的严格位置；一个实体多个 mention 产生多把 key
            for m in e.mentions:
                if m.span is not None:
                    keys.add((e.type, norm_name(m.surface),
                              m.span.start, m.span.end))
        else:
            keys.add((e.type, norm_name(e.name)))
    return keys


def relation_keys(result: ExtractionResult, *, strict: bool) -> set:
    keys = set()
    for r in result.relations:
        base = (norm_name(r.head), predicate_norm(r.predicate),
                norm_name(r.tail))
        if strict and r.evidence_span is not None:
            base = base + (r.evidence_span.start, r.evidence_span.end)
        keys.add(base)
    return keys


def _prf(pred: set, gold: set) -> PRF:
    tp = len(pred & gold)
    return PRF(tp=tp, fp=len(pred - gold), fn=len(gold - pred))


@dataclass
class EvalReport:
    entity_strict: PRF = field(default_factory=PRF)
    entity_relaxed: PRF = field(default_factory=PRF)
    relation: PRF = field(default_factory=PRF)

    def as_dict(self) -> dict:
        return {
            "entity_strict": self.entity_strict.as_dict(),
            "entity_relaxed": self.entity_relaxed.as_dict(),
            "relation": self.relation.as_dict(),
        }


def evaluate(pred: ExtractionResult, gold: ExtractionResult) -> EvalReport:
    """对单篇：比较预测结果与黄金标准。"""
    return EvalReport(
        entity_strict=_prf(entity_keys(pred, strict=True),
                           entity_keys(gold, strict=True)),
        entity_relaxed=_prf(entity_keys(pred, strict=False),
                            entity_keys(gold, strict=False)),
        relation=_prf(relation_keys(pred, strict=False),
                      relation_keys(gold, strict=False)),
    )


def macro_average(reports: list[EvalReport]) -> dict:
    """宏平均：每篇先算指标，再对篇数取平均（每篇等权，避免长文主导）。"""
    if not reports:
        return {}

    def avg(attr):
        return round(sum(getattr(r, attr).f1 for r in reports) / len(reports), 4)

    def avg_field(attr, field_name):
        return round(sum(getattr(r, attr).__dict__[field_name]
                         for r in reports) / len(reports), 4)

    out = {}
    for attr in ("entity_strict", "entity_relaxed", "relation"):
        out[attr] = {
            "precision": round(
                sum(getattr(r, attr).precision for r in reports) / len(reports), 4),
            "recall": round(
                sum(getattr(r, attr).recall for r in reports) / len(reports), 4),
            "f1": round(
                sum(getattr(r, attr).f1 for r in reports) / len(reports), 4),
        }
    out["documents"] = len(reports)
    return out
