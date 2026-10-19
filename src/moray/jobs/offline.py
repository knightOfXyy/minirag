"""离线确定性假抽取器（worker --offline / 多 worker 教学演示用）。

不调模型、不加载嵌入，只按文档里出现的关键词造几个实体/关系，
使多 worker 的"认领-心跳-入库-租约"行为可以零成本反复演示。
"""

from __future__ import annotations

from ..schema import Entity, EntityMention, ExtractionResult, Relation, Span

# 关键词 -> 类型（命中即造一个实体）
_KEYWORDS = [
    ("腾讯", "ORG"), ("阿里巴巴", "ORG"), ("华为", "ORG"), ("比亚迪", "ORG"),
    ("深圳", "LOC"), ("杭州", "LOC"), ("贵阳", "LOC"), ("北京", "LOC"),
    ("马化腾", "PER"), ("马云", "PER"), ("任正非", "PER"), ("王传福", "PER"),
]


async def fake_extractor(text: str, feedback: str | None = None) -> ExtractionResult:
    entities: list[Entity] = []
    seen: set[tuple[str, str]] = set()
    for kw, etype in _KEYWORDS:
        start = 0
        mentions = []
        while True:
            i = text.find(kw, start)
            if i < 0:
                break
            mentions.append(EntityMention(
                surface=kw, span=Span(start=i, end=i + len(kw), text=kw)))
            start = i + len(kw)
        if mentions and (kw, etype) not in seen:
            seen.add((kw, etype))
            entities.append(Entity(
                name=kw, type=etype, confidence=0.9, mentions=mentions))

    relations: list[Relation] = []
    if "腾讯" in text and "深圳" in text:
        relations.append(Relation(
            head="腾讯", predicate="总部位于", tail="深圳", confidence=0.8))
    return ExtractionResult(entities=entities, relations=relations)
