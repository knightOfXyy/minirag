"""
离线基线抽取器（L15 评测专用）
==============================

评测需要一个【免费、确定性、可复现】的被评对象，否则每次改进都要烧 LLM token、
数字还会因模型抖动而不可比。这里提供两档确定性抽取器，专门演示评测驱动改进：

  BaselineExtractor   后缀启发式：
      · ORG：扫描「…公司/集团/厂/银行」结尾的片段；
      · PER：常见姓氏 + 1~2 个名字字；
      · LOC：内置地名词典；
      · 不抽关系（很多朴素 NER 流水线就是这样）。
  ImprovedExtractor   在基线之上：
      · 加一份领域专名词典（最长匹配优先）修正/补全实体边界；
      · 用少量中文句式模式抽「总部位于 / 创立 / 子公司」关系。

两者都实现 async extract(Document)->ExtractionResult，与 LLM Extractor 同约定，
因此评测器对"规则 vs LLM"一视同仁。
"""

from __future__ import annotations

import re

from ..schema import Entity, EntityMention, ExtractionResult, Relation, Span
from .document import Document

# 常见单字姓氏（覆盖黄金集人名）
_SURNAMES = set("马王任刘张陈李赵黄周吴徐孙胡朱高林何郭")

# 名字常用字：用于抑制把动词/虚词当名字（传/腾/云/正/非/军…之外的噪声）
_GIVEN_OK = set("化腾云飞正非军传福平晓明强伟刚勇俊杰涛斌超")

# 不能出现在组织名里的"边界字"：标点 + 常见动词/虚词（防止跨小句吞字）
_ORG_STOP = set("，。、；：的了是在位于创立创办创建总部子公司")

# 地名词典（LOC 后缀启发容易漏，直接收常见城市）
_PLACES = ["深圳", "杭州", "贵阳", "北京", "上海", "广州"]

# 组织名后缀：往前吃，遇到标点/动词边界即停
_ORG_SUFFIX = re.compile(r"[一-龥]{2,12}?(?:股份有限公司|有限公司|集团|公司|银行|厂)")

# 领域专名词典（improved 用）：完整规范名 -> 类型
_GAZETTEER = {
    "腾讯控股有限公司": "ORG",
    "阿里巴巴集团控股有限公司": "ORG",
    "华为技术有限公司": "ORG",
    "华为云计算技术有限公司": "ORG",
    "比亚迪股份有限公司": "ORG",
    "马化腾": "PER", "马云": "PER", "任正非": "PER", "王传福": "PER",
    "深圳": "LOC", "杭州": "LOC", "贵阳": "LOC",
}

# 关系句式（improved 用）。按子句切分后逐条尝试。
#   (谓词, 头类型约束, 尾类型约束, 编译好的正则：组1=头，组2=尾)
_REL_PATTERNS = [
    ("总部位于", "ORG", "LOC",
     re.compile(r"(.{2,15}?)总部(?:位于|在)(.{2,8}?)[，。、；]")),
    ("创立", "PER", "ORG",
     re.compile(r"(.{2,5}?)(?:创立|创办|创建)(?:了|之)?(.{2,15}?)[，。、；]")),
    ("子公司", "ORG", "ORG",
     re.compile(r"(.{2,15}?)的子公司(.{2,15}?)总部")),
]


def _mention(text: str, start: int, surface: str) -> EntityMention:
    return EntityMention(
        surface=surface,
        span=Span(start=start, end=start + len(surface), text=surface))


def _find_orgs(text: str) -> list[tuple[int, str]]:
    """后缀扫描 + 边界裁剪：命中后缀后向前扩展，遇到停用字即停。"""
    hits: list[tuple[int, str]] = []
    for m in re.finditer(r"(?:股份有限公司|有限公司|集团|公司|银行|厂)", text):
        end = m.end()
        start = m.start()
        while start > 0 and text[start - 1] not in _ORG_STOP \
                and end - start <= 14 and "一" <= text[start - 1] <= "龥":
            start -= 1
        name = text[start:end]
        if len(name) >= 3 and not any(c in _ORG_STOP for c in name):
            hits.append((start, name))
    return hits


def _dedup_entities(ents: list[Entity]) -> list[Entity]:
    """按 (类型, 名字) 去重，合并 mention。"""
    by_key: dict[tuple, Entity] = {}
    for e in ents:
        k = (e.type, e.name)
        if k in by_key:
            by_key[k].mentions.extend(e.mentions)
        else:
            by_key[k] = e
    return list(by_key.values())


class BaselineExtractor:
    """后缀/姓氏/地名词典启发式，不抽关系。"""

    domain_code = "general"

    async def extract(self, document: Document) -> ExtractionResult:  # noqa: D401
        text = document.text
        ents: list[Entity] = []

        for start, name in _find_orgs(text):
            ents.append(Entity(
                name=name, type="ORG", confidence=0.6,
                mentions=[_mention(text, start, name)]))

        for place in _PLACES:
            start = 0
            while True:
                i = text.find(place, start)
                if i < 0:
                    break
                ents.append(Entity(
                    name=place, type="LOC", confidence=0.7,
                    mentions=[_mention(text, i, place)]))
                start = i + len(place)

        # 人名：姓氏后接 1~2 个汉字，且不能与已识别 ORG/LOC 区间重叠
        occupied = [
            (mm.span.start, mm.span.end) for e in ents for mm in e.mentions]
        for i, ch in enumerate(text):
            if ch in _SURNAMES:
                for length in (3, 2):  # 优先三字姓名
                    cand = text[i:i + length]
                    if len(cand) < length or not all(
                            "一" <= c <= "龥" for c in cand):
                        continue
                    if any(s <= i < e for s, e in occupied):
                        break
                    # 名字用字需常见，且三字名后必须是标点/动词等边界
                    given = cand[1:]
                    if not all(c in _GIVEN_OK for c in given):
                        continue
                    boundary = "，。、；的是在了创总"
                    if length == 2 or (
                            i + length < len(text)
                            and text[i + length] in boundary):
                        ents.append(Entity(
                            name=cand, type="PER", confidence=0.5,
                            mentions=[_mention(text, i, cand)]))
                        break

        return ExtractionResult(
            entities=_dedup_entities(ents), relations=[])


class ImprovedExtractor:
    """基线 + 专名词典（最长匹配）+ 中文模式关系抽取。"""

    domain_code = "general"

    async def extract(self, document: Document) -> ExtractionResult:  # noqa: D401
        text = document.text

        # 1) 词典最长匹配：优先于启发式，保证边界与规范名正确
        ents: list[Entity] = []
        used = [False] * len(text)
        for name, etype in sorted(
                _GAZETTEER.items(), key=lambda kv: -len(kv[0])):
            start = 0
            while True:
                i = text.find(name, start)
                if i < 0:
                    break
                if not any(used[i:i + len(name)]):
                    for j in range(i, i + len(name)):
                        used[j] = True
                    ents.append(Entity(
                        name=name, type=etype, confidence=0.9,
                        mentions=[_mention(text, i, name)]))
                start = i + len(name)

        # 2) 兜底启发式补词典外实体（跳过已被词典覆盖区间）
        for start, name in _find_orgs(text):
            if not any(used[start:start + len(name)]):
                ents.append(Entity(
                    name=name, type="ORG", confidence=0.6,
                    mentions=[_mention(text, start, name)]))

        # 3) 模式抽关系：把【已识别实体名】回填进句式，并用类型约束兜底
        type_of = {e.name: e.type for e in ents}
        names = sorted(type_of, key=len, reverse=True)
        rels: list[Relation] = []
        clauses = re.split(r"(?<=[。！？\n])", text)
        for clause in clauses:
            for predicate, head_type, tail_type, pat in _REL_PATTERNS:
                for rm in pat.finditer(clause):
                    head_raw, tail_raw = rm.group(1), rm.group(2)
                    head = _resolve(head_raw, names)
                    tail = _resolve(tail_raw, names)
                    if (head and tail and head != tail
                            and type_of.get(head) == head_type
                            and type_of.get(tail) == tail_type):
                        rels.append(Relation(
                            head=head, predicate=predicate, tail=tail,
                            confidence=0.7))
        # 关系去重
        seen = {(r.head, r.predicate, r.tail) for r in rels}
        uniq = []
        for r in rels:
            k = (r.head, r.predicate, r.tail)
            if k in seen:
                uniq.append(r)
                seen.discard(k)

        return ExtractionResult(
            entities=_dedup_entities(ents), relations=uniq)


def _resolve(raw: str, names: list[str]) -> str | None:
    """把从句中截出的文本解析成某个已识别实体的规范名。"""
    raw = raw.strip("，。、；的了是在")
    for n in names:  # names 已按长度降序，优先长名
        if n in raw:
            return n
    return None
