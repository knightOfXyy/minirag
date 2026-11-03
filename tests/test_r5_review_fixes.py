"""工单 03 第 5 轮评审（r5）发现的回归用例。

只覆盖本轮要修的 should / nit 中可离线钉住的行为：

* F1：唯一别名端点只在瘦身模式（有 backfill 信号）下被审校接受；
* F2：歧义未归属是质量信号，不得进返工 feedback（不做模型补问）；
* F3：整篇别名补扫用 source_text，而不是可能追加了反馈的 text；
* F5：aggregate.delta 的 *_min_* 取各轮最坏值，不再求均值；
* F6：temperature 可发现（.env.example + describe）；
* F7：采样参数进分块内容缓存键，改温度不得复用旧产物；
* F9：领域指纹钉死 full/slim 期望值（任何无关变更都会显形）。
"""

import sys
from pathlib import Path
from types import SimpleNamespace

from moray.config import describe
from moray.extraction import Chunk, Document, ExtractionOutput
from moray.extraction.document import Usage
from moray.extraction.cache import ContentResultCache, InMemoryCacheBackend
from moray.extraction.local_backfill import backfill_provenance
from moray.extraction.pipeline import ChunkedExtractionPipeline
from moray.orchestration.specialists import (
    ReviewVerdict,
    rule_review,
    run_extraction_team,
)
from moray.schema import Entity, ExtractionResult, Relation

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

from tools.backfill_stress_ab import _aggregate_delta  # noqa: E402


# ---------------------------------------------------------------------------
# F1：唯一别名端点只在瘦身模式被接受
# ---------------------------------------------------------------------------
def _alias_endpoint_result():
    text = "鹅厂发布了混元。"
    result, report = backfill_provenance(
        ExtractionResult(
            entities=[
                Entity(name="腾讯", type="ORG", aliases=["鹅厂"]),
                Entity(name="混元", type="MISC"),
            ],
            relations=[Relation(head="鹅厂", predicate="发布", tail="混元")],
        ),
        text,
    )
    return text, result, report


def test_rule_review_rejects_unique_alias_endpoint_when_backfill_disabled():
    """关闭态/full 模式保持改造前的严格口径：别名端点仍报 ②。"""
    text, result, _report = _alias_endpoint_result()
    review = rule_review(text, result, backfill=None)
    assert review.verdict == ReviewVerdict.NEEDS_FIX
    assert any("关系头实体「鹅厂」" in issue for issue in review.issues)


def test_rule_review_accepts_unique_alias_endpoint_in_slim_mode():
    """瘦身模式才放宽：唯一归属的别名是合法端点。"""
    text, result, report = _alias_endpoint_result()
    review = rule_review(text, result, backfill=report.to_dict())
    assert review.verdict == ReviewVerdict.PASS, review.issues


# ---------------------------------------------------------------------------
# F2：歧义未归属只做信号，不进返工 feedback
# ---------------------------------------------------------------------------
async def test_ambiguous_mention_does_not_trigger_model_rework():
    # 最苛刻的输入：正文只有歧义字面，没有任何可归属的标准名。
    # 此时 block_confidence 天然为 0，但歧义不是模型返工能解决的问题。
    text = "同名发布了混元。"
    logical = ExtractionResult(entities=[
        Entity(name="腾讯", type="ORG", aliases=["同名"]),
        Entity(name="网易", type="ORG", aliases=["同名"]),
    ])
    sink: dict = {}
    calls: list[str | None] = []

    async def extractor(body: str, feedback: str | None):
        calls.append(feedback)
        result, report = backfill_provenance(logical, body)
        sink["backfill"] = report.to_dict()
        return result

    reviewed = await run_extraction_team(
        text, extractor=extractor, backfill_sink=sink)

    assert len(calls) == 1, "歧义不得触发模型补问/返工"
    assert reviewed.accepted is True
    assert reviewed.backfill["ambiguous_mentions"], "歧义仍须作为质量信号保留"


def test_true_missing_entity_still_triggers_rework_alongside_ambiguity():
    """不搞一刀切：歧义不返工，但真正未命中的实体仍要被规则审校拦下。"""
    text = "同名发布了混元。"
    result, report = backfill_provenance(
        ExtractionResult(entities=[
            Entity(name="腾讯", type="ORG", aliases=["同名"]),
            Entity(name="网易", type="ORG", aliases=["同名"]),
            Entity(name="幽灵公司", type="ORG"),
        ]),
        text,
    )
    review = rule_review(text, result, backfill=report.to_dict())
    assert review.verdict == ReviewVerdict.NEEDS_FIX
    assert any("回填命中率 0/3" in issue for issue in review.issues)


# ---------------------------------------------------------------------------
# F3：整篇别名补扫用 source_text
# ---------------------------------------------------------------------------
class _AliasOnlyInFeedbackFake:
    domain = SimpleNamespace(code="general")
    model_name = "fake-r5"
    local_backfill = True

    async def extract(self, document: Document):
        logical = ExtractionResult(entities=[
            Entity(name="腾讯", type="ORG", aliases=["鹅厂"])])
        result, report = backfill_provenance(logical, document.text)
        return ExtractionOutput(
            document_id=document.id, domain_code="general",
            result=result, model_name=self.model_name, usage=Usage(),
            attempts=1, backfill=report.to_dict(),
        )


async def test_whole_document_alias_scan_uses_source_text_not_feedback():
    original = "腾讯发布财报。"
    body = original + "\n【上次问题】鹅厂 未出现"
    chunks = [Chunk(id="d#c0", text=original, start=0,
                    end=len(original), index=0)]
    out = await ChunkedExtractionPipeline(
        _AliasOnlyInFeedbackFake(), concurrency=1).run(
            Document(id="d", text=body, source_text=original), chunks)

    # 「鹅厂」只在反馈里出现，原始正文没有 -> 必须仍算幻觉
    assert out.backfill["unmatched_aliases"] == [
        {"entity": "腾讯", "literal": "鹅厂"}]
    assert "鹅厂" not in out.backfill["seen_literals"]


# ---------------------------------------------------------------------------
# F5：aggregate.delta 的 min 字段取最坏值
# ---------------------------------------------------------------------------
def test_aggregate_delta_min_fields_take_worst_value():
    deltas = [
        {"span_ratio_min_before": 0.0110, "truncated_chunks_before": 2,
         "entity_macro_f1_delta_pt": 1.0},
        {"span_ratio_min_before": 0.0099, "truncated_chunks_before": 6,
         "entity_macro_f1_delta_pt": -1.3},
        {"span_ratio_min_before": 0.0120, "truncated_chunks_before": 4,
         "entity_macro_f1_delta_pt": 0.52},
    ]
    agg = _aggregate_delta(deltas)
    assert agg["span_ratio_min_before"] == 0.0099
    assert agg["truncated_chunks_before"] == 6
    # 非 min / 非截断指标仍是均值
    assert agg["entity_macro_f1_delta_pt"] == 0.0733


# ---------------------------------------------------------------------------
# F6：temperature 可发现
# ---------------------------------------------------------------------------
def test_env_example_documents_temperature():
    content = (_REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "LLM_TEMPERATURE" in content


def test_describe_includes_temperature(monkeypatch):
    monkeypatch.setenv("ARK_API_KEY", "ark-fake-key-for-tests-0000")
    from moray.config import get_settings

    get_settings.cache_clear()
    d = describe()
    assert "temperature" in d
    assert d["temperature"] is None


# ---------------------------------------------------------------------------
# F7：采样参数进缓存键
# ---------------------------------------------------------------------------
def test_cache_key_changes_with_sampling_fingerprint():
    import inspect

    from moray.extraction.cache import build_cache_key

    assert "sampling_fingerprint" in inspect.signature(build_cache_key).parameters
    k_none = build_cache_key("fp", "m", "正文", sampling_fingerprint="temp=None")
    k_zero = build_cache_key("fp", "m", "正文", sampling_fingerprint="temp=0.0")
    assert k_none != k_zero


class _SamplingFake:
    domain = SimpleNamespace(code="general")
    model_name = "m"
    local_backfill = False

    def __init__(self, sampling: str):
        self.sampling_fingerprint = sampling
        self.calls = 0

    async def extract(self, document: Document):
        self.calls += 1
        return ExtractionOutput(
            document_id=document.id, domain_code="general",
            result=ExtractionResult(entities=[Entity(name="腾讯", type="ORG")]),
            model_name=self.model_name, usage=Usage(), attempts=1,
        )


async def test_shared_cache_does_not_reuse_result_across_sampling_params():
    text = "腾讯发布财报。"
    cache = ContentResultCache(InMemoryCacheBackend())
    low = _SamplingFake("temp=0.0")
    high = _SamplingFake("temp=1.0")
    chunks = [Chunk(id="d#c0", text=text, start=0, end=len(text), index=0)]

    await ChunkedExtractionPipeline(
        low, concurrency=1, result_cache=cache).run(
            Document(id="d", text=text), chunks)
    await ChunkedExtractionPipeline(
        high, concurrency=1, result_cache=cache).run(
            Document(id="d", text=text), chunks)

    assert low.calls == 1
    assert high.calls == 1, "采样参数不同不得命中同一缓存键"


# ---------------------------------------------------------------------------
# F9：指纹钉死期望值（改在 tests/test_r4_review_fixes.py 的原用例上）
# ---------------------------------------------------------------------------
