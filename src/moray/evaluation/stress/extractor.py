"""
压力语料的确定性分块抽取器（工单 02）
====================================

评测需要一个【免费、确定性、可复现】且**真的走分块**的被评对象：
``moray.extraction.baseline`` 的两档启发式是整篇扫描的，块预算怎么调它的
结果都不变，量不出切块/回填改动的影响。这里的 ``StressRuleExtractor``：

  1. 用生产同款 :class:`~moray.extraction.chunking.Chunker` 按
     ``(max_units, overlap_units)`` 把正文切块；
  2. 对**每个块单独**跑后缀/词典/句式启发式（它看不到块外文字，和 LLM 一样）；
  3. 块内 span 经 :func:`remap_spans` 平移成全局偏移，再 :func:`naive_merge`
     按规范名归并、三元组去重。

因此当一条关系的头、尾从不同块共现，本抽取器**抽不到它**（naive_merge
从不跨块造关系）——这正是「调大块预算应改善关系召回」的确定性靶子。
零模型请求：全部规则均在本地，无网络/无 SDK 依赖。
"""

from __future__ import annotations

import re

from ...extraction.chunking import Chunker
from ...extraction.document import Document
from ...extraction.pipeline import naive_merge, remap_spans
from ...schema import Entity, EntityMention, ExtractionResult, Relation, Span
from .corpus import (
    CLUE_ITEMS, GIVEN, LOC_POOL, PREDICATE_TRIGGERS, SURNAMES)

# 组织后缀。必须把「集团有限公司」排在「集团」之前，否则非贪婪前缀会在
# 「…实业集团」处先命中「集团」，把规范名截断（「有限公司」同理要长后缀在前）。
_ORG_SUFFIX = re.compile(
    r"[一-龥]{2,18}?(?:股份有限公司|有限责任公司|集团有限公司|有限公司|集团)")
# 案号：A2024-001
_CASE_RE = re.compile(r"A\d{4}-\d{3}")
# 物证线索：与语料生成器共用同一张短语表（出现即收，类型 MISC）
_CLUES = list(CLUE_ITEMS)
# 地名：与语料生成器共用同一份地名池（词典直配，LOC）
_PLACES = list(LOC_POOL)
# 常见单字姓氏（用于 PER 启发）
_SURNAMES = set(SURNAMES)
# PER 名字用字：抑制把动词/虚词当名字
_GIVEN_OK = set("".join(GIVEN))
# 组织名边界字：只用于向前扩边界时停字（**不得**用来否决整名——
# 「任」是「有限责任公司」的合法构成字，整名回绝会把这类单位全部丢掉）。
_ORG_STOP = set("，。、；：的了是位户籍曾任职")
# 句首残留的动词/虚词：允许出现在正则匹配的最前面，但必须正向裁掉
_ORG_LEAD_TRIM = set("在曾前往与和到把被对")
# 组织名里不允许出现的字符（标点/空白）；用于替代旧的"停字整名回绝"
_ORG_PUNCT = set("，。、；：！？\n\r \t（）()「」《》")

# 每个 gold 谓词 -> (左触发, 右触发, 头类型, 尾类型)。
# 一张表同时表达句面触发与类型约束；左触发词统一取 ``corpus.PREDICATE_TRIGGERS``
# （生成器写句、测试语义闸门共用同一来源，文案不会三处漂移）。
# 注意 gold 的规范谓词不一定逐字出现：供职于在案卷里写作"曾在 X 任职"。
_PREDICATE_SPECS: dict[str, tuple[str, str | None, str, str]] = {
    "供职于": (PREDICATE_TRIGGERS["供职于"], "任职", "PER", "ORG"),
    "户籍在": (PREDICATE_TRIGGERS["户籍在"], None, "PER", "LOC"),
    "注册地位于": (PREDICATE_TRIGGERS["注册地位于"], None, "ORG", "LOC"),
    "涉案关联": (PREDICATE_TRIGGERS["涉案关联"], None, "PER", "EVENT"),
    "关联物证": (PREDICATE_TRIGGERS["关联物证"], None, "PER", "MISC"),
    "办案地位于": (PREDICATE_TRIGGERS["办案地位于"], None, "ORG", "LOC"),
    "追赃方向指向": (PREDICATE_TRIGGERS["追赃方向指向"], None, "ORG", "LOC"),
}

# 案卷里"涉案关联"的句面是"案件编号 A-… 涉及 张三、李四 等人员"，头在案号
# 之后、人员并列里，需要单独处理；通用 (left,right) 切分覆盖其余谓词。


def _mention(rel_start: int, surface: str) -> EntityMention:
    return EntityMention(
        surface=surface,
        span=Span(start=rel_start, end=rel_start + len(surface), text=surface))


def _find_orgs(text: str) -> list[tuple[int, str]]:
    hits: list[tuple[int, str]] = []
    for m in _ORG_SUFFIX.finditer(text):
        start, end = m.start(), m.end()
        # 向前吃成前缀，遇标点/动词边界停；随后再把"残留的停用字前缀"
        # （正则从停用字之后才起算，如"曾在XX公司"整体被匹配）正向裁掉。
        while start > 0 and text[start - 1] not in _ORG_STOP \
                and end - start <= 18 and "一" <= text[start - 1] <= "龥":
            start -= 1
        # 正向裁掉句首残留的动词/虚词（如"曾在XX公司"里的"曾在"）
        trim = _ORG_STOP | _ORG_LEAD_TRIM
        while start < end and text[start] in trim:
            start += 1
        name = text[start:end]
        # 只按"标点/空白"否决（合法单位名内部可以含"任/职/位"等停字）
        if len(name) >= 4 and not any(c in _ORG_PUNCT for c in name):
            hits.append((start, name))
    return hits


def _find_persons(text: str,
                  occupied: list[tuple[int, int]]) -> list[tuple[int, str]]:
    """姓氏 + 2 个名字字，边界受标点/动词约束，且不与已识别区间重叠。"""
    out: list[tuple[int, str]] = []
    boundary = set("，。、；：的，男女身份证户籍地曾在任职")
    for i, ch in enumerate(text):
        if ch not in _SURNAMES:
            continue
        cand = text[i:i + 3]
        if len(cand) < 3 or any(c not in _GIVEN_OK for c in cand[1:]):
            continue
        if any(s <= i < e for s, e in occupied):
            continue
        after = text[i + 3] if i + 3 < len(text) else "。"
        if after in boundary:
            out.append((i, cand))
    return out


def extract_chunk(chunk_text: str) -> ExtractionResult:
    """对单个块文本跑启发式，span 为【块内偏移】（与 LLM per-chunk 同契约）。

    上层用 :func:`remap_spans` 平移成全文全局偏移。
    """
    entities: list[Entity] = []
    occupied: list[tuple[int, int]] = []

    def add(name: str, etype: str, start: int, conf: float) -> None:
        entities.append(Entity(
            name=name, type=etype, confidence=conf,
            mentions=[_mention(start, name)]))
        occupied.append((start, start + len(name)))

    # ORG：后缀扫描
    for start, name in _find_orgs(chunk_text):
        if not any(s <= start < e for s, e in occupied):
            add(name, "ORG", start, 0.6)
    # LOC：地名词典最长匹配
    for place in sorted(_PLACES, key=len, reverse=True):
        cursor = 0
        while True:
            idx = chunk_text.find(place, cursor)
            if idx < 0:
                break
            start = idx
            if not any(s <= start < e for s, e in occupied):
                add(place, "LOC", start, 0.8)
            cursor = idx + len(place)
    # EVENT：案号正则
    for m in _CASE_RE.finditer(chunk_text):
        add(m.group(0), "EVENT", m.start(), 0.9)
    # MISC：物证短语
    for clue in _CLUES:
        cursor = 0
        while True:
            idx = chunk_text.find(clue, cursor)
            if idx < 0:
                break
            add(clue, "MISC", idx, 0.8)
            cursor = idx + len(clue)
    # PER：姓氏启发（跳过已被其他实体占据的区间）
    for start, name in _find_persons(chunk_text, occupied):
        if not any(s <= start < e for s, e in occupied):
            add(name, "PER", start, 0.5)

    # 关系：同句内头/尾都在本块可见时才成立（evidence 用块内偏移，不强制保留）
    type_of = {e.name: e.type for e in entities}
    names = sorted(type_of, key=len, reverse=True)
    relations: list[Relation] = []
    for raw_sent in re.findall(r"[^。！？\n]+[。！？\n]?", chunk_text):
        if not raw_sent.strip():
            continue
        relations.extend(
            _relations_in_sentence(raw_sent, names, type_of))

    return ExtractionResult(entities=entities, relations=relations)


def _relations_in_sentence(sent: str, names: list[str],
                           type_of: dict[str, str]) -> list[Relation]:
    """在一个句内、且仅依据该句可见的已识别实体抽取关系。

    每个 gold 谓词对应一个"左触发词"：触发词前窗口找头、后窗口找尾；
    「供职于」写作"曾在 X 任职"，用右触发词"任职"截尾窗口。
    句中找不到对应类型的实体则放弃（不跨句、不访问块外文本）。
    """
    out: list[Relation] = []
    seen: set[tuple[str, str, str]] = set()

    def emit(head: str | None, tail: str | None, predicate: str) -> None:
        if not head or not tail or head == tail:
            return
        key = (head, predicate, tail)
        if key in seen:
            return
        if not _type_ok(type_of.get(head), predicate, type_of.get(tail)):
            return
        seen.add(key)
        out.append(Relation(head=head, predicate=predicate, tail=tail,
                            confidence=0.7))

    for predicate, (left, right, head_type, tail_type) in _PREDICATE_SPECS.items():
        cursor = 0
        while True:
            li = sent.find(left, cursor)
            if li < 0:
                break
            head_win = sent[max(0, li - 48):li]
            tail_start = li + len(left)
            tail_end = len(sent)
            if right is not None:
                ri = sent.find(right, tail_start)
                if ri >= 0:
                    tail_end = ri
            tail_win = sent[tail_start:tail_end]
            head = _resolve_typed(head_win, names, type_of, head_type)
            if head is None:
                # 块预算敏感长句把主语放在句首、触发词+尾放在句末（相距
                # ~1900 units），48 字左窗必然看不到头。回退到句内**最近的
                # 前置**同类型实体：3500 档整句可见时能锁到句首主语；
                # 1500 档硬切后句首主语在另一块，这里仍应返回 None（关系
                # 继续丢失，块预算信号不被抹平）。
                head = _resolve_typed_nearest(
                    sent[:li], names, type_of, head_type)
            tail = _resolve_typed(tail_win, names, type_of, tail_type)
            emit(head, tail, predicate)
            cursor = li + len(left)

    # 涉案关联的特殊句面："案件编号 A-… 涉及 张三、李四 等人员"
    # gold 方向为 (人员 PER, 涉案关联, 案号 EVENT)，取案号后第一个 PER 为头。
    if "案件编号" in sent:
        m = _CASE_RE.search(sent)
        if m:
            tail = m.group(0)
            head_win = sent[m.end():m.end() + 60]
            head = _resolve_typed(head_win, names, type_of, "PER")
            emit(head, tail, "涉案关联")
    return out


def _type_ok(head_type: str | None, predicate: str,
             tail_type: str | None) -> bool:
    _, _, expect_head, expect_tail = _PREDICATE_SPECS[predicate]
    return head_type == expect_head and tail_type == expect_tail


def _resolve_typed(window: str, names: list[str], type_of: dict[str, str],
                   want_type: str) -> str | None:
    """在窗口里找最早出现的指定类型实体；同一位置取最长名（最完整规范名）。"""
    best_pos: int | None = None
    best_name: str | None = None
    for n in names:
        if type_of.get(n) != want_type:
            continue
        idx = window.find(n)
        if idx < 0:
            continue
        if best_pos is None or idx < best_pos or (
                idx == best_pos and len(n) > len(best_name)):
            best_pos, best_name = idx, n
    return best_name


def _resolve_typed_nearest(prefix: str, names: list[str],
                           type_of: dict[str, str],
                           want_type: str) -> str | None:
    """在 ``prefix`` 里找**最近的前置**指定类型实体（取最靠右的出现）。

    长句回退用：头在句首、触发词在句末时，48 字左窗为空，只有回看整句前缀
    才能找到主语。同位置仍取最长名；只回看前缀，绝不跨句或访问块外文本。
    """
    best_pos: int | None = None
    best_name: str | None = None
    for n in names:
        if type_of.get(n) != want_type:
            continue
        idx = prefix.rfind(n)
        if idx < 0:
            continue
        if best_pos is None or idx > best_pos or (
                idx == best_pos and len(n) > len(best_name)):
            best_pos, best_name = idx, n
    return best_name


class StressRuleExtractor:
    """生产同款分块 + 块内规则抽取 + 全局偏移归并的确定性被评对象。"""

    domain_code = "general"
    # 报告身份：注入别的被评对象时各自声明，默认值即本演示抽取器。
    identity: dict[str, object] = {
        "name": "StressRuleExtractor",
        "revision": 2,
    }

    def __init__(self, *, max_units: int = 1500,
                 overlap_units: int = 200) -> None:
        self.max_units = max_units
        self.overlap_units = overlap_units
        self.chunker = Chunker(max_units=max_units, overlap_units=overlap_units)

    async def extract(self, document: Document) -> ExtractionResult:  # noqa: D401
        chunks = self.chunker.chunk_text(
            document.text, part_id=document.id, id_prefix=document.id)
        if not chunks:
            return ExtractionResult()
        partials = []
        for chunk in chunks:
            local = extract_chunk(chunk.text)
            partials.append(remap_spans(local, chunk))
        return naive_merge(partials)
