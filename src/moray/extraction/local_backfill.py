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
    candidates: list[str]          # 候选实体标准名
    reason: str = ""


@dataclass
class BackfillReport:
    """一次回填的确定性质量信号（供下游替代模型自报置信度）。"""

    # 实体名 -> 该实体在块内被命中的不同字面（标准名/别名）数
    matched_literals: dict[str, int] = field(default_factory=dict)
    # 模型给了、但正文里一次都没出现的名字/别名（疑似幻觉）
    unmatched_aliases: list[dict] = field(default_factory=list)
    # 歧义未归属的出现
    ambiguous_mentions: list[AmbiguousMention] = field(default_factory=list)
    # 块级置信度：被正文证据命中的实体占比（0~1）
    entities_total: int = 0
    entities_grounded: int = 0
    # 关系证据命中情况
    relations_total: int = 0
    relations_with_evidence: int = 0
    relations_without_evidence: list[dict] = field(default_factory=list)
    # 每条关系的全部同句共现候选（evidence_span 取其中最近的一对）
    relation_evidence_candidates: dict[str, list[dict]] = field(default_factory=dict)

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
    def from_dict(cls, data: dict) -> "BackfillReport":
        """从落盘/跨进程传递的纯字典还原（pipeline 汇总分块信号用）。"""
        report = cls()
        report.matched_literals = dict(data.get("matched_literals") or {})
        report.unmatched_aliases = list(data.get("unmatched_aliases") or [])
        report.ambiguous_mentions = [
            AmbiguousMention(
                surface=x["surface"], start=x["start"], end=x["end"],
                candidates=list(x.get("candidates") or []),
                reason=x.get("reason", ""),
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
            "entities_total": self.entities_total,
            "entities_grounded": self.entities_grounded,
            "relations_total": self.relations_total,
            "relations_with_evidence": self.relations_with_evidence,
            "matched_literals": self.matched_literals,
            "unmatched_aliases": self.unmatched_aliases,
            "ambiguous_mentions": [
                {
                    "surface": am.surface,
                    "start": am.start,
                    "end": am.end,
                    "candidates": am.candidates,
                    "reason": am.reason,
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
def merge_entity_metadata(base: Entity, incoming: Entity) -> Entity:
    """把 ``incoming`` 的别名/属性/置信度并入 ``base``（确定性、首见优先）。

    只合并「逻辑身份相同」的重复输出：身份键由调用方决定（回填/归并都用
    ``(type, name)``）。mentions 不在这里合并——回填要重建、naive_merge 要
    把跨块 mention 全量保留，语义不同，由调用方自己处理。
    """
    for alias in incoming.aliases:
        if alias not in base.aliases and alias != incoming.name:
            base.aliases.append(alias)
    for key, value in incoming.attributes.items():
        base.attributes.setdefault(key, value)
    base.confidence = max(base.confidence, incoming.confidence)
    if not base.description:
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


def _entity_patterns(
    logical: list[Entity],
) -> tuple[list[str], dict[str, list[int]]]:
    """收集全部模式，并建立 字面 -> 候选【逻辑实体】下标 映射。

    只有当同一字面映射到【不同】逻辑实体（不同 (type,name)，或别名归属冲突）
    时才算歧义；同一逻辑实体自身重复出现已在 :func:`_coalesce_entities` 合并。
    """
    literal_to_entities: dict[str, list[int]] = {}
    for idx, e in enumerate(logical):
        for literal in [e.name, *e.aliases]:
            literal = literal.strip()
            if not literal:
                continue
            literal_to_entities.setdefault(literal, [])
            if idx not in literal_to_entities[literal]:
                literal_to_entities[literal].append(idx)
    patterns = list(literal_to_entities.keys())
    return patterns, literal_to_entities


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

    if patterns:
        ac = AhoCorasick(patterns)
        raw_hits = list(ac.scan(text))

        # 最长匹配优先：长模式先占区间；同长按起点；起点相同同长 = 歧义
        # （同一起点、同一长度必然是同一字面，归属在下面按候选数判定）。
        raw_hits.sort(key=lambda h: (-(h[1] - h[0]), h[0]))
        # 区间占用用按字符的 bytearray（0=空，1=已占）：
        # 旧实现每次遍历 occupied 列表判重叠，命中多时是 O(H²)；这里
        # `find`/切片赋值都在 C 层完成，20k 字上从秒级降到毫秒级。
        occupied = bytearray(len(text))

        def _is_free(s: int, e: int) -> bool:
            return occupied.find(b"\x00", s, e) != -1

        for start, end, literal in raw_hits:
            # 先做最长匹配的区间裁决：若该区间已被更长的命中占用，直接跳过
            # （例如「苹果公司」已归属，其前缀「苹果」不再单独计为一次出现）。
            if not _is_free(start, end):
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
                            candidates=[logical[i].name for i in candidates],
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
        report.matched_literals[e.name] = len(hit)
        # 标准名没出现是「实体未命中正文」，不是「别名幻觉」；只有模型给的
        # 别名（!= 标准名）未出现才计入 unmatched_aliases。
        for literal in e.aliases:
            literal = literal.strip()
            if literal and literal != e.name and literal not in hit:
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
    name_to_idx = {e.name: i for i, e in enumerate(entities)}
    sentences = split_sentences(text)

    def _sentence_of(pos: int) -> int | None:
        for k, (s, e) in enumerate(sentences):
            if s <= pos < e:
                return k
        return None

    out: list[Relation] = []
    for r in relations:
        hi = name_to_idx.get(r.head)
        ti = name_to_idx.get(r.tail)
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
            # 最近的一对；取该句整句作为证据 span
            ordered = sorted(pairs, key=lambda p: p[4])
            hs, ts, ss, se, _dist = ordered[0]
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


def merge_backfill_reports(reports: list[BackfillReport]) -> BackfillReport:
    """把分块回填信号聚合成整篇信号（计数相加、明细拼接、候选合并）。

    不做「对块 ratio 再平均」——那会让小块的噪声等权放大；这里直接用
    grounded/total 的整数计数，块级置信度仍然是「命中实体 / 实体总数」。
    """
    merged = BackfillReport()
    for report in reports:
        merged.entities_total += report.entities_total
        merged.entities_grounded += report.entities_grounded
        merged.relations_total += report.relations_total
        merged.relations_with_evidence += report.relations_with_evidence
        for name, n in report.matched_literals.items():
            merged.matched_literals[name] = merged.matched_literals.get(name, 0) + n
        merged.unmatched_aliases.extend(report.unmatched_aliases)
        merged.ambiguous_mentions.extend(report.ambiguous_mentions)
        merged.relations_without_evidence.extend(report.relations_without_evidence)
        for key, cands in report.relation_evidence_candidates.items():
            merged.relation_evidence_candidates.setdefault(key, []).extend(cands)
    return merged
