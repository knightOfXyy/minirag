"""抽取质量探针（L04 下，L06/L15 会扩展成完整指标体系）。

目前做一件"零成本但很有效"的事：校验模型给的字符偏移 span 是否真的对。
模型偶尔会把 start/end 数错，或者 span.text 与原文对不上——这类错误
人不看原文很难发现，但程序可以 100% 自动核对。

返回一致率，不直接判定整次抽取失败（个别 mention 缺失可以容忍），
但一致率过低就是明确的危险信号，可触发重抽或人工复核。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..schema import Entity, ExtractionResult


@dataclass
class SpanReport:
    total: int = 0
    matched: int = 0
    mismatches: list[dict] = field(default_factory=list)

    @property
    def ratio(self) -> float:
        return self.matched / self.total if self.total else 1.0

    @property
    def ok(self) -> bool:
        return self.matched == self.total


def _check_one_span(text: str, surface: str, span, where: str) -> dict | None:
    """核对一个 span；返回不匹配详情，匹配则返回 None。"""
    if span is None:
        return {"where": where, "surface": surface, "reason": "缺少 span"}
    # 边界合法性
    if not (0 <= span.start <= span.end <= len(text)):
        return {
            "where": where, "surface": surface,
            "reason": f"越界 [{span.start}:{span.end}]，文本长度 {len(text)}",
        }
    sliced = text[span.start:span.end]
    if sliced != span.text:
        return {
            "where": where, "surface": surface,
            "reason": f"span.text 与原文不符：记录「{span.text}」实际「{sliced}」",
        }
    if sliced != surface:
        return {
            "where": where, "surface": surface,
            "reason": f"偏移处文本「{sliced}」与 mention.surface「{surface}」不符",
        }
    return None


def verify_spans(result: ExtractionResult, text: str) -> SpanReport:
    """核对所有实体 mention 与关系证据的字符偏移。"""
    report = SpanReport()

    def _visit_entities(entities: list[Entity]) -> None:
        for e in entities:
            for m in e.mentions:
                report.total += 1
                bad = _check_one_span(text, m.surface, m.span, f"实体「{e.name}」")
                if bad:
                    report.mismatches.append(bad)
                else:
                    report.matched += 1

    _visit_entities(result.entities)

    for r in result.relations:
        if r.evidence_span is not None:
            report.total += 1
            bad = _check_one_span(
                text, r.evidence_span.text, r.evidence_span,
                f"关系 {r.head}-{r.predicate}->{r.tail}",
            )
            if bad:
                report.mismatches.append(bad)
            else:
                report.matched += 1

    return report
