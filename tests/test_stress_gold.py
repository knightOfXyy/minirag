"""压力案卷黄金语料：确定性生成 + 答案可信校验 + 离线分块评测（工单 02）。

本测试不发任何 LLM 请求，全部走确定性路径：
  · 语料：moray.evaluation.stress.corpus 按显式种子生成合成案卷；
  · 抽取：StressRuleExtractor（后缀/词典/句式启发式）经真实 Chunker 分块归并；
  · 评分：metrics 的 strict/relaxed 口径 + 按实体类型拆分的 P/R/F1。
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from moray.evaluation import load_gold_dir, run_evaluation
from moray.evaluation.stress import (
    DECOY_PREFIX,
    DEFAULT_GRIDS,
    STRESS_SEEDS,
    StressEntity,
    StressRelation,
    StressRuleExtractor,
    aggregate_cases,
    aggregate_grid,
    build_report,
    build_text,
    evaluate_case,
    extract_chunk,
    generate_case,
    load_stress_dir,
    relation_recall_for_predicates,
    run_grid_async,
    serialize_case,
    validate_case,
)
from moray.extraction.baseline import ImprovedExtractor
from moray.extraction.chunking import Chunker, estimate_tokens
from moray.extraction.document import Document

REPO_ROOT = Path(__file__).resolve().parent.parent
STRESS_DIR = REPO_ROOT / "data" / "gold" / "stress"
GOLD_DIR = REPO_ROOT / "data" / "gold"

# 生产分块预算（config.py 默认 max_units=1500），语料必须超过它的 5 倍
PROD_MAX_UNITS = 1500


# ---------------------------------------------------------------------------
# 切片 1：生成器确定性、规模、答案经程序过滤可信
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("seed", STRESS_SEEDS)
def test_generated_case_is_large_and_answers_are_text_backed(seed):
    case = generate_case(seed=seed)
    # 规模：正文估算 units 超过当前生产块预算的 5 倍
    assert estimate_tokens(case.text) > PROD_MAX_UNITS * 5
    # 实体答案：规范名 + 类型齐全，且每个名字都能在干净正文中字面找到
    assert case.entities
    types = {e.type for e in case.entities}
    assert {"PER", "ORG", "LOC"} <= types
    for e in case.entities:
        assert e.name and e.name in case.text
    # 关系答案：头/尾名称都在正文中可定位；且都指向答案里真实存在的实体名
    ent_names = {e.name for e in case.entities}
    assert case.relations
    for r in case.relations:
        assert r.head in case.text
        assert r.tail in case.text
        assert r.head in ent_names and r.tail in ent_names
    # validate_case 自身也应判通过（它是生成器落盘前后的同一道闸门）
    missing_e, missing_r = validate_case(case)
    assert not missing_e and not missing_r


# 谓词 -> 正文里真正的触发词（gold 的规范谓词不一定逐字出现）。
_TRIGGER = {
    "供职于": "曾在",
    "户籍在": "户籍地",
    "注册地位于": "注册地位于",
    "涉案关联": "案件编号",
    "关联物证": "关联到关键物证",
    "办案地位于": "办案地位于",
    "追赃方向指向": "追赃方向指向",
}


def _relation_supported(case, r) -> bool:
    """句面语义校验：头是触发词前最近的同类实体，尾是触发词后最近的同类实体。

    只查"头尾可定位"查不出「马秀兰-供职于-锦江华诚机械制造有限公司」这类
    假答案；只查"同一行出现"又会放过长句里尾放错位置的松校验。这里的
    "最近同类实体"同时锁死两边：头与触发词之间、触发词与尾之间都不能插进
    另一个同类型实体名。
    """
    trigger = _TRIGGER[r.predicate]
    type_of = {e.name: e.type for e in case.entities}
    by_type: dict[str, set[str]] = {}
    for e in case.entities:
        by_type.setdefault(e.type, set()).add(e.name)
    head_type, tail_type = type_of[r.head], type_of[r.tail]

    for line in case.text.splitlines():
        ti = line.find(trigger)
        if ti < 0 or r.head not in line or r.tail not in line:
            continue
        if r.predicate == "涉案关联":
            # "案件编号 A-… 涉及 头名、… 等人员"：头在触发词之后。
            case_pos = line.find(r.tail, ti)
            hi = line.find(r.head, case_pos + len(r.tail))
            if case_pos < 0 or hi < 0:
                continue
            between = [n for n in by_type.get(head_type, ())
                       if n != r.head and n in line[case_pos:hi]]
            if not between:
                return True
            continue
        hi = line.rfind(r.head, 0, ti)
        if hi < 0:
            continue
        between_head = [n for n in by_type.get(head_type, ())
                        if n != r.head
                        and n in line[hi + len(r.head):ti]]
        ti_end = ti + len(trigger)
        tail_pos = line.find(r.tail, ti_end)
        if tail_pos < 0:
            continue
        between_tail = [n for n in by_type.get(tail_type, ())
                        if n != r.tail and n in line[ti_end:tail_pos]]
        if not between_head and not between_tail:
            return True
    return False


@pytest.mark.parametrize("seed", STRESS_SEEDS)
def test_gold_relations_are_semantically_backed_by_text(seed):
    """每条关系都必须有一句正文真正陈述它，而不只是"头尾都能在文中找到"。

    decoy 单位名与真实单位撞名时，仅靠"可定位"闸门会把
    「马秀兰 供职于 锦江华诚机械制造有限公司」写进黄金答案；
    这里按谓词逐条核对句面，锁死这一类语义假答案。
    """
    case = generate_case(seed=seed)
    predicates = {r.predicate for r in case.relations}
    assert predicates == set(_TRIGGER)
    for r in case.relations:
        assert _relation_supported(case, r), (
            f"{r.head}-{r.predicate}->{r.tail} 正文没有任何一句语义支撑")


@pytest.mark.parametrize("seed", STRESS_SEEDS)
def test_decoy_entities_never_collide_with_real_names(seed):
    """decoy 必须结构上不可能撞名（固定前缀），保证过滤闸门不是靠巧合。"""
    case = generate_case(seed=seed)
    rng = random.Random(seed)
    _, candidates, _ = build_text(
        rng, target_chars=12000, max_units=PROD_MAX_UNITS)
    decoys = [c for c in candidates if c.name.startswith(DECOY_PREFIX)]
    assert decoys
    for d in decoys:
        assert d.name not in case.text
        assert d.name not in {e.name for e in case.entities}


def test_generator_is_deterministic_byte_for_byte():
    a = generate_case(seed=STRESS_SEEDS[0])
    b = generate_case(seed=STRESS_SEEDS[0])
    payload_a = json.dumps(serialize_case(a), ensure_ascii=False,
                           sort_keys=True)
    payload_b = json.dumps(serialize_case(b), ensure_ascii=False,
                           sort_keys=True)
    assert a.text == b.text
    assert payload_a == payload_b


def test_generator_filters_answers_not_backed_by_text():
    """生成器候选里刻意混入正文不出现的实体/头尾不可定位的关系，
    generate_case 必须把它们全部剔除（而不是靠巧合全命中）。"""
    rng = random.Random(STRESS_SEEDS[0])
    _, candidates, rel_candidates = build_text(
        rng, target_chars=12000, max_units=1500)
    case = generate_case(STRESS_SEEDS[0])
    cand_names = {e.name for e in candidates}
    kept = {e.name for e in case.entities}
    dropped = cand_names - kept
    assert dropped  # 至少有候选被剔除
    # 被剔除的名字确实不在正文里
    assert all(n not in case.text for n in dropped)
    # 关系候选多于保留下来的（含刻意的不可定位 decoy）
    assert len(rel_candidates) > len(case.relations)


def test_validate_case_flags_bad_answers():
    good = generate_case(STRESS_SEEDS[0])
    tampered = type(good)(
        seed=good.seed, name=good.name, text=good.text,
        entities=good.entities + [StressEntity("不存在单位有限公司", "ORG")],
        relations=good.relations + [
            StressRelation(good.entities[0].name, "供职于", "不存在单位有限公司")],
    )
    missing_e, missing_r = validate_case(tampered)
    assert "不存在单位有限公司" in missing_e
    assert any("不存在单位有限公司" in r for r in missing_r)


def test_different_seeds_produce_different_cases():
    cases = [generate_case(seed=s) for s in STRESS_SEEDS]
    texts = {c.text for c in cases}
    assert len(texts) == len(STRESS_SEEDS) >= 3


def test_case_numbers_are_seed_dependent():
    """案号也要吃种子；否则三篇文档的 EVENT 编号完全相同，削弱种子独立性。"""
    event_sets = []
    for seed in STRESS_SEEDS:
        case = generate_case(seed=seed)
        event_sets.append(
            {e.name for e in case.entities if e.type == "EVENT"})
    assert len(event_sets) == len(STRESS_SEEDS)
    for i in range(len(event_sets)):
        for j in range(i + 1, len(event_sets)):
            assert event_sets[i] != event_sets[j]


# ---------------------------------------------------------------------------
# 切片 2：确定性分块规则抽取器（不调 LLM；真实走 Chunker + 偏移换算 + 归并）
# ---------------------------------------------------------------------------
def test_org_names_with_legal_suffixes_are_extracted():
    """被评抽取器必须完整抽出「有限责任公司」与「…集团有限公司」。

    历史 bug：`_ORG_STOP` 含「任」，整名回绝把「有限责任公司」全部丢掉；
    后缀表把「集团」排在「有限公司」前面，把「…集团有限公司」截成「…集团」。
    """
    text = ("城南信达物流有限责任公司，曾在北辰恒泰物流有限责任公司任职。"
            "临江宏远实业集团有限公司注册地位于临江区。")
    names = {e.name for e in extract_chunk(text).entities if e.type == "ORG"}
    assert names == {
        "城南信达物流有限责任公司",
        "北辰恒泰物流有限责任公司",
        "临江宏远实业集团有限公司",
    }


async def _extract_chunked(text, max_units, overlap_units):
    extractor = StressRuleExtractor(
        max_units=max_units, overlap_units=overlap_units)
    return await extractor.extract(
        Document(id="t", text=text, source="test", domain_code="general"))


async def test_chunked_extractor_finds_typed_entities_with_global_spans():
    case = generate_case(seed=STRESS_SEEDS[0])
    result = await _extract_chunked(case.text, 1500, 200)
    # 至少抽出三类受控实体
    by_type: dict[str, set[str]] = {}
    for e in result.entities:
        by_type.setdefault(e.type, set()).add(e.name)
    assert {"PER", "ORG", "LOC"} <= set(by_type)
    # 案号 EVENT 也应被识别
    assert any(e.type == "EVENT" for e in result.entities)
    # 所有保留下来的 span 都是相对【全文】的全局偏移，且切片恰等于 surface
    # （块内偏移换算正确）。硬切边界处的越界 span 按 remap_spans 既有契约置空，
    # 这里只校验未置空者，不强迫硬切实体保留 span。
    n_located = 0
    for e in result.entities:
        for m in e.mentions:
            if m.span is not None:
                n_located += 1
                assert case.text[m.span.start:m.span.end] == m.surface
    assert n_located > 0
    # 该文在生产预算下确实被切成多块（否则测不到分块行为）
    chunks = Chunker(1500, 200).chunk_text(case.text)
    assert len(chunks) > 1


async def test_chunked_extractor_is_deterministic():
    case = generate_case(seed=STRESS_SEEDS[1])
    r1 = await _extract_chunked(case.text, 1500, 200)
    r2 = await _extract_chunked(case.text, 1500, 200)
    key = lambda r: (  # noqa: E731
        sorted((e.type, e.name,
                tuple(sorted((m.span.start, m.span.end) for m in e.mentions)))
               for e in r.entities),
        sorted((x.head, x.predicate, x.tail) for x in r.relations),
    )
    assert key(r1) == key(r2)


def _long_relation_pairs(case) -> set[tuple[str, str, str]]:
    """找出正文里由一条 >1500 units 的长句承载的关系三元组。"""
    pairs: set[tuple[str, str, str]] = set()
    for r in case.relations:
        trigger = _TRIGGER[r.predicate]
        for line in case.text.splitlines():
            if (estimate_tokens(line) > PROD_MAX_UNITS
                    and r.head in line and r.tail in line
                    and trigger in line):
                pairs.add((r.head, r.predicate, r.tail))
    return pairs


@pytest.mark.parametrize("seed", STRESS_SEEDS)
def test_each_case_has_multiple_budget_sensitive_long_relations(seed):
    """每篇至少 4 条 >1500 units 的含头尾长句，且谓词不止一种。

    只有 1 条时，工单 04 的「关系 F1 不降」门槛只剩 1% 余量，一个 FP
    就可能吃掉信号。
    """
    case = generate_case(seed=seed)
    long_pairs = _long_relation_pairs(case)
    assert len(long_pairs) >= 4
    assert len({p for _, p, _ in long_pairs}) >= 2


async def test_relation_budget_gap_is_double_digit():
    """1500→3500 的关系召回差异必须来自两位数条关系，而不是 3 条巧合。"""
    cases = load_stress_dir(STRESS_DIR)
    grids = await run_grid_async(cases, grids=[(1500, 200), (3500, 200)])
    small = aggregate_grid(grids[(1500, 200)])["relation"]
    big = aggregate_grid(grids[(3500, 200)])["relation"]
    assert big["fn"] == 0
    assert small["fn"] - big["fn"] >= 10


async def test_long_relation_sentence_is_lost_at_production_budget():
    """>1500 units 的含头尾关系长句：1500 档被 hard_split 拆开必丢，
    3500 档整句同块必中——这是关系轴对块预算的真实信号，而不是恒等式。"""
    case = generate_case(seed=STRESS_SEEDS[0])
    long_pairs = _long_relation_pairs(case)
    assert long_pairs
    r_small = await _extract_chunked(case.text, 1500, 200)
    r_big = await _extract_chunked(case.text, 3500, 200)
    got_small = {(x.head, x.predicate, x.tail) for x in r_small.relations}
    got_big = {(x.head, x.predicate, x.tail) for x in r_big.relations}
    assert not (long_pairs & got_small)
    assert long_pairs <= got_big


# ---------------------------------------------------------------------------
# 切片 3：落盘语料装载 + 按实体类型评分 + 分块参数网格
# ---------------------------------------------------------------------------
def test_load_committed_stress_corpus_validates():
    cases = load_stress_dir(STRESS_DIR)
    assert {c.seed for c in cases} == set(STRESS_SEEDS)
    for c in cases:
        assert estimate_tokens(c.text) > PROD_MAX_UNITS * 5
        missing_e, missing_r = validate_case(c)
        assert not missing_e and not missing_r
        # 装载后的 gold ExtractionResult：实体带可定位 span，关系齐全
        assert c.gold.entities and c.gold.relations
        for e in c.gold.entities:
            assert e.mentions and all(
                m.span is not None
                and c.text[m.span.start:m.span.end] == m.surface
                for m in e.mentions)


def test_per_type_breakdown_reconciles_with_totals():
    """按类型表必须与顶层实体计数对账，包括金标里没有的预测类型。

    历史 bug：``types`` 只从 gold 取，抽取器吐出金标外类型（如 DATE）时，
    该实体的 FP 进了顶层 ``entity_relaxed`` 却被按类型表静默丢掉。
    """
    from moray.schema import Entity, ExtractionResult

    gold = ExtractionResult(entities=[Entity(name="张三", type="PER")])
    pred = ExtractionResult(entities=[
        Entity(name="张三", type="PER"),      # TP
        Entity(name="2024年3月", type="DATE"),  # FP，gold 里没有 DATE
    ])
    agg = aggregate_cases([evaluate_case("synthetic", 1, pred, gold)])
    assert agg["entity_relaxed"]["fp"] == 1
    assert "DATE" in agg["entity_by_type"]
    assert sum(p["fp"] for p in agg["entity_by_type"].values()) == 1
    # fn 同理：预测漏掉的类型（此处无）也必须能被对账
    assert sum(p["fn"] for p in agg["entity_by_type"].values()) == (
        agg["entity_relaxed"]["fn"])


def test_committed_corpus_matches_generator_output_byte_for_byte():
    """仓内 6 个语料产物必须等于生成器输出，防止数据悄悄过期。

    ``test_generator_is_deterministic_byte_for_byte`` 只比较同进程两次生成；
    改了 ``corpus.py`` 却忘记重新生成时，那种测试仍然全绿。这里用与
    ``gen_stress_gold.py`` 相同的序列化口径（indent=2 + 结尾换行）把仓内
    文件钉到 ``generate_case(seed)`` 上。
    """
    cases = load_stress_dir(STRESS_DIR)
    assert {c.seed for c in cases} == set(STRESS_SEEDS)
    for c in cases:
        fresh = generate_case(seed=c.seed)
        assert fresh.text == c.text, f"{c.name}.md 与生成器输出不一致"
        answer_path = STRESS_DIR / f"{c.name}.answer.json"
        expected = json.dumps(
            serialize_case(fresh), ensure_ascii=False, indent=2) + "\n"
        assert answer_path.read_text(encoding="utf-8") == expected, (
            f"{c.name}.answer.json 与生成器输出不一致")


def test_predicate_normalization_is_public_api():
    """谓词归一必须是公开 API，压力评测不再 import 私有 ``_predicate_norm``。"""
    from moray.evaluation.metrics import predicate_norm

    assert predicate_norm("位于") == "总部位于"
    assert predicate_norm("办案地位于") == "办案地位于"


async def test_grid_baseline_metrics_and_per_type_breakdown():
    cases = load_stress_dir(STRESS_DIR)
    grids = await run_grid_async(cases, grids=[(1500, 200), (3500, 200)])
    assert set(grids) == {(1500, 200), (3500, 200)}
    agg = aggregate_grid(grids[(1500, 200)])
    # 顶层三块：实体 strict/relaxed + 关系，均为合法 P/R/F1
    for key in ("entity_strict", "entity_relaxed", "relation"):
        prf = agg[key]
        assert 0.0 <= prf["precision"] <= 1.0
        assert 0.0 <= prf["recall"] <= 1.0
        assert 0.0 <= prf["f1"] <= 1.0
    # 按实体类型出数：PER/ORG/LOC/EVENT/MISC 五类键齐
    by_type = agg["entity_by_type"]
    assert {"PER", "ORG", "LOC", "EVENT", "MISC"} <= set(by_type)
    for etype, prf in by_type.items():
        assert {"precision", "recall", "f1", "tp", "fp", "fn"} <= set(prf)
    # 基线确实抽到了一些实体（relaxed 精确率/召回都非零），关系也非零
    assert agg["entity_relaxed"]["recall"] > 0.0
    assert agg["relation"]["recall"] > 0.0


async def test_larger_chunk_budget_strictly_improves_relation_recall():
    """同一语料按块参数维度复跑：3500 档的关系召回必须**严格高于** 1500 档。

    旧断言 `>=` 在 0.7523 == 0.7523 时也通过（关系轴对预算完全无响应），
    等于给工单 04 的「关系 F1 不降」门槛放了一张空转通行证。
    """
    cases = load_stress_dir(STRESS_DIR)
    grids = await run_grid_async(cases, grids=[(1500, 200), (3500, 200)])
    modeled = {r.predicate for c in cases for r in c.relations}

    r_small = relation_recall_for_predicates(
        grids[(1500, 200)], modeled)["recall"]
    r_big = relation_recall_for_predicates(
        grids[(3500, 200)], modeled)["recall"]
    assert r_big > r_small
    assert r_big > 0.0


async def test_grid_runner_uses_injected_extractor_factory():
    """网格复跑必须能注入别的被评对象（工单 04 要扫真实抽取路径）。"""
    cases = load_stress_dir(STRESS_DIR)

    class TrackingExtractor(StressRuleExtractor):
        identity = {"name": "TrackingExtractor", "revision": 11}
        calls = 0

        async def extract(self, doc):
            type(self).calls += 1
            return await super().extract(doc)

    grids = await run_grid_async(
        cases, grids=[(1500, 200)], extractor_factory=TrackingExtractor)
    assert TrackingExtractor.calls == len(cases)
    report = build_report(grids, extractor_factory=TrackingExtractor)
    assert report["extractor"] == {"name": "TrackingExtractor", "revision": 11}


def test_grid_report_records_extractor_identity():
    """留存报告必须写明是哪个抽取器/哪版规则跑出来的，否则几个月后无法比对。"""
    payload = build_report({(1500, 200): []})
    assert payload["extractor"]["name"] == "StressRuleExtractor"
    assert isinstance(payload["extractor"]["revision"], int)
    assert payload["corpus_version"] >= 2
    # 没有判别力的维度必须写明，否则后续工单会把"恒等"误当 A/B 证据。
    assert "overlap" in payload["overlap_note"]
    assert payload["entity_axis_note"]


def test_default_grids_cover_production_and_candidate_budgets():
    # 生产档 (1500,200) 必须在内，且至少给出一组候选更大预算用于 A/B
    assert (1500, 200) in DEFAULT_GRIDS
    assert any(mx > 1500 for mx, _ in DEFAULT_GRIDS)


async def test_old_six_gold_cases_unchanged():
    """新增语料不改变旧 6 案例的既有评测结果（共存、不覆盖）。

    用精确常量锁定（而不是 "关系 F1 > 0.5" 这种魔法阈值：实测基线 F1=0.8，
    掉到 0.51 也能过）。
    """
    old_cases = load_gold_dir(GOLD_DIR)
    assert len(old_cases) == 6
    run = await run_evaluation(ImprovedExtractor(), old_cases, name="imp")
    micro = run.micro()
    assert micro["entity_strict"] == {
        "tp": 16, "fp": 0, "fn": 0,
        "precision": 1.0, "recall": 1.0, "f1": 1.0}
    assert micro["entity_relaxed"] == {
        "tp": 16, "fp": 0, "fn": 0,
        "precision": 1.0, "recall": 1.0, "f1": 1.0}
    assert micro["relation"] == {
        "tp": 6, "fp": 0, "fn": 3,
        "precision": 1.0, "recall": 0.6667, "f1": 0.8}
    # 旧黄金目录里没有 stress 子目录被误扫
    assert {c.doc_id for c in old_cases} == {
        f"case0{i}.tagged" for i in range(1, 7)}
