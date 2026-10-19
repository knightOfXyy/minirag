"""评测模块离线测试（黄金集加载 + P/R/F1 + 基线对比）。无需 DB/LLM。"""

from pathlib import Path

import pytest

from moray.evaluation import (
    evaluate,
    load_gold_dir,
    run_evaluation,
    compare,
)
from moray.extraction.baseline import BaselineExtractor, ImprovedExtractor
from moray.schema import Entity, ExtractionResult, Relation

GOLD_DIR = Path(__file__).resolve().parent.parent / "data" / "gold"


def test_gold_loader_clean_text_and_spans():
    cases = load_gold_dir(GOLD_DIR)
    assert len(cases) == 6
    for c in cases:
        # 干净文本不含词性标签
        assert "/nt" not in c.clean_text and "/nr" not in c.clean_text
        # 每个 gold mention 的 span 都能在干净文本里还原
        for e in c.gold.entities:
            for m in e.mentions:
                assert c.clean_text[m.span.start:m.span.end] == m.surface
    # 关系 sidecar 被加载（共 9 条黄金关系）
    total_rel = sum(len(c.gold.relations) for c in cases)
    assert total_rel == 9


def test_prf_metrics():
    gold = ExtractionResult(entities=[
        Entity(name="腾讯", type="ORG"), Entity(name="深圳", type="LOC")])
    pred = ExtractionResult(entities=[
        Entity(name="腾讯", type="ORG"), Entity(name="广州", type="LOC")])
    rep = evaluate(pred, gold)
    prf = rep.entity_relaxed
    assert prf.tp == 1 and prf.fn == 1 and prf.fp == 1
    assert rep.entity_relaxed.precision == pytest.approx(0.5)
    assert rep.entity_relaxed.recall == pytest.approx(0.5)
    assert rep.entity_relaxed.f1 == pytest.approx(0.5)


def test_relation_matching_with_predicate_synonym():
    gold = ExtractionResult(relations=[
        Relation(head="腾讯", predicate="总部位于", tail="深圳")])
    pred = ExtractionResult(relations=[
        Relation(head="腾讯", predicate="位于", tail="深圳")])
    # 谓词归一后应算命中
    assert evaluate(pred, gold).relation.tp == 1


async def test_baseline_to_improved_improves_scores():
    cases = load_gold_dir(GOLD_DIR)
    base = await run_evaluation(BaselineExtractor(), cases, name="base")
    imp = await run_evaluation(ImprovedExtractor(), cases, name="imp")

    base_e = base.micro()["entity_relaxed"]
    imp_e = imp.micro()["entity_relaxed"]
    assert imp_e["f1"] > base_e["f1"]
    # 改进版实体在这批黄金集上满分
    assert imp_e["precision"] == 1.0 and imp_e["recall"] == 1.0

    # 基线不抽关系 -> 关系 F1 为 0；改进版 > 0
    assert base.micro()["relation"]["f1"] == 0.0
    assert imp.micro()["relation"]["f1"] > 0.5

    delta = compare(base, imp)
    assert delta["entity_relaxed"]["delta"] > 0
    assert delta["relation"]["delta"] > 0
