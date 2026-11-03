"""
压力案卷落盘语料装载（工单 02）
================================

磁盘布局（进入 ``data/gold/stress/``，随仓提交，非临时目录）::

    stress_seed20261102.md            干净正文（喂给被评抽取器，无标签）
    stress_seed20261102.answer.json   标准答案 sidecar：
                                     {seed,name,entities:[{name,type}],
                                      relations:[{head,predicate,tail}]}

装载时把规范名在正文中的【每一次字面出现】都建成带 span 的 mention，
这样 strict 口径（type+surface+span）与按块统计都能直接用；
并再次跑 :func:`validate_case` 口径，发现不可定位的答案直接报错——
黄金语料必须自洽，宁可早炸也不拿脏答案算分。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ...schema import Entity, EntityMention, ExtractionResult, Relation, Span
from .corpus import (
    StressCase,
    StressEntity,
    StressRelation,
    validate_case,
)


@dataclass
class StressGoldCase:
    """一篇已落盘压力案卷：正文 + 结构化标准答案 + 可评分 ExtractionResult。"""

    seed: int
    name: str
    text: str
    entities: list[StressEntity]
    relations: list[StressRelation]
    gold: ExtractionResult

    @property
    def doc_id(self) -> str:
        return self.name


def _all_mentions(text: str, surface: str) -> list[EntityMention]:
    mentions: list[EntityMention] = []
    start = 0
    while True:
        idx = text.find(surface, start)
        if idx < 0:
            break
        mentions.append(EntityMention(
            surface=surface,
            span=Span(start=idx, end=idx + len(surface), text=surface)))
        start = idx + len(surface)
    return mentions


def stress_case_to_gold(case: StressCase) -> ExtractionResult:
    """把生成/载入的标准答案转成评测用 ExtractionResult（全字面出现建 mention）。"""
    entities: list[Entity] = []
    for se in case.entities:
        entities.append(Entity(
            name=se.name, type=se.type, confidence=1.0,
            mentions=_all_mentions(case.text, se.name)))
    relations = [
        Relation(head=r.head, predicate=r.predicate, tail=r.tail,
                 confidence=1.0)
        for r in case.relations
    ]
    return ExtractionResult(entities=entities, relations=relations)


def load_stress_case(md_path: str | Path) -> StressGoldCase:
    md_path = Path(md_path)
    text = md_path.read_text(encoding="utf-8")
    answer_path = md_path.with_suffix(".answer.json")
    data = json.loads(answer_path.read_text(encoding="utf-8"))

    entities = [StressEntity(name=e["name"], type=e["type"])
                for e in data.get("entities", [])]
    relations = [StressRelation(head=r["head"], predicate=r["predicate"],
                                tail=r["tail"])
                 for r in data.get("relations", [])]
    case = StressCase(
        seed=int(data["seed"]),
        name=data.get("name", md_path.stem),
        text=text,
        entities=entities,
        relations=relations,
    )
    missing_e, missing_r = validate_case(case)
    if missing_e or missing_r:
        raise ValueError(
            f"压力语料 {md_path.name} 答案不自洽："
            f"缺实体 {missing_e[:3]} 缺关系 {missing_r[:3]}")
    return StressGoldCase(
        seed=case.seed, name=case.name, text=text,
        entities=entities, relations=relations,
        gold=stress_case_to_gold(case))


def load_stress_dir(directory: str | Path) -> list[StressGoldCase]:
    """按文件名稳定顺序装载目录下全部 ``*.md`` 压力案卷。"""
    return [load_stress_case(p)
            for p in sorted(Path(directory).glob("*.md"))]
