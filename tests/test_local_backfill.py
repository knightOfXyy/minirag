"""确定性本地回填（mention/span + 关系证据）测试。

这些测试全部离线、确定性：模型只给逻辑实体/关系（名字、类型、别名、属性；
关系只有 头/谓词/尾），出现位置由本地多模式字面匹配回填。
"""

from moray.extraction import Chunk, remap_spans
from moray.extraction.local_backfill import (
    AhoCorasick,
    BackfillReport,
    backfill_provenance,
)
from moray.extraction.quality import verify_spans
from moray.schema import Entity, ExtractionResult, Relation


def _result() -> ExtractionResult:
    return ExtractionResult(
        entities=[
            Entity(name="阿里巴巴", type="ORG", aliases=["阿里", "BABA"]),
            Entity(name="马云", type="PER"),
            Entity(name="杭州", type="LOC"),
        ],
        relations=[
            Relation(head="马云", predicate="创立", tail="阿里巴巴"),
            Relation(head="阿里巴巴", predicate="总部位于", tail="杭州"),
        ],
    )


TEXT = "阿里巴巴（简称阿里，NYSE:BABA）由马云创立。阿里巴巴总部位于杭州。"


def test_multi_entity_alias_repeated_mixed_script_backfill():
    """多实体 + 别名 + 同文多次出现 + 中英文混排：一次扫描全部回填。"""
    result, report = backfill_provenance(_result(), TEXT)

    by_name = {e.name: e for e in result.entities}

    # 标准名「阿里巴巴」出现 2 次；别名「阿里」独立出现 1 次（在「简称」后）；
    # 英文别名 BABA 出现 1 次 —— 共 4 个 mention，且按出现顺序排列。
    ali = by_name["阿里巴巴"]
    surfaces = [m.surface for m in ali.mentions]
    assert surfaces == ["阿里巴巴", "阿里", "BABA", "阿里巴巴"]

    assert [m.surface for m in by_name["马云"].mentions] == ["马云"]
    assert [m.surface for m in by_name["杭州"].mentions] == ["杭州"]

    # 每个 span 都是原文的真实切片（独立核对：直接切原文），偏移零幻觉。
    for e in result.entities:
        for m in e.mentions:
            assert m.span is not None
            assert m.span.text == TEXT[m.span.start:m.span.end]
            assert m.span.text == m.surface
            assert TEXT[m.span.start:m.span.end] == m.surface

    # 现有质量探针必须 100% 通过。
    probe = verify_spans(result, TEXT)
    assert probe.ok, probe.mismatches
    assert probe.ratio == 1.0

    assert isinstance(report, BackfillReport)


def test_n_occurrences_produce_n_mentions_with_exact_slices():
    """实体在块内出现 N 次 -> N 个 mention，且每个 span 都是真实切片。"""
    text = "华为很强。华为在深圳。又是华为。"
    result, _ = backfill_provenance(
        ExtractionResult(entities=[Entity(name="华为", type="ORG")]), text
    )
    e = result.entities[0]
    assert len(e.mentions) == 3
    # 独立真值：用 str.find 逐步定位（不复用被测逻辑）
    cursor, truth = 0, []
    while True:
        i = text.find("华为", cursor)
        if i < 0:
            break
        truth.append((i, i + 2))
        cursor = i + 2
    assert [(m.span.start, m.span.end) for m in e.mentions] == truth
    for m in e.mentions:
        assert text[m.span.start:m.span.end] == "华为"


def test_prefix_substring_longest_match_wins():
    """一个实体名是另一个实体名的前缀/子串时，最长匹配优先，不错误覆盖。"""
    text = "苹果公司今日发布新品，苹果股价上涨。"
    result, report = backfill_provenance(
        ExtractionResult(entities=[
            Entity(name="苹果公司", type="ORG"),
            Entity(name="苹果", type="ORG"),
        ]),
        text,
    )
    by_name = {e.name: e for e in result.entities}

    # 「苹果公司」处归长名，不被短名抢占；
    first = by_name["苹果公司"].mentions
    assert [(m.surface) for m in first] == ["苹果公司"]
    assert first[0].span.start == text.find("苹果公司")

    # 独立出现的「苹果」（在「，」之后）仍归短名
    short = by_name["苹果"].mentions
    assert [m.surface for m in short] == ["苹果"]
    assert short[0].span.start == text.find("，苹果") + 1

    # 两处 span 不重叠，且都与原文切片一致
    all_spans = [(m.span.start, m.span.end)
                 for e in result.entities for m in e.mentions]
    assert all_spans
    for s, e2 in all_spans:
        assert text[s:e2] in ("苹果公司", "苹果")
    # 无歧义信号（两个不同字面各自唯一归属）
    assert report.ambiguous_mentions == []


def test_canonical_and_alias_ground_to_same_entity_phantom_alias_signaled():
    """别名与标准名都回填到同一实体；正文不存在的别名不产生 mention 且计入信号。"""
    text = "腾讯总部在深圳，微信是腾讯的产品。"
    result, report = backfill_provenance(
        ExtractionResult(entities=[
            Entity(name="腾讯", type="ORG", aliases=["腾讯公司", "鹅厂"]),
        ]),
        text,
    )
    e = result.entities[0]
    # 标准名「腾讯」出现 2 次；别名「腾讯公司」「鹅厂」正文里没有 -> 0 mention
    assert [m.surface for m in e.mentions] == ["腾讯", "腾讯"]
    assert all(m.span.text == "腾讯" for m in e.mentions)

    phantom = {(u["entity"], u["literal"]) for u in report.unmatched_aliases}
    assert ("腾讯", "腾讯公司") in phantom
    assert ("腾讯", "鹅厂") in phantom
    # 标准名命中，不出现在未命中列表里
    assert ("腾讯", "腾讯") not in phantom
    assert report.entities_grounded == 1
    assert report.block_confidence == 1.0


def test_same_name_different_type_is_ambiguous_and_unattributed():
    """同名不同类型：该字面出现被标记歧义，不写进任一实体。"""
    text = "苹果发布了新手机，苹果很好吃。"
    result, report = backfill_provenance(
        ExtractionResult(entities=[
            Entity(name="苹果", type="ORG"),
            Entity(name="苹果", type="MISC"),
        ]),
        text,
    )
    # 两个实体都拿不到 mention（归属不确定，不猜）
    for e in result.entities:
        assert e.mentions == []
        assert e.confidence == 0.0

    assert len(report.ambiguous_mentions) == 2
    for am in report.ambiguous_mentions:
        assert am.surface == "苹果"
        # 两个候选实体（同名）都被记录，而不是强行归给其一
        assert len(am.candidates) == 2
        assert text[am.start:am.end] == "苹果"
    # 两个实体都未被正文证据命中
    assert report.entities_grounded == 0


def test_alias_ownership_conflict_is_ambiguous():
    """同一别名被两个不同标准名实体声明时，该出现记为歧义、不归属。"""
    text = "东方与西方合作。"
    result, report = backfill_provenance(
        ExtractionResult(entities=[
            Entity(name="东方集团", type="ORG", aliases=["东方"]),
            Entity(name="东方科技", type="ORG", aliases=["东方"]),
        ]),
        text,
    )
    for e in result.entities:
        assert all(m.surface != "东方" for m in e.mentions)
    am = report.ambiguous_mentions
    assert len(am) == 1
    assert am[0].surface == "东方"
    assert set(am[0].candidates) == {"东方集团", "东方科技"}
    assert text[am[0].start:am[0].end] == "东方"


def test_relation_evidence_same_sentence_backfilled():
    """头、尾同句共现 -> 证据 span 为该句原文切片；候选全部保留。"""
    text = "张三于2020年创立了腾飞公司，这是一家科技企业。另一句无关。"
    result, report = backfill_provenance(
        ExtractionResult(
            entities=[
                Entity(name="张三", type="PER"),
                Entity(name="腾飞公司", type="ORG"),
            ],
            relations=[Relation(head="张三", predicate="创立", tail="腾飞公司")],
        ),
        text,
    )
    r = result.relations[0]
    assert r.evidence_span is not None
    first_sentence = text.split("。")[0] + "。"
    assert r.evidence_span.text == first_sentence
    assert text[r.evidence_span.start:r.evidence_span.end] == first_sentence
    assert report.relations_with_evidence == 1
    key = "张三-创立->腾飞公司"
    assert key in report.relation_evidence_candidates
    assert len(report.relation_evidence_candidates[key]) >= 1
    # 证据 span 通过质量探针
    assert verify_spans(result, text).ok


def test_relation_evidence_cross_sentence_is_empty_not_fabricated():
    """头、尾分属不同句（或块内根本不共现）-> 证据为空，不臆造位置。"""
    text = "张三来到了会场。腾飞公司发布了财报。"
    result, report = backfill_provenance(
        ExtractionResult(
            entities=[
                Entity(name="张三", type="PER"),
                Entity(name="腾飞公司", type="ORG"),
            ],
            relations=[Relation(head="张三", predicate="创立", tail="腾飞公司")],
        ),
        text,
    )
    r = result.relations[0]
    assert r.evidence_span is None
    assert r.confidence == 0.0
    assert report.relations_with_evidence == 0
    assert report.relations_without_evidence == [
        {"head": "张三", "predicate": "创立", "tail": "腾飞公司"}
    ]


def test_relation_evidence_nearest_pair_wins_and_all_kept():
    """一句内有多对共现时取最近的一对，全部候选保留在信号里。"""
    text = "马云在杭州见马云，阿里巴巴在杭州。"
    result, report = backfill_provenance(
        ExtractionResult(
            entities=[
                Entity(name="马云", type="PER"),
                Entity(name="杭州", type="LOC"),
            ],
            relations=[Relation(head="马云", predicate="位于", tail="杭州")],
        ),
        text,
    )
    # 整段是一句：马云 2 次 × 杭州 2 次 = 4 对共现候选；取距离最近的一对
    r = result.relations[0]
    assert r.evidence_span is not None
    assert r.evidence_span.text == text
    cands = report.relation_evidence_candidates["马云-位于->杭州"]
    assert len(cands) == 4
    distances = [c["distance"] for c in cands]
    assert distances == sorted(distances)  # 已按距离排序
    assert cands[0]["distance"] == min(distances)


class _CountingText:
    """统计被迭代次数与字符推进次数的 str 包装（不改变被遍历内容）。"""

    def __init__(self, text: str) -> None:
        self._text = text
        self.iter_starts = 0
        self.chars_yielded = 0

    def __iter__(self):
        self.iter_starts += 1
        for ch in self._text:
            self.chars_yielded += 1
            yield ch

    def __len__(self) -> int:
        return len(self._text)


def test_scan_is_single_linear_pass_independent_of_pattern_count():
    """扫描对正文只做一次：自动机按字符推进一趟，模式数量不增加扫描趟数。"""
    patterns = ["阿里", "腾讯", "字节", "百度", "京东", "美团", "拼多多", "网易"]
    text = "阿里与腾讯竞争，字节和百度合作，京东美团拼多多网易各有布局。"

    # 命中集合等于独立的 str.find 真值
    ac = AhoCorasick(patterns)
    hits = {(s, e, p) for s, e, p in ac.scan(text)}
    truth = set()
    for p in patterns:
        cur = 0
        while True:
            i = text.find(p, cur)
            if i < 0:
                break
            truth.add((i, i + len(p), p))
            cur = i + len(p)
    assert hits == truth

    # 8 个模式：正文只被迭代一趟，字符推进次数恰为正文长度
    ct1 = _CountingText(text)
    list(ac.scan(ct1))
    assert ct1.iter_starts == 1
    assert ct1.chars_yielded == len(text)

    # 模式数扩大到 58 个：同样只迭代一趟、同样的字符推进次数（不随模式增长）
    ac2 = AhoCorasick(patterns + [f"公司{i}" for i in range(50)])
    ct2 = _CountingText(text)
    list(ac2.scan(ct2))
    assert ct2.iter_starts == 1
    assert ct2.chars_yielded == len(text)


def test_duplicate_logical_entities_are_coalesced_not_ambiguous():
    """模型把同一 (类型,标准名) 重复输出多遍时：合并为一个，不算歧义。

    这与 pipeline.naive_merge 的聚合身份 (type,name) 一致——重复是没去重，
    而不是「同一字面归属多个逻辑实体」。
    """
    text = "华为发布新品，华为营收增长。"
    result, report = backfill_provenance(
        ExtractionResult(entities=[
            Entity(name="华为", type="ORG", aliases=["华为公司"]),
            Entity(name="华为", type="ORG"),  # 同一逻辑实体的重复输出
        ]),
        text,
    )
    assert len(result.entities) == 1
    e = result.entities[0]
    # 别名取并集保留
    assert "华为公司" in e.aliases
    # 两次出现都回填到同一个实体
    assert [m.surface for m in e.mentions] == ["华为", "华为"]
    assert report.ambiguous_mentions == []
    assert report.entities_total == 1
    assert report.entities_grounded == 1


def test_backfill_then_remap_to_global_offsets():
    """块内回填 -> 现有 remap_spans 平移 -> 全局偏移对原文切片一致。"""
    full = "开头无关铺垫。阿里巴巴由马云在杭州创立。结尾。"
    chunk_start = full.find("阿里巴巴由马云")
    chunk_end = chunk_start + len("阿里巴巴由马云在杭州创立。")
    chunk = Chunk(
        id="d#c1",
        text=full[chunk_start:chunk_end],
        start=chunk_start,
        end=chunk_end,
        index=1,
    )
    logical = ExtractionResult(
        entities=[
            Entity(name="阿里巴巴", type="ORG"),
            Entity(name="马云", type="PER"),
            Entity(name="杭州", type="LOC"),
        ],
        relations=[Relation(head="马云", predicate="创立", tail="阿里巴巴")],
    )
    local, _ = backfill_provenance(logical, chunk.text)
    global_res = remap_spans(local, chunk)

    # 所有实体 mention 的全局偏移对【整篇原文】切片一致
    for e in global_res.entities:
        for m in e.mentions:
            assert full[m.span.start:m.span.end] == m.surface
    r = global_res.relations[0]
    assert r.evidence_span is not None
    assert full[r.evidence_span.start:r.evidence_span.end] == r.evidence_span.text
    # 块内偏移 + chunk.start == 全局偏移
    local_ali = local.entities[0].mentions[0].span.start
    global_ali = global_res.entities[0].mentions[0].span.start
    assert global_ali == local_ali + chunk.start
