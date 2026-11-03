"""确定性本地回填：逻辑实体/关系 -> mention/span/关系证据。

为什么存在
==========
模型最不擅长、出错最多的就是「数字符偏移」。瘦身模式下模型只输出逻辑实体
（标准名、类型、别名、属性）与逻辑关系（头/谓词/尾），所有出现位置由本模块
用纯本地、确定性的逻辑回填，杜绝偏移幻觉。

做法
====
* **多模式自动机（Aho–Corasick）**：把每个实体的标准名与全部别名作为模式，
  对分块正文只做【一次线性扫描】，即可拿到所有模式在所有位置的出现，
  而不是「每个名字各扫一遍全文」。
* **最长匹配优先**：扫描出的原始命中可能互相重叠/嵌套（如「阿里」是
  「阿里巴巴」的前缀）。用「按长度降序、起点升序」的贪心选取，保证长名字
  优先占住其区间，短名字只能落在不重叠的位置——不会发生短名对长名的错误覆盖。
* **歧义不猜**：同一个字面串若可归属到多个逻辑实体（同名不同类型，或别名
  归属冲突），该次出现被标记为「歧义未归属」，不写进任何实体，只计入质量信号。
* **关系证据**：按「头、尾实体在同一句中共现」定位；一句内有多对时取最近的
  一对作为 evidence_span，全部候选保留在回填信号里；跨句/跨块不臆造，证据留空。

本模块是纯函数，不调模型、不读环境、不可变地返回一个新的 ExtractionResult，
便于离线单测与评测复现。全局偏移不在此处理——调用方对块文本回填得到「块内
偏移」，再由既有的 ``pipeline.remap_spans`` 平移到文档全局坐标。

模块内分区（阅读顺序）
======================
1. ``BackfillReport`` / ``AmbiguousMention``：确定性质量信号（跨块汇总见
   ``merge_backfill_reports``，会把块内偏移平移成全局并带 chunk_id）。
2. ``AhoCorasick``：一趟扫描的全部原始命中。
3. ``backfill_provenance``：最长匹配区间裁决、歧义占位、mention 组装、
   别名幻觉信号（键用 ``entity_signal_key`` = JSON [type,name]）。
4. ``_backfill_relations``：同句共现证据（认英文句号、跨句不臆造）。
开关与上层接线见 ``Extractor(local_backfill=...)``、``pipeline``、审校
``rule_review(backfill=...)`` 与 ``docs/ARCHITECTURE.md`` 的瘦身回填章节。
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field

from ..schema import (
    Entity,
    EntityMention,
    ExtractionResult,
    Relation,
    Span,
)
from .chunking import split_sentences


# ---------------------------------------------------------------------------
# 回填质量信号
# ---------------------------------------------------------------------------
@dataclass
class AmbiguousMention:
    """一次可归属到多个逻辑实体的歧义出现。"""

    surface: str
    start: int
    end: int
    # 候选实体键（entity_signal_key：JSON [type, name]，同名不同类型不丢信息）
    candidates: list[str]
    reason: str = ""
    chunk_id: str = ""
    # 重叠块里同一处原文出现只保留一条歧义记录，来源块全部记在这里
    chunk_ids: list[str] = field(default_factory=list)


def max_confidence(a: float | None, b: float | None) -> float | None:
    """None = 未知；只有双方都未知时才保持未知，否则取已知最大值。"""
    known = [v for v in (a, b) if v is not None]
    return max(known) if known else None


def entity_signal_key(entity: Entity) -> str:
    """回填信号里实体键：JSON [type, name]。

    不能只用 name——同名不同类型（苹果/ORG 与 苹果/MISC）会互相覆盖，
    这也正是「歧义未归属」要保留的信息。
    """
    return json.dumps([entity.type, entity.name], ensure_ascii=False)



@dataclass
class BackfillReport:
    """一次回填的确定性质量信号（供下游替代模型自报置信度）。"""

    # 实体键（JSON [type,name]）-> 该实体在块内被命中的不同字面（标准名/别名）数。
    # 跨块汇总时按块相加：这是 chunk-weighted 计数（重叠块会对同一处原文各计
    # 一次），不是文档级去重后的「不同字面数」；需要文档级去重的消费方应自行
    # 按 (entity_key, span) 收敛（r4 N4）。
    matched_literals: dict[str, int] = field(default_factory=dict)
    # 模型给了、但正文里一次都没出现的名字/别名（疑似幻觉）
    unmatched_aliases: list[dict] = field(default_factory=list)
    # 该块正文中出现过的全部字面（来自 AC 原始命中，含被最长匹配/歧义吸收的）。
    # 别名幻觉判定只看这份集合，不再对每个别名做一次全文包含扫描；
    # 跨块汇总时取并集，让「正文」语义覆盖整篇文档而不是单块。
    seen_literals: list[str] = field(default_factory=list)
    # 歧义未归属的出现
    ambiguous_mentions: list[AmbiguousMention] = field(default_factory=list)
    # 块级置信度：被正文证据命中的实体占比（0~1）。
    # 跨块汇总按块相加（chunk-weighted）：重叠块会对同一原文各计一次，
    # 不是文档级去重计数；block_confidence 因此是「块加权命中率」（r4 N4）。
    entities_total: int = 0
    entities_grounded: int = 0
    # 关系证据命中情况（同上，跨块为 chunk-weighted 计数）
    relations_total: int = 0
    relations_with_evidence: int = 0
    relations_without_evidence: list[dict] = field(default_factory=list)
    # 每条关系的全部同句共现候选（evidence_span 取其中最近的一对）
    relation_evidence_candidates: dict[str, list[dict]] = field(default_factory=dict)
    # 该信号属于哪一块（跨块汇总时用于把块内偏移平移成全局偏移）
    chunk_id: str = ""
    chunk_start: int = 0

    @property
    def block_confidence(self) -> float:
        """块级确定性置信度 = 有正文命中的实体 / 实体总数（无实体时为 1.0）。"""
        return self.entities_grounded / self.entities_total if self.entities_total else 1.0

    @property
    def relation_evidence_ratio(self) -> float:
        return (
            self.relations_with_evidence / self.relations_total
            if self.relations_total else 1.0
        )

    @classmethod
    def from_dict(
        cls, data: dict, *, chunk_start: int = 0, chunk_id: str = ""
    ) -> "BackfillReport":
        """从落盘/跨进程传递的纯字典还原（pipeline 汇总分块信号用）。

        ``chunk_start``/``chunk_id`` 由调用方在跨块汇总时注入，用于把块内偏移
        平移成文档全局偏移并标明来源块（字典里的同名字段仅作回读兜底）。
        """
        report = cls()
        report.chunk_start = int(chunk_start or data.get("chunk_start", 0))
        report.chunk_id = chunk_id or str(data.get("chunk_id", ""))
        report.matched_literals = dict(data.get("matched_literals") or {})
        report.unmatched_aliases = list(data.get("unmatched_aliases") or [])
        report.seen_literals = list(data.get("seen_literals") or [])
        report.ambiguous_mentions = [
            AmbiguousMention(
                surface=x["surface"], start=x["start"], end=x["end"],
                candidates=list(x.get("candidates") or []),
                reason=x.get("reason", ""),
                chunk_id=x.get("chunk_id", report.chunk_id),
                chunk_ids=list(x.get("chunk_ids") or []),
            )
            for x in (data.get("ambiguous_mentions") or [])
        ]
        report.entities_total = int(data.get("entities_total", 0))
        report.entities_grounded = int(data.get("entities_grounded", 0))
        report.relations_total = int(data.get("relations_total", 0))
        report.relations_with_evidence = int(
            data.get("relations_with_evidence", 0))
        report.relations_without_evidence = list(
            data.get("relations_without_evidence") or [])
        report.relation_evidence_candidates = {
            key: list(cands)
            for key, cands in (data.get("relation_evidence_candidates") or {}).items()
        }
        return report

    def to_dict(self) -> dict:
        """可落盘/进看板的纯字典（确定性信号，供下游替代模型自报置信度）。"""
        return {
            "block_confidence": round(self.block_confidence, 4),
            "relation_evidence_ratio": round(self.relation_evidence_ratio, 4),
            "chunk_id": self.chunk_id,
            "chunk_start": self.chunk_start,
            "entities_total": self.entities_total,
            "entities_grounded": self.entities_grounded,
            "relations_total": self.relations_total,
            "relations_with_evidence": self.relations_with_evidence,
            "matched_literals": self.matched_literals,
            "unmatched_aliases": self.unmatched_aliases,
            "seen_literals": self.seen_literals,
            "ambiguous_mentions": [
                {
                    "surface": am.surface,
                    "start": am.start,
                    "end": am.end,
                    "candidates": am.candidates,
                    "reason": am.reason,
                    "chunk_id": am.chunk_id,
                    "chunk_ids": am.chunk_ids,
                }
                for am in self.ambiguous_mentions
            ],
            "relations_without_evidence": self.relations_without_evidence,
            "relation_evidence_candidates": self.relation_evidence_candidates,
        }


# ---------------------------------------------------------------------------
# Aho–Corasick 多模式自动机
# ---------------------------------------------------------------------------
class _Node:
    __slots__ = ("next", "fail", "out")

    def __init__(self) -> None:
        self.next: dict[str, int] = {}
        self.fail: int = 0
        # 在该节点（含 fail 链输出）结束的模式 id
        self.out: list[int] = []


class AhoCorasick:
    """一次扫描匹配全部模式的自动机。

    patterns: 去重后的字面串列表；命中结果通过 pattern id 关联回模式。
    """

    def __init__(self, patterns: list[str]) -> None:
        self._nodes: list[_Node] = [_Node()]       # 0 = 根
        self._patterns: list[str] = []
        self._pattern_id: dict[str, int] = {}
        for p in patterns:
            self._add(p)
        self._build_fail()

    def _add(self, pattern: str) -> None:
        if not pattern or pattern in self._pattern_id:
            return
        pid = len(self._patterns)
        self._pattern_id[pattern] = pid
        self._patterns.append(pattern)
        node = 0
        for ch in pattern:
            nxt = self._nodes[node].next.get(ch)
            if nxt is None:
                nxt = len(self._nodes)
                self._nodes[node].next[ch] = nxt
                self._nodes.append(_Node())
            node = nxt
        self._nodes[node].out.append(pid)

    def _build_fail(self) -> None:
        """BFS 建 failure 链接，并把输出沿 fail 链合并。"""
        queue: deque[int] = deque()
        for child in self._nodes[0].next.values():
            queue.append(child)
        while queue:
            v = queue.popleft()
            for ch, u in self._nodes[v].next.items():
                f = self._nodes[v].fail
                while f and ch not in self._nodes[f].next:
                    f = self._nodes[f].fail
                self._nodes[u].fail = self._nodes[f].next.get(ch, 0)
                # 合并 fail 链上的输出（后缀模式），使扫描无需回溯 fail 链
                self._nodes[u].out = (
                    self._nodes[u].out + self._nodes[self._nodes[u].fail].out
                )
                queue.append(u)

    def scan(self, text: str):
        """对正文做一次线性扫描，yield (start, end, pattern) 全部原始命中。"""
        node = 0
        root = self._nodes[0]
        for i, ch in enumerate(text):
            while node and ch not in self._nodes[node].next:
                node = self._nodes[node].fail
            node = self._nodes[node].next.get(ch, 0)
            for pid in self._nodes[node].out:
                pat = self._patterns[pid]
                yield (i + 1 - len(pat), i + 1, pat)


# ---------------------------------------------------------------------------
# 纯函数回填
# ---------------------------------------------------------------------------
def merge_entity_metadata(
    base: Entity, incoming: Entity, *, fill_description: bool = True
) -> Entity:
    """把 ``incoming`` 的别名/属性/置信度并入 ``base``（确定性、首见优先）。

    只合并「逻辑身份相同」的重复输出：身份键由调用方决定（回填/归并都用
    ``(type, name)``）。mentions 不在这里合并——回填要重建、naive_merge 要
    把跨块 mention 全量保留，语义不同，由调用方自己处理。
    ``fill_description=False`` 保留 262319b 的旧行为（不做 description 补全），
    供开关关闭时的 naive_merge 使用，保证逐字段一致。
    """
    for alias in incoming.aliases:
        if alias not in base.aliases and alias != incoming.name:
            base.aliases.append(alias)
    for key, value in incoming.attributes.items():
        base.attributes.setdefault(key, value)
    base.confidence = max_confidence(base.confidence, incoming.confidence)
    if fill_description and not base.description:
        base.description = incoming.description
    return base


def _coalesce_entities(entities: list[Entity]) -> list[Entity]:
    """把同一逻辑实体的重复输出合并为一个（逻辑身份 = (type, 标准名)）。

    与 ``pipeline.naive_merge`` 使用同一套元数据合并规则，避免两处逻辑漂移：
    模型若没去重、把同一 (类型,标准名) 输出了多遍（常带互补的别名/属性），
    这里按首次出现顺序确定性地并成一个逻辑实体，别名取并集、属性首见优先。
    这样「重复」不会被误判成「同名歧义」。
    """
    merged: list[Entity] = []
    index: dict[tuple[str, str], int] = {}
    for e in entities:
        key = (e.type, e.name)
        if key in index:
            merged[index[key]] = merge_entity_metadata(merged[index[key]], e)
        else:
            index[key] = len(merged)
            merged.append(Entity(
                name=e.name, type=e.type, aliases=list(e.aliases),
                mentions=[], confidence=e.confidence,
                attributes=dict(e.attributes),
                description=e.description))
    return merged


def literal_index(entities: list[Entity]) -> dict[str, list[int]]:
    """建立 字面（标准名/别名，去空白）-> 候选逻辑实体下标 的唯一索引。

    歧义、关系端点解析、AC 模式集必须共用同一套口径（去空、strip、别名去重），
    否则两处规则会各自漂移（r4 N5）。
    """
    literal_to_entities: dict[str, list[int]] = {}
    for idx, e in enumerate(entities):
        for literal in [e.name, *e.aliases]:
            literal = literal.strip()
            if not literal:
                continue
            indices = literal_to_entities.setdefault(literal, [])
            if idx not in indices:
                indices.append(idx)
    return literal_to_entities


def _entity_patterns(
    logical: list[Entity],
) -> tuple[list[str], dict[str, list[int]]]:
    """收集全部模式，并建立 字面 -> 候选【逻辑实体】下标 映射。

    只有当同一字面映射到【不同】逻辑实体（不同 (type,name)，或别名归属冲突）
    时才算歧义；同一逻辑实体自身重复出现已在 :func:`_coalesce_entities` 合并。
    """
    literal_to_entities = literal_index(logical)
    return list(literal_to_entities), literal_to_entities


def backfill_provenance(
    result: ExtractionResult,
    text: str,
) -> tuple[ExtractionResult, BackfillReport]:
    """对一次逻辑抽取结果回填全部 mention/span 与关系证据。

    返回 (新的 ExtractionResult, BackfillReport)；输入不被修改。
    span 为相对 ``text`` 的偏移：分块调用传块文本即得块内偏移，
    再交给 remap_spans 平移为全局偏移。
    """
    report = BackfillReport()
    # 先把同一逻辑实体 (type,name) 的重复输出合并，再做多模式匹配。
    logical = _coalesce_entities(list(result.entities))
    report.entities_total = len(logical)
    report.relations_total = len(result.relations)

    patterns, literal_to_entities = _entity_patterns(logical)

    # 该实体下累计的 mention，按出现位置排列
    mentions_by_entity: dict[int, list[EntityMention]] = {i: [] for i in range(len(logical))}
    matched_literals: dict[int, set[str]] = {i: set() for i in range(len(logical))}
    seen_literals: set[str] = set()

    if patterns:
        ac = AhoCorasick(patterns)
        raw_hits = list(ac.scan(text))
        # 一趟扫描的副产物：正文里真实出现过的全部字面（含随后被最长匹配
        # 或歧义占位吸收的命中）。别名是否「正文不存在」只看这份集合，
        # 不再对每个别名做一次 `literal in text` 全文扫描——那会让整体
        # 复杂度随别名数×文本长度增长，违反验收 10 的单趟线性承诺。
        seen_literals = {literal for _s, _e, literal in raw_hits}
        report.seen_literals = sorted(seen_literals)
        # 最长匹配优先：长模式先占区间；同长按起点；起点相同同长 = 歧义
        # （同一起点、同一长度必然是同一字面，归属在下面按候选数判定）。
        raw_hits.sort(key=lambda h: (-(h[1] - h[0]), h[0]))
        # 区间占用用按字符的 bytearray（0=空，1=已占）：
        # 旧实现每次遍历 occupied 列表判重叠，命中多时是 O(H²)；这里
        # `find`/切片赋值都在 C 层完成，20k 字上从秒级降到毫秒级。
        occupied = bytearray(len(text))

        def _interval_all_free(s: int, e: int) -> bool:
            # 语义：整个区间都未被占用才接受。r2 性能改造曾写成
            # `find(b"\x00", s, e) != -1`（区间里有任一空位就放行），
            # 导致部分重叠的短名漏进长名内部（阿里巴巴/巴巴）。
            return occupied.find(b"\x01", s, e) == -1

        for start, end, literal in raw_hits:
            # 先做最长匹配的区间裁决：若该区间已被更长的命中占用，直接跳过
            # （例如「苹果公司」已归属，其前缀「苹果」不再单独计为一次出现）。
            if not _interval_all_free(start, end):
                continue
            candidates = literal_to_entities.get(literal, [])
            # 歧义：同一字面归属多个逻辑实体 -> 不猜，标记且不写入任何实体。
            # 但区间仍要占住：否则短名/子串会从歧义串内部「漏」出来，被错误归属。
            occupied[start:end] = b"\x01" * (end - start)
            if len(candidates) > 1:
                if not any(
                    am.start == start and am.surface == literal
                    for am in report.ambiguous_mentions
                ):
                    report.ambiguous_mentions.append(
                        AmbiguousMention(
                            surface=literal,
                            start=start,
                            end=end,
                            candidates=[entity_signal_key(logical[i]) for i in candidates],
                            reason="同一字面归属多个逻辑实体",
                        )
                    )
                continue
            (ei,) = candidates
            surface = text[start:end]
            mentions_by_entity[ei].append(
                EntityMention(surface=surface, span=Span(start=start, end=end, text=surface))
            )
            matched_literals[ei].add(literal)

    # 组装实体：mentions 按起点排序；统计命中与未命中别名（幻觉信号）
    new_entities: list[Entity] = []
    for i, e in enumerate(logical):
        ms = sorted(mentions_by_entity[i], key=lambda m: m.span.start)
        hit = matched_literals[i]
        if ms:
            report.entities_grounded += 1
        report.matched_literals[entity_signal_key(e)] = len(hit)
        # 标准名没出现是「实体未命中正文」，不是「别名幻觉」；只有模型给的
        # 别名（!= 标准名）在正文中【字面完全不存在】才计入 unmatched_aliases。
        # 不能只看它是否独立成为 hit：作为长名前缀/子串被吸收（阿里巴巴/阿里）
        # 时别名明明出现了，报成幻觉是误报。这里比对一趟扫描收集的
        # seen_literals，避免按别名逐个重扫全文。
        for literal in e.aliases:
            literal = literal.strip()
            if literal and literal != e.name and literal not in seen_literals:
                report.unmatched_aliases.append(
                    {"entity": e.name, "literal": literal}
                )
        new_entities.append(
            Entity(
                name=e.name,
                type=e.type,
                aliases=list(e.aliases),
                mentions=ms,
                # 稳定内核 confidence 的语义是「模型自报抽取置信度」，回填不得
                # 用 grounding(0/1) 覆盖它；确定性信号只走 BackfillReport。
                confidence=e.confidence,
                attributes=dict(e.attributes),
                description=e.description,
            )
        )

    new_relations = _backfill_relations(
        list(result.relations), mentions_by_entity, logical, text, report
    )

    return ExtractionResult(entities=new_entities, relations=new_relations), report


def _relation_key(r: Relation) -> str:
    """三元组的无碰撞 key（JSON 编码，避免 "-" / "->" 拼接碰撞）。"""
    return json.dumps([r.head, r.predicate, r.tail], ensure_ascii=False)


def _backfill_relations(
    relations: list[Relation],
    mentions_by_entity: dict[int, list[EntityMention]],
    entities: list[Entity],
    text: str,
    report: BackfillReport,
) -> list[Relation]:
    """按「头、尾同句共现」回填关系证据 span；不共现则留空。"""
    # 关系端点按标准名或别名解析：模型有时会把别名写进 head/tail。
    # 只有「唯一归属」才认（同一字面映射到多个逻辑实体时不猜），与
    # backfill_provenance 的歧义口径共用 literal_index。
    literal_to_indices = literal_index(entities)

    def _resolve(literal: str) -> int | None:
        indices = literal_to_indices.get(literal.strip()) or []
        return indices[0] if len(indices) == 1 else None

    # 关系证据必须认英文句号（"Alice met Bob. Carol met Bob." 是两句），
    # 中文句切保持原行为；返回的偏移仍可对原文直接切片。
    sentences = split_sentences(text, ascii_period=True, preserve_leading=True)

    def _sentence_of(pos: int) -> int | None:
        for k, (s, e) in enumerate(sentences):
            if s <= pos < e:
                return k
        return None

    out: list[Relation] = []
    for r in relations:
        hi = _resolve(r.head)
        ti = _resolve(r.tail)
        pairs: list[tuple[int, int, int, int, int]] = []
        # (head_start, tail_start, sent_start, sent_end, distance)
        if hi is not None and ti is not None:
            hms = mentions_by_entity.get(hi, [])
            tms = mentions_by_entity.get(ti, [])
            for hm in hms:
                hs = hm.span.start
                sh = _sentence_of(hs)
                if sh is None:
                    continue
                for tm in tms:
                    ts = tm.span.start
                    if sh == _sentence_of(ts):
                        ss, se = sentences[sh]
                        pairs.append((hs, ts, ss, se, abs(hs - ts)))

        key = _relation_key(r)
        evidence: Span | None = None
        if pairs:
            # 最近的一对；取该句整句作为证据 span（去掉句首尾空白，
            # 但保持对原文的精确切片）
            ordered = sorted(pairs, key=lambda p: p[4])
            hs, ts, ss, se, _dist = ordered[0]
            while ss < se and text[ss].isspace():
                ss += 1
            while se > ss and text[se - 1].isspace():
                se -= 1
            evidence = Span(start=ss, end=se, text=text[ss:se])
            report.relations_with_evidence += 1
            # 候选按距离排序，并显式标出「被选中的最近一对」，让证据 span
            # 可观测、可复核（而不只是存一个不可解释的 evidence_span）。
            cands = [
                {
                    "head_start": p[0],
                    "tail_start": p[1],
                    "sent_start": p[2],
                    "sent_end": p[3],
                    "distance": p[4],
                    "selected": p is ordered[0],
                }
                for p in ordered
            ]
            report.relation_evidence_candidates[key] = cands
        else:
            report.relations_without_evidence.append(
                {"head": r.head, "predicate": r.predicate, "tail": r.tail}
            )

        out.append(
            Relation(
                head=r.head,
                predicate=r.predicate,
                tail=r.tail,
                evidence_span=evidence,
                # 同实体：不把 grounding 写回内核 confidence。
                confidence=r.confidence,
            )
        )
    return out


def merge_backfill_reports(
    reports: list[BackfillReport], *, document_text: str | None = None,
) -> BackfillReport:
    """把分块回填信号聚合成整篇信号（计数相加、偏移平移、明细去重）。

    不做「对块 ratio 再平均」——那会让小块的噪声等权放大；这里直接用
    grounded/total 的整数计数，块级置信度仍然是「命中实体 / 实体总数」。
    计数是 **chunk-weighted** 口径：重叠块会对同一处原文各计一次，
    ``entities_total`` / ``matched_literals`` 不是文档级去重值（r4 N4）；
    需要文档级去重的消费方自行按 ``(entity_key, span)`` 收敛。

    跨块语义：``ambiguous_mentions`` 与 ``relation_evidence_candidates`` 里的
    偏移在单块报告里是块内坐标；汇总时统一 + ``chunk_start`` 平移成文档全局
    坐标，并带上 ``chunk_id``，上层不会把 0..块长误当全局偏移。
    重叠块里的同一处歧义按 ``(surface, start, end, candidates)`` 去重，
    来源块收进 ``chunk_ids``，避免 overlap 把人工复核信号放大。
    ``unmatched_aliases`` 以整篇文档为口径：先取各块 ``seen_literals`` 的并集，
    只有所有块都没见过的别名才算幻觉，并按 ``(entity, literal)`` 去重。
    ``document_text`` 非空时再对「未被任何块采集到的候选别名」做一次线性
    AC 扫描：真实分块里某块可能漏抽实体，导致它的别名从未进入该块的
    ``seen_literals``；只有整篇正文都找不到的字面才是幻觉（r4 S1）。
    ``relations_without_evidence`` 按三元组去重（不同块重复命中同一关系时
    只保留一条，chunk_ids 记录来源块），避免按块堆叠把计数放大。
    """
    merged = BackfillReport()
    missing_index: dict[str, dict] = {}
    ambiguous_index: dict[tuple, AmbiguousMention] = {}
    unmatched_index: dict[tuple[str, str], dict] = {}
    seen_literals: set[str] = set()
    for report in reports:
        offset = report.chunk_start
        merged.entities_total += report.entities_total
        merged.entities_grounded += report.entities_grounded
        merged.relations_total += report.relations_total
        merged.relations_with_evidence += report.relations_with_evidence
        for key, n in report.matched_literals.items():
            merged.matched_literals[key] = merged.matched_literals.get(key, 0) + n
        seen_literals.update(report.seen_literals)
        for entry in report.unmatched_aliases:
            key = (str(entry.get("entity", "")), str(entry.get("literal", "")))
            unmatched_index.setdefault(key, dict(entry))
        for am in report.ambiguous_mentions:
            start, end = am.start + offset, am.end + offset
            key = (am.surface, start, end, tuple(am.candidates))
            existing = ambiguous_index.get(key)
            source_chunks = am.chunk_ids or (
                [am.chunk_id] if am.chunk_id else [report.chunk_id])
            if existing is None:
                merged_am = AmbiguousMention(
                    surface=am.surface,
                    start=start,
                    end=end,
                    candidates=list(am.candidates),
                    reason=am.reason,
                    chunk_id=source_chunks[0] if source_chunks else "",
                    chunk_ids=list(source_chunks),
                )
                ambiguous_index[key] = merged_am
                merged.ambiguous_mentions.append(merged_am)
            else:
                for cid in source_chunks:
                    if cid and cid not in existing.chunk_ids:
                        existing.chunk_ids.append(cid)
        for entry in report.relations_without_evidence:
            item = dict(entry)
            item["chunk_id"] = report.chunk_id
            key = json.dumps(
                [item.get("head"), item.get("predicate"), item.get("tail")],
                ensure_ascii=False, sort_keys=True)
            existing = missing_index.get(key)
            if existing is None:
                item["chunk_ids"] = [report.chunk_id]
                missing_index[key] = item
                merged.relations_without_evidence.append(item)
            else:
                chunk_ids = existing.setdefault("chunk_ids", [])
                if report.chunk_id not in chunk_ids:
                    chunk_ids.append(report.chunk_id)
        for key, cands in report.relation_evidence_candidates.items():
            shifted = []
            for c in cands:
                item = dict(c)
                for field in ("head_start", "tail_start", "sent_start", "sent_end"):
                    if item.get(field) is not None:
                        item[field] = item[field] + offset
                item["chunk_id"] = report.chunk_id
                shifted.append(item)
            merged.relation_evidence_candidates.setdefault(key, []).extend(shifted)
    # 真实分块的盲点：某块漏抽实体时，该实体的别名不会进入那一块的模式集，
    # 于是 seen_literals 里没有它，即使正文里明明存在。对「仍可能被判幻觉」
    # 的候选别名在整篇正文上补一次 AC 扫描（候选数远小于实体别名数，仍是
    # 线性），把整篇可见的字面并入 seen_literals（r4 S1）。
    if document_text is not None and unmatched_index:
        candidates = sorted({
            key[1] for key in unmatched_index
            if key[1] and key[1] not in seen_literals
        })
        if candidates:
            seen_literals.update(
                literal for _s, _e, literal in AhoCorasick(candidates).scan(
                    document_text)
            )
    merged.seen_literals = sorted(seen_literals)
    # 整篇口径：只有所有块都没出现过的别名才算幻觉；同一 (entity,literal)
    # 只保留一条，不按块堆叠。
    merged.unmatched_aliases = [
        entry for key, entry in unmatched_index.items()
        if key[1] not in seen_literals
    ]
    return merged
