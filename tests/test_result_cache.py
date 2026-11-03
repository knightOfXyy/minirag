"""分块抽取结果内容级缓存的单测（离线、确定性、不联网）。

缓存是纯加速层：键覆盖领域配置指纹、模型名、分块正文；命中不调模型、
不把命中计成真实 token；同进程并发同键只抽一次（单飞）；后端损坏自动降级。
"""

import pytest

from moray.extraction.cache import (
    ContentResultCache,
    InMemoryCacheBackend,
    build_cache_key,
)
from moray.extraction.document import ExtractionOutput, Usage
from moray.schema import Entity, EntityMention, ExtractionResult, Span


def _output(name: str = "华为", *, inp: int = 10, out: int = 4) -> ExtractionOutput:
    return ExtractionOutput(
        document_id="chunk",
        domain_code="general",
        result=ExtractionResult(
            entities=[
                Entity(
                    name=name,
                    type="ORG",
                    mentions=[
                        EntityMention(surface=name, span=Span(start=0, end=len(name), text=name))
                    ],
                )
            ]
        ),
        model_name="fake-model",
        usage=Usage(input_tokens=inp, output_tokens=out),
        attempts=1,
    )


# ---------------------------------------------------------------------------
# 键：内容/领域指纹/模型 任一变化即换键
# ---------------------------------------------------------------------------
def test_key_changes_when_content_changes():
    k1 = build_cache_key("fp1", "model-a", "正文甲")
    k2 = build_cache_key("fp1", "model-a", "正文乙")
    assert k1 != k2


def test_key_changes_when_domain_fingerprint_changes():
    k1 = build_cache_key("fp1", "model-a", "同一段正文")
    k2 = build_cache_key("fp2", "model-a", "同一段正文")
    assert k1 != k2


def test_key_changes_when_model_changes():
    k1 = build_cache_key("fp1", "model-a", "同一段正文")
    k2 = build_cache_key("fp1", "model-b", "同一段正文")
    assert k1 != k2


def test_key_stable_for_identical_inputs():
    assert (
        build_cache_key("fp", "m", "正文")
        == build_cache_key("fp", "m", "正文")
    )


def test_prompt_template_version_is_part_of_key(monkeypatch):
    """提示模板/生成参数版本变化也要换键（N11）：改 _LEAD_IN 不失效的风险用版本号兜住。"""
    import moray.extraction.cache as cache_mod

    before = build_cache_key("fp", "m", "正文")
    monkeypatch.setattr(cache_mod, "_PROMPT_TEMPLATE_VERSION", 2)
    after = build_cache_key("fp", "m", "正文")
    assert before != after


# ---------------------------------------------------------------------------
# 命中/未命中
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_miss_calls_extractor_then_hit_does_not():
    cache = ContentResultCache(InMemoryCacheBackend())
    calls = {"n": 0}

    async def extract():
        calls["n"] += 1
        return _output()

    o1, hit1 = await cache.get_or_extract(
        domain_fingerprint="fp1", model_name="m", chunk_text="正文",
        extract=extract)
    o2, hit2 = await cache.get_or_extract(
        domain_fingerprint="fp1", model_name="m", chunk_text="正文",
        extract=extract)

    assert hit1 is False and hit2 is True
    assert calls["n"] == 1
    # 命中还原出逐字段一致的结果
    assert o1.result.model_dump() == o2.result.model_dump()
    assert o2.model_dump() == o1.model_dump()


@pytest.mark.asyncio
async def test_hit_returns_independent_copy():
    """命中返回的对象与缓存里的不是同一份：调用方改写不污染缓存。"""
    cache = ContentResultCache(InMemoryCacheBackend())

    async def extract():
        return _output()

    out, _ = await cache.get_or_extract(
        domain_fingerprint="fp", model_name="m", chunk_text="正文",
        extract=extract)
    out.result.entities[0].name = "被改写"
    again, hit = await cache.get_or_extract(
        domain_fingerprint="fp", model_name="m", chunk_text="正文",
        extract=extract)
    assert hit is True
    assert again.result.entities[0].name == "华为"


@pytest.mark.asyncio
async def test_content_change_invalidates_and_recalls_extractor():
    cache = ContentResultCache(InMemoryCacheBackend())
    calls = {"n": 0}

    async def extract():
        calls["n"] += 1
        return _output()

    await cache.get_or_extract("fp", "m", "正文一", extract)
    out, hit = await cache.get_or_extract("fp", "m", "正文二", extract)
    assert hit is False
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_domain_fingerprint_change_invalidates():
    cache = ContentResultCache(InMemoryCacheBackend())
    calls = {"n": 0}

    async def extract():
        calls["n"] += 1
        return _output()

    await cache.get_or_extract("fp-v1", "m", "同正文", extract)
    _, hit = await cache.get_or_extract("fp-v2", "m", "同正文", extract)
    assert hit is False
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_model_change_invalidates():
    cache = ContentResultCache(InMemoryCacheBackend())
    calls = {"n": 0}

    async def extract():
        calls["n"] += 1
        return _output()

    await cache.get_or_extract("fp", "model-a", "同正文", extract)
    _, hit = await cache.get_or_extract("fp", "model-b", "同正文", extract)
    assert hit is False
    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# 单飞：并发同键只抽一次
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_concurrent_same_key_extracts_once():
    cache = ContentResultCache(InMemoryCacheBackend())
    calls = {"n": 0}

    async def extract():
        calls["n"] += 1
        return _output()

    import asyncio

    results = await asyncio.gather(
        *[cache.get_or_extract(
            "fp", "m", "并发同正文", extract) for _ in range(8)])
    assert calls["n"] == 1
    assert [r.result.model_dump() for r, _ in results] == (
        [results[0][0].result.model_dump()] * 8)


# ---------------------------------------------------------------------------
# 损坏/不可读后端：降级为正常抽取，不抛、不丢结果
# ---------------------------------------------------------------------------
class _BrokenBackend:
    async def get(self, key):
        raise RuntimeError("磁盘不可读")

    async def set(self, key, value):
        raise RuntimeError("磁盘不可写")


@pytest.mark.asyncio
async def test_broken_backend_degrades_to_normal_extraction():
    cache = ContentResultCache(_BrokenBackend())
    calls = {"n": 0}

    async def extract():
        calls["n"] += 1
        return _output()

    o1, hit1 = await cache.get_or_extract("fp", "m", "正文", extract)
    o2, hit2 = await cache.get_or_extract("fp", "m", "正文", extract)
    assert calls["n"] == 2          # 写不进去 → 每次都真抽
    assert hit1 is False and hit2 is False
    assert o1.result.model_dump() == o2.result.model_dump()


class _CorruptPayloadBackend:
    def __init__(self):
        self.store = {}

    async def get(self, key):
        return "{这不是合法json"

    async def set(self, key, value):
        self.store[key] = value


@pytest.mark.asyncio
async def test_corrupt_payload_is_treated_as_miss():
    cache = ContentResultCache(_CorruptPayloadBackend())
    calls = {"n": 0}

    async def extract():
        calls["n"] += 1
        return _output()

    out, hit = await cache.get_or_extract("fp", "m", "正文", extract)
    assert hit is False
    assert calls["n"] == 1
    assert out.result.entities[0].name == "华为"


@pytest.mark.asyncio
async def test_concurrent_singleflight_failure_propagates_and_clears():
    """抽取者失败时等待者收到同一异常，且键被释放后可再次正常抽取（不永久卡单飞）。"""
    import asyncio

    cache = ContentResultCache(InMemoryCacheBackend())
    attempt = {"n": 0}

    async def flaky():
        attempt["n"] += 1
        await asyncio.sleep(0)          # 真实抽取在模型调用处必然让出事件循环
        if attempt["n"] == 1:
            raise RuntimeError("模型超时")
        return _output()

    key_text = "同键失败后重试"
    # 制造同键并发：全部都在第一次抽取上汇合
    async def call():
        return await cache.get_or_extract("fp", "m", key_text, flaky)

    results = await asyncio.gather(
        *[call() for _ in range(4)], return_exceptions=True)
    assert all(isinstance(r, RuntimeError) for r in results)
    assert attempt["n"] == 1                 # 仍只真实调用一次
    # 单飞已释放：下一次调用重新抽取并成功、随后可命中
    out, hit = await cache.get_or_extract("fp", "m", key_text, flaky)
    assert hit is False and attempt["n"] == 2
    out2, hit2 = await cache.get_or_extract("fp", "m", key_text, flaky)
    assert hit2 is True and attempt["n"] == 2


@pytest.mark.asyncio
async def test_single_call_failure_leaves_no_unretrieved_future_exception():
    """单块失败且无并发等待者时，不能留下没人取异常的 future（S6）。"""
    import asyncio
    import gc

    cache = ContentResultCache(InMemoryCacheBackend())
    unhandled = []
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _l, ctx: unhandled.append(ctx))

    async def boom():
        raise RuntimeError("模型超时")

    try:
        with pytest.raises(RuntimeError):
            await cache.get_or_extract("fp", "m", "单块失败", boom)
        gc.collect()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous)

    messages = [str(ctx.get("message", "")) for ctx in unhandled]
    assert not any("never retrieved" in m for m in messages), messages


class _SlowGetBackend:
    """get 会真实让出事件循环，模拟 postgres 网络往返；统计并发读峰值。"""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.active = 0
        self.max_active = 0

    async def get(self, key):
        import asyncio

        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.02)
            return self.store.get(key)
        finally:
            self.active -= 1

    async def set(self, key, value):
        self.store[key] = value


@pytest.mark.asyncio
async def test_backend_reads_are_not_serialized_by_global_inflight_lock():
    """不同键的缓存读不能排在同一把全局锁里（S5）：并发读峰值应 > 1。"""
    import asyncio

    backend = _SlowGetBackend()
    cache = ContentResultCache(backend)

    async def extract():
        return _output()

    await asyncio.gather(
        cache.get_or_extract("fp", "m", "键A", extract),
        cache.get_or_extract("fp", "m", "键B", extract),
    )
    assert backend.max_active >= 2


def test_memory_backend_evicts_least_recently_used():
    """进程内后端必须有上限，长跑 worker 不能只增不减（N6）。"""
    import asyncio

    backend = InMemoryCacheBackend(max_entries=2)
    asyncio.run(backend.set("a", "1"))
    asyncio.run(backend.set("b", "2"))
    assert asyncio.run(backend.get("a")) == "1"   # 访问把 a 提到最近使用
    asyncio.run(backend.set("c", "3"))            # 淘汰最久未用的 b
    assert asyncio.run(backend.get("b")) is None
    assert asyncio.run(backend.get("a")) == "1"
    assert asyncio.run(backend.get("c")) == "3"


# ---------------------------------------------------------------------------
# 端到端：接入 ChunkedExtractionPipeline
# ---------------------------------------------------------------------------
from moray.extraction.pipeline import ChunkedExtractionPipeline  # noqa: E402
from moray.extraction.chunking import Chunk, Chunker  # noqa: E402
from moray.schema import DomainSpec, TypeSpec  # noqa: E402


class _CountingRealExtractor:
    """满足 pipeline 契约并带 domain_fingerprint；对同一段正文确定性产出。"""

    def __init__(self, domain, model_name="fake-model"):
        self.domain = domain
        self.model_name = model_name
        self.calls = 0

    async def extract(self, document):
        # 真实模型调用必然让出事件循环；sleep(0) 让同键并发都进入单飞关键区。
        import asyncio

        await asyncio.sleep(0)
        self.calls += 1
        name = document.text[:6]
        res = ExtractionResult(entities=[Entity(
            name=name, type="ORG",
            mentions=[EntityMention(surface=name,
                                    span=Span(start=0, end=len(name), text=name))])])
        return ExtractionOutput(
            document_id=document.id, domain_code=self.domain.code,
            result=res, model_name=self.model_name,
            usage=Usage(input_tokens=3, output_tokens=2), attempts=1)


def _long_text():
    return "".join(f"第{n}段记载某某事件{n}。" for n in range(1, 40))


def _chunks(text):
    return Chunker(max_units=40, overlap_units=0).chunk_text(text)


def _pipeline(real, cache=None):
    return ChunkedExtractionPipeline(
        real, concurrency=2, result_cache=cache)


@pytest.mark.asyncio
async def test_pipeline_second_run_makes_no_calls_and_matches_exactly():
    text = _long_text()
    real = _CountingRealExtractor(
        DomainSpec(code="general", name="通用",
                   types=(TypeSpec("ORG", "机构", "机构"),)))
    cache = ContentResultCache(InMemoryCacheBackend())
    doc1 = __import__("moray.extraction.document", fromlist=["Document"]).Document(
        id="docA", text=text, source="t", domain_code="general")

    out1 = await _pipeline(real, cache).run(doc1, _chunks(text))
    calls_first = real.calls
    out2 = await _pipeline(real, cache).run(doc1, _chunks(text))

    assert calls_first >= 2                       # 第一次确实抽了多块
    assert real.calls == calls_first              # 第二次零模型调用
    assert out2.result.model_dump() == out1.result.model_dump()
    # 全局偏移逐字段一致，且都能对回原文
    for e in out2.result.entities:
        for m in e.mentions:
            assert m.span is not None
            assert text[m.span.start:m.span.end] == m.span.text
    assert out2.usage.cache_hits == len(_chunks(text))
    # 命中不产生任何真实 token
    assert out2.usage.input_tokens == 0
    assert out2.usage.output_tokens == 0


@pytest.mark.asyncio
async def test_pipeline_without_cache_keeps_original_behavior():
    text = _long_text()
    real = _CountingRealExtractor(
        DomainSpec(code="general", name="通用",
                   types=(TypeSpec("ORG", "机构", "机构"),)))
    doc = __import__("moray.extraction.document", fromlist=["Document"]).Document(
        id="docA", text=text, source="t", domain_code="general")
    chunks = _chunks(text)
    out = await _pipeline(real, None).run(doc, chunks)
    assert real.calls == len(chunks)             # 每块都真抽
    assert out.usage.cache_hits == 0
    assert out.usage.input_tokens == 3 * len(chunks)
    assert out.usage.output_tokens == 2 * len(chunks)


@pytest.mark.asyncio
async def test_pipeline_content_edit_invalidates_cache():
    text1 = _long_text()
    real = _CountingRealExtractor(
        DomainSpec(code="general", name="通用",
                   types=(TypeSpec("ORG", "机构", "机构"),)))
    cache = ContentResultCache(InMemoryCacheBackend())
    Document = __import__("moray.extraction.document", fromlist=["Document"]).Document
    await _pipeline(real, cache).run(
        Document(id="d", text=text1, source="t"), _chunks(text1))
    calls_after_first = real.calls

    # 改正文中一个字（同长度替换，分块边界不变，便于精确断言）
    text2 = text1.replace("第2段", "第２段", 1)
    chunks2 = _chunks(text2)
    assert len(chunks2) == len(_chunks(text1))
    out2 = await _pipeline(real, cache).run(
        Document(id="d", text=text2, source="t"), chunks2)
    # 只有含被改字符的那一块真抽，其余块全部命中
    assert real.calls == calls_after_first + 1
    assert out2.usage.cache_hits == len(chunks2) - 1


@pytest.mark.asyncio
async def test_pipeline_domain_or_model_change_invalidates_cache():
    text = _long_text()
    chunks = _chunks(text)
    Document = __import__("moray.extraction.document", fromlist=["Document"]).Document
    doc = Document(id="d", text=text, source="t")

    def domain(code):
        return DomainSpec(code=code, name="通用",
                          types=(TypeSpec("ORG", "机构", "机构"),))

    real = _CountingRealExtractor(domain("general"))
    cache = ContentResultCache(InMemoryCacheBackend())
    await _pipeline(real, cache).run(doc, chunks)
    n1 = real.calls

    # 换模型名：指纹同、模型不同 → 全部重抽
    real.model_name = "another-model"
    await _pipeline(real, cache).run(doc, chunks)
    assert real.calls == n1 + len(chunks)

    # 切领域（指纹变化）：同样全部重抽
    real2 = _CountingRealExtractor(
        DomainSpec(code="finance", name="金融",
                   types=(TypeSpec("ORG", "机构", "机构"),)),
        model_name="another-model")
    await _pipeline(real2, cache).run(doc, chunks)
    assert real2.calls == len(chunks)


# ---------------------------------------------------------------------------
# 编排入口 run_chunked_team：缓存贯穿断点续跑/重试/重复提交
# ---------------------------------------------------------------------------
class _TeamProvider:
    def __init__(self, real):
        self._real = real

    async def get_extractor(self, domain_code):
        return self._real, self._real.domain


@pytest.mark.asyncio
async def test_chunked_team_resume_second_submission_uses_cache():
    """同一长文第二次抽取（续跑/重试/重复提交）：全命中、零真实 token。"""
    from moray.orchestration import run_chunked_team

    text = _long_text()
    real = _CountingRealExtractor(
        DomainSpec(code="general", name="通用",
                   types=(TypeSpec("ORG", "机构", "机构"),)))
    provider = _TeamProvider(real)
    cache = ContentResultCache(InMemoryCacheBackend())

    kwargs = dict(
        extractor=None, provider=provider, domain_code="general",
        chunk_threshold_units=20, chunk_max_units=40, chunk_overlap_units=0,
        chunk_concurrency=2, result_cache=cache)

    async def closure(text, feedback):
        raise AssertionError("chunk path 不应走整篇闭包")

    u1 = Usage()
    out1 = await run_chunked_team(text, **kwargs, usage=u1)
    n_chunks = len(_chunks(text))
    assert real.calls == n_chunks

    u2 = Usage()
    out2 = await run_chunked_team(text, **kwargs, usage=u2)
    assert real.calls == n_chunks                 # 第二次没有新模型调用
    assert out2.result.model_dump() == out1.result.model_dump()
    assert u2.cache_hits == n_chunks
    assert u2.input_tokens == 0 and u2.output_tokens == 0


@pytest.mark.asyncio
async def test_chunked_team_without_cache_arg_keeps_old_behavior():
    """不传 result_cache：分块路径逐字节保持旧行为（每块真抽）。"""
    from moray.orchestration import run_chunked_team

    text = _long_text()
    real = _CountingRealExtractor(
        DomainSpec(code="general", name="通用",
                   types=(TypeSpec("ORG", "机构", "机构"),)))
    provider = _TeamProvider(real)
    u = Usage()
    await run_chunked_team(
        text, extractor=None, provider=provider, domain_code="general",
        chunk_threshold_units=20, chunk_max_units=40, chunk_overlap_units=0,
        chunk_concurrency=2, usage=u)
    n_chunks = len(_chunks(text))
    assert real.calls == n_chunks
    assert u.cache_hits == 0
    assert u.input_tokens == 3 * n_chunks


@pytest.mark.asyncio
async def test_domain_enum_edit_changes_fingerprint_and_invalidates():
    """改实体类型枚举（领域配置版本变化）→ 指纹变化 → 全部重抽。

    直接验证领域指纹这一缓存键要素：枚举增删必须换指纹，这是 schema 版本
    失效的独立证据（不依赖 DB updated_at）。
    """
    from moray.schema import domain_fingerprint as fp

    base = DomainSpec(code="g", name="通用",
                      types=(TypeSpec("ORG", "机构", "机构"),))
    with_new_enum = DomainSpec(
        code="g", name="通用",
        types=(TypeSpec("ORG", "机构", "机构"),
               TypeSpec("PRODUCT", "产品", "产品")))
    assert fp(base) != fp(with_new_enum)

    text = _long_text()
    chunks = _chunks(text)
    Document = __import__("moray.extraction.document", fromlist=["Document"]).Document
    doc = Document(id="d", text=text, source="t")
    cache = ContentResultCache(InMemoryCacheBackend())

    real = _CountingRealExtractor(base)
    await _pipeline(real, cache).run(doc, chunks)
    n1 = real.calls

    real2 = _CountingRealExtractor(with_new_enum)
    await _pipeline(real2, cache).run(doc, chunks)
    assert real2.calls == len(chunks)     # 指纹不同 → 没有任何命中
    assert real.calls == n1               # 旧领域结果也未被污染


@pytest.mark.asyncio
async def test_cached_usage_excluded_from_real_cost_fields():
    """命中缓存的分块只增加 cache_hits，不增加任何真实 token 计量字段。

    看板/成本统计汇总的是 input_tokens/output_tokens/cached_input_tokens
    （都是真实 LLM 计量）；cache_hits 是独立的“省了多少次调用”计数，成本
    不会把命中误算成真实 token。
    """
    text = _long_text()
    real = _CountingRealExtractor(
        DomainSpec(code="general", name="通用",
                   types=(TypeSpec("ORG", "机构", "机构"),)))
    cache = ContentResultCache(InMemoryCacheBackend())
    Document = __import__("moray.extraction.document", fromlist=["Document"]).Document
    doc = Document(id="d", text=text, source="t")
    pipe = _pipeline(real, cache)

    out1 = await pipe.run(doc, _chunks(text))
    n_chunks = len(_chunks(text))
    assert out1.usage.input_tokens == 3 * n_chunks
    assert out1.usage.output_tokens == 2 * n_chunks

    out2 = await pipe.run(doc, _chunks(text))
    # 第二次：真实 token 增量为 0，只有 cache_hits
    assert out2.usage.input_tokens == 0
    assert out2.usage.output_tokens == 0
    assert out2.usage.cached_input_tokens == 0
    assert out2.usage.cache_hits == n_chunks
    # 命中时缓存里还原出来的用量记到独立的「复用」字段，不计真实成本（S4）
    assert out2.usage.reused_input_tokens == 3 * n_chunks
    assert out2.usage.reused_output_tokens == 2 * n_chunks
    # 序列化（作业结果/API 响应/看板都走 model_dump）带上这一列
    dumped = out2.usage.model_dump()
    assert dumped["cache_hits"] == n_chunks
    assert dumped["input_tokens"] == 0
    # 提示缓存命中率仍是真实 token 口径，不被内容缓存命中污染（分母 0 → 0）
    assert dumped["cache_hit_rate"] == 0.0


@pytest.mark.asyncio
async def test_pipeline_concurrent_same_chunk_text_extracts_once():
    """多个协程并发抽取【同一内容】的分块时只发生一次真实模型调用（单飞）。

    用同一段正文造多个不同位置的块：键不含位置，故同内容块应去重为一次调用；
    偏移换算仍各自按块位置平移，结果位置正确。
    """
    body = "华为技术有限公司位于深圳。"
    # 同内容、不同 start 的块（位置只影响换算，不进键）
    same_chunks = [
        Chunk(id=f"d#c{i:04d}", text=body, start=10 * i,
              end=10 * i + len(body), index=i)
        for i in range(6)
    ]
    real = _CountingRealExtractor(
        DomainSpec(code="general", name="通用",
                   types=(TypeSpec("ORG", "机构", "机构"),)))
    cache = ContentResultCache(InMemoryCacheBackend())
    Document = __import__("moray.extraction.document", fromlist=["Document"]).Document
    doc = Document(id="d", text=body, source="t")

    out = await _pipeline(real, cache).run(doc, same_chunks)
    assert real.calls == 1                 # 同内容只真抽一次
    assert out.usage.cache_hits == 5
    # 每个块都按自己的 start 换算：实体名一致、全局偏移各不相同
    offsets = sorted(m.span.start for e in out.result.entities
                     for m in e.mentions if m.span)
    assert offsets == [10 * i for i in range(6)]


@pytest.mark.asyncio
async def test_domain_fingerprint_computed_once_per_pipeline(monkeypatch):
    """每个分块都重建一次领域模型是纯浪费（N2）：一次 pipeline.run 只算一次指纹。"""
    import moray.extraction.pipeline as pipeline_mod

    calls = {"n": 0}
    original = pipeline_mod.domain_fingerprint

    def counting(domain):
        calls["n"] += 1
        return original(domain)

    monkeypatch.setattr(pipeline_mod, "domain_fingerprint", counting)

    text = _long_text()
    real = _CountingRealExtractor(
        DomainSpec(code="general", name="通用",
                   types=(TypeSpec("ORG", "机构", "机构"),)))
    cache = ContentResultCache(InMemoryCacheBackend())
    Document = __import__("moray.extraction.document", fromlist=["Document"]).Document
    pipe = _pipeline(real, cache)
    await pipe.run(Document(id="d", text=text, source="t"), _chunks(text))
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_non_domain_spec_fallback_includes_full_identity():
    """非 DomainSpec 的假对象也不能只退化成 code：不同 domain 内容必须换键（N3）。"""
    from types import SimpleNamespace

    class _FakeExtractor:
        def __init__(self, domain):
            self.domain = domain
            self.model_name = "fake-model"
            self.calls = 0

        async def extract(self, document):
            self.calls += 1
            return ExtractionOutput(
                document_id=document.id, domain_code=self.domain.code,
                result=ExtractionResult(), model_name=self.model_name,
                usage=Usage(input_tokens=1), attempts=1)

    text = _long_text()
    chunks = _chunks(text)
    Document = __import__("moray.extraction.document", fromlist=["Document"]).Document
    cache = ContentResultCache(InMemoryCacheBackend())

    a = _FakeExtractor(SimpleNamespace(code="g", types=("ORG",)))
    b = _FakeExtractor(SimpleNamespace(code="g", types=("ORG", "PER")))
    await _pipeline(a, cache).run(Document(id="d", text=text, source="t"), chunks)
    await _pipeline(b, cache).run(Document(id="d", text=text, source="t"), chunks)
    assert b.calls == len(chunks)      # 同一 code 但配置内容不同 → 不命中


