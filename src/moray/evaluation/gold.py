"""
黄金数据集加载（L15）
=====================

黄金语料用"人民日报风格"的逐词标注（data/gold/*.tagged.txt），同一份文件一物两用：
  · clean_tagged_text 还原出干净原文 —— 喂给被评测的抽取器（它看不到标签）；
  · extract_from_tagged_text 解析出带精确 span 的实体 —— 作为标准答案。

关系标准答案不藏在词性标签里（词性标不出关系），用一个显式的 sidecar JSON
（*.relations.json）描述 (头, 谓词, 尾)，加载时合并进 gold ExtractionResult。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..extraction.document import Document
from ..extraction.rule_extractor import (
    clean_tagged_text,
    extract_from_tagged_text,
)
from ..schema import Relation, Span


@dataclass
class GoldCase:
    doc_id: str
    clean_text: str
    gold: object  # ExtractionResult


def _remap_spans_to_clean(result, clean_text: str) -> None:
    """把 gold 实体的 mention span 重定位到【干净文本】的偏移。

    规则解析给的偏移相对带标签原文（含 /nt、行首 ID），而被测抽取器只看
    clean_text。两者词与词序完全一致（清洗只删标签不删词），因此按"每个实体
    的 surface 在干净文本中第 n 次出现"对齐它的第 n 个 mention 即可。
    """
    for ent in result.entities:
        cursor = 0
        for m in ent.mentions:
            idx = clean_text.find(m.surface, cursor)
            if idx < 0:
                # 干净文本里找不到（理论上不应发生）：丢弃该 mention 的 span
                m.span = None
                continue
            m.span = Span(start=idx, end=idx + len(m.surface), text=m.surface)
            cursor = idx + len(m.surface)


def load_gold_case(path: str | Path) -> GoldCase:
    path = Path(path)
    tagged = path.read_text(encoding="utf-8")
    clean = clean_tagged_text(tagged)
    result = extract_from_tagged_text(tagged)
    # 评测口径统一到干净文本偏移
    _remap_spans_to_clean(result, clean)

    # 可选关系标准答案：同名 .relations.json
    rel_path = path.with_suffix("").with_suffix(".relations.json")
    # with_suffix 对双后缀处理：tagged.txt -> tagged.relations.json 手动拼
    rel_path = path.parent / (path.name.split(".")[0] + ".relations.json")
    if rel_path.exists():
        data = json.loads(rel_path.read_text(encoding="utf-8"))
        for r in data.get("relations", []):
            result.relations.append(Relation(
                head=r["head"], predicate=r["predicate"], tail=r["tail"],
                confidence=1.0))
    return GoldCase(
        doc_id=path.stem,
        clean_text=clean,
        gold=result,
    )


def load_gold_dir(directory: str | Path) -> list[GoldCase]:
    cases = []
    for p in sorted(Path(directory).glob("*.tagged.txt")):
        cases.append(load_gold_case(p))
    return cases


def case_document(case: GoldCase) -> Document:
    return Document(id=case.doc_id, text=case.clean_text,
                    source="gold", domain_code="general")
