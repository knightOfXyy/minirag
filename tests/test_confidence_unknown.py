"""评审 r2 blocker 回归：未知置信度必须可区分，不能被当成 0.0 真实值。

瘦身模式模型不自报 confidence -> 稳定内核必须是「未知」(None)，
入库/查询/API/审校据此走「未知」分支；full 模式仍保留模型自报数值。
"""

from moray.extraction import Extractor
from moray.kb.ingestor import Ingestor
from moray.schema import (
    GENERAL_DOMAIN,
    Entity,
    ExtractionResult,
    Relation,
    build_extraction_model,
    to_core_result,
)
from moray.orchestration.specialists import ReviewVerdict, rule_review


def test_slim_to_core_keeps_confidence_unknown():
    model = build_extraction_model(GENERAL_DOMAIN, slim=True)
    dyn = model.model_validate({
        "entities": [{"name": "华为", "type": "ORG"}],
        "relations": [{"head": "华为", "predicate": "位于", "tail": "深圳"}],
    })
    core = to_core_result(dyn)
    assert core.entities[0].confidence is None
    assert core.relations[0].confidence is None


def test_full_to_core_still_keeps_model_reported_confidence():
    model = build_extraction_model(GENERAL_DOMAIN, slim=False)
    dyn = model.model_validate({
        "entities": [{"name": "华为", "type": "ORG", "confidence": 0.42}],
        "relations": [],
    })
    core = to_core_result(dyn)
    assert core.entities[0].confidence == 0.42


class _FakeStore:
    """只记录 upsert 入参，验证 ingestor 不把未知伪造成 0.0。"""

    def __init__(self) -> None:
        self.entities: list[dict] = []
        self.relations: list[dict] = []

    async def upsert_entity(self, **kwargs):
        self.entities.append(kwargs)
        return len(self.entities)

    async def upsert_relation(self, **kwargs):
        self.relations.append(kwargs)
        return len(self.relations)

    async def add_relation_evidence(self, *args, **kwargs):
        return None


class _FakeEmbedder:
    def embed_one(self, text: str):
        return [0.0] * 512


async def test_ingestor_passes_unknown_confidence_through_as_none():
    from moray.extraction.document import ExtractionOutput

    store = _FakeStore()
    ing = Ingestor(store, embedder=_FakeEmbedder())
    output = ExtractionOutput(
        document_id="d1",
        domain_code="general",
        result=ExtractionResult(
            entities=[Entity(name="华为", type="ORG")],
            relations=[Relation(head="华为", predicate="位于", tail="深圳")],
        ),
    )
    await ing.ingest(output)
    assert store.entities[0]["confidence"] is None
    assert store.relations[0]["confidence"] is None


def test_rule_review_flags_evidence_less_relation_when_confidence_unknown():
    """slim 模式 confidence=None：无证据关系仍要被规则④拦下（r1 这里永不触发）。"""
    result = ExtractionResult(
        entities=[Entity(name="A", type="ORG"), Entity(name="B", type="ORG")],
        relations=[Relation(head="A", predicate="r", tail="B")],
    )
    review = rule_review("A 和 B。", result)
    assert review.verdict == ReviewVerdict.NEEDS_FIX
    assert any("证据" in issue for issue in review.issues)


def test_rule_review_consumes_backfill_block_confidence():
    """审校要消费确定性回填命中信号：未命中实体会被点名。"""
    result = ExtractionResult(
        entities=[Entity(name="幽灵公司", type="ORG")],
        relations=[],
    )
    backfill = {
        "block_confidence": 0.0,
        "entities_total": 1,
        "entities_grounded": 0,
        "ambiguous_mentions": [],
        "unmatched_aliases": [{"entity": "幽灵公司", "literal": "幽灵"}],
    }
    review = rule_review("华为在深圳。", result, backfill=backfill)
    assert review.verdict == ReviewVerdict.NEEDS_FIX
    joined = " ".join(review.issues)
    assert "命中" in joined and "0/1" in joined

    # 全部命中时不因回填再报问题
    ok = rule_review(
        "华为在深圳。",
        ExtractionResult(entities=[Entity(name="华为", type="ORG")], relations=[]),
        backfill={"block_confidence": 1.0, "entities_total": 1,
                  "entities_grounded": 1, "ambiguous_mentions": [],
                  "unmatched_aliases": []},
    )
    assert ok.verdict == ReviewVerdict.PASS


def test_extractor_slim_marks_confidence_unknown():
    ex = Extractor("general", max_retries=0, local_backfill=True)
    # schema 里已没有 confidence 字段，内核转换必须表达为未知而不是 0.0
    assert "confidence" not in ex._schema_model.model_fields["entities"].annotation.__args__[0].model_fields
