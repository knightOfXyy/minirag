"""压力案卷黄金语料：确定性生成 + 答案可信校验 + 离线分块评测（工单 02）。

本测试不发任何 LLM 请求，全部走确定性路径：
  · 语料：moray.evaluation.stress.corpus 按显式种子生成合成案卷；
  · 抽取：StressRuleExtractor（后缀/词典/句式启发式）经真实 Chunker 分块归并；
  · 评分：metrics 的 strict/relaxed 口径 + 按实体类型拆分的 P/R/F1。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from moray.evaluation.stress.corpus import (
    STRESS_SEEDS,
    generate_case,
    serialize_case,
    validate_case,
)
from moray.extraction.chunking import estimate_tokens

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
    import random
    from moray.evaluation.stress.corpus import build_text
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
    from moray.evaluation.stress.corpus import StressEntity, StressRelation
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


# ---------------------------------------------------------------------------
# 切片 2：确定性分块规则抽取器（不调 LLM；真实走 Chunker + 偏移换算 + 归并）
# ---------------------------------------------------------------------------
from moray.evaluation.stress.extractor import StressRuleExtractor  # noqa: E402
from moray.extraction.document import Document  # noqa: E402
from moray.extraction.chunking import Chunker  # noqa: E402


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


async def test_chunked_extractor_missing_cross_chunk_distal_relation():
    """远端关系（办案地位于：头在卷一、尾只在卷五）在 1500 小预算下应丢，
    模拟 naive_merge 从不跨块造关系——这正是大块预算要改善的召回。"""
    case = generate_case(seed=STRESS_SEEDS[0])
    distal = {(r.head, r.tail) for r in case.relations
              if r.predicate == "办案地位于"}
    assert distal  # 标准答案里确实有远端关系
    result = await _extract_chunked(case.text, 1500, 200)
    got = {(r.head, r.tail) for r in result.relations}
    # 至少一条远端关系因跨块而抽不到（不要求全丢：头尾在卷五可能有别的名字）
    assert not distal <= got


# ---------------------------------------------------------------------------
# 切片 3：落盘语料装载 + 按实体类型评分 + 分块参数网格
# ---------------------------------------------------------------------------
from moray.evaluation.stress.loader import (  # noqa: E402
    load_stress_dir,
)
from moray.evaluation.stress.runner import (  # noqa: E402
    DEFAULT_GRIDS,
    aggregate_grid,
    run_grid_async,
)
from moray.evaluation import load_gold_dir, run_evaluation  # noqa: E402
from moray.extraction.baseline import ImprovedExtractor  # noqa: E402


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


async def test_larger_chunk_budget_does_not_reduce_modeled_relation_recall():
    """同一语料按块参数维度复跑：被建模的【近端】关系在大块下不应更差
    （远端关系因本抽取器不跨块造关系而恒丢，不作为 A/B 判据）。"""
    from moray.evaluation.stress.metrics import relation_recall_for_predicates
    cases = load_stress_dir(STRESS_DIR)
    grids = await run_grid_async(cases, grids=[(1500, 200), (3500, 200)])
    modeled = {"供职于", "户籍在", "注册地位于", "涉案关联", "关联物证"}

    r_small = relation_recall_for_predicates(grids[(1500, 200)], modeled)["recall"]
    r_big = relation_recall_for_predicates(grids[(3500, 200)], modeled)["recall"]
    assert r_big >= r_small
    assert r_big > 0.0


def test_default_grids_cover_production_and_candidate_budgets():
    # 生产档 (1500,200) 必须在内，且至少给出一组候选更大预算用于 A/B
    assert (1500, 200) in DEFAULT_GRIDS
    assert any(mx > 1500 for mx, _ in DEFAULT_GRIDS)


async def test_old_six_gold_cases_unchanged():
    """新增语料不改变旧 6 案例的既有评测结果（共存、不覆盖）。"""
    old_cases = load_gold_dir(GOLD_DIR)
    assert len(old_cases) == 6
    run = await run_evaluation(ImprovedExtractor(), old_cases, name="imp")
    micro = run.micro()
    # 锁定 test_evaluation.py 已确立的既有口径：improved 在 6 案例实体满分
    assert micro["entity_relaxed"]["precision"] == 1.0
    assert micro["entity_relaxed"]["recall"] == 1.0
    assert micro["relation"]["f1"] > 0.5
    # 旧黄金目录里没有 stress 子目录被误扫
    assert {c.doc_id for c in old_cases} == {
        f"case0{i}.tagged" for i in range(1, 7)}


