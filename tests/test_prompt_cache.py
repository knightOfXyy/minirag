"""提示缓存（prompt / prefix cache）相关改动的离线单测。

覆盖三件事：
1. 可观测：cached_input_tokens 从 Msg.usage 一路贯通到 Usage/看板；
2. 前缀稳定：与文档无关的规则全在 system prompt，user 消息只有固定引导 + 正文；
3. 主动预热：Extractor.warm_prefix() 只探一次、失败不抛，pipeline 起飞前调用。

全部用假模型返回，不联网、不依赖数据库。
"""

from types import SimpleNamespace

import pytest

from moray.extraction import ChunkedExtractionPipeline, Chunker, Document, Extractor
from moray.extraction.document import Usage
from moray.extraction.extractor import _usage_from_msg
from moray.schema import (
    TASK_PROTOCOL,
    DomainSpec,
    TypeSpec,
    build_system_prompt,
    get_domain,
)


# ---------------------------------------------------------------------------
# 假抽取器：记录每次真正发给模型的内容，可脚本化返回与抛错
# ---------------------------------------------------------------------------
class _ProbeExtractor(Extractor):
    def __init__(self, replies=None, *, fail=False, max_retries=1):
        super().__init__("general", max_retries=max_retries)
        self._replies = list(replies or [])
        self._fail = fail
        self.sent: list[str] = []
        self.probe_calls = 0

    def _build_agent(self):
        return SimpleNamespace()

    async def _call_structured(self, agent, content):
        self.probe_calls += 1
        self.sent.append(content)
        if self._fail:
            raise RuntimeError("端点没起来")
        reply = self._replies.pop(0) if self._replies else {"entities": [], "relations": []}
        return SimpleNamespace(
            structured_output=reply,
            usage=SimpleNamespace(input_tokens=100, output_tokens=5,
                                  cache_input_tokens=60),
        )


# ---------------------------------------------------------------------------
# 1. 可观测
# ---------------------------------------------------------------------------
def test_usage_from_msg_reads_cache_field():
    """agentscope 把网关的 cached_tokens 归一成 cache_input_tokens，我们要接住。"""
    msg = SimpleNamespace(usage=SimpleNamespace(
        input_tokens=1000, output_tokens=20, cache_input_tokens=800))
    u = _usage_from_msg(msg)
    assert (u.input_tokens, u.output_tokens, u.cached_input_tokens) == (1000, 20, 800)
    assert u.cache_hit_rate == pytest.approx(0.8)


def test_usage_from_msg_without_cache_field_is_zero():
    """老/假返回没有缓存字段时不能炸，命中率记 0。"""
    u = _usage_from_msg(SimpleNamespace(
        usage=SimpleNamespace(input_tokens=10, output_tokens=1)))
    assert u.cached_input_tokens == 0 and u.cache_hit_rate == 0.0
    assert _usage_from_msg(SimpleNamespace(usage=None)).input_tokens == 0


def test_usage_add_accumulates_cache_and_serializes_rate():
    a = Usage(input_tokens=100, output_tokens=5, cached_input_tokens=40)
    a.add(Usage(input_tokens=100, output_tokens=5, cached_input_tokens=60))
    assert a.cached_input_tokens == 100
    # computed_field：序列化自动带命中率，API/作业结果不用各处手算
    dumped = a.model_dump()
    assert dumped["cache_hit_rate"] == 0.5
    assert dumped["cached_input_tokens"] == 100


# ---------------------------------------------------------------------------
# 2. 前缀稳定
# ---------------------------------------------------------------------------
def test_system_prompt_is_byte_stable_and_doc_free():
    """同领域两次构造的系统提示词必须逐字节一致（否则缓存必然失效）。"""
    d1, d2 = get_domain("general"), get_domain("general")
    p1, p2 = build_system_prompt(d1), build_system_prompt(d2)
    assert p1 == p2
    assert TASK_PROTOCOL in p1            # 输出协议进 system，不再挤在 user 消息里
    assert "从 0 开始计数" in p1


def test_first_prompt_keeps_changing_text_last():
    """user 消息 = 固定引导 + 正文；固定部分跨块/跨文档共享，正文最后才出现。"""
    lead = Extractor._LEAD_IN
    a = Extractor._first_prompt("第一块正文……")
    b = Extractor._first_prompt("完全不相干的另一块正文……")
    assert a.startswith(lead) and b.startswith(lead)
    assert a.endswith("第一块正文……")
    # 两条消息的公共前缀至少等于引导语（真实可复用前缀还要加上 system）
    assert a[:len(lead)] == b[:len(lead)]


@pytest.mark.asyncio
async def test_retry_sends_only_feedback_not_text():
    """返工只追加纠错反馈：省 token，且不改动已经缓存的历史前缀。"""
    text = "华为技术有限公司宣布与贵阳市政府合作。"
    bad = {"entities": [{"name": "x", "type": "NOT_A_TYPE"}], "relations": []}
    good = {"entities": [{"name": "华为技术有限公司", "type": "ORG"}], "relations": []}
    ex = _ProbeExtractor([bad, good], max_retries=1)

    out = await ex.extract(Document(id="d1", text=text))
    assert out.attempts == 2
    first, retry = ex.sent[0], ex.sent[1]
    assert text in first                       # 首轮带正文
    assert text not in retry                   # 返工不重复发正文（历史里已有）
    assert "没有通过校验" in retry
    assert out.usage.cached_input_tokens == 120  # 两次尝试各 60
    assert out.usage.cache_hit_rate == pytest.approx(0.6)


# ---------------------------------------------------------------------------
# 3. 预热
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_warm_prefix_probes_once_per_instance():
    ex = _ProbeExtractor()
    probe = await ex.warm_prefix()
    assert probe.cached_input_tokens == 60     # 探测请求回报的命中量
    assert probe.input_tokens == 100
    assert ex.probe_calls == 1
    assert ex.sent[0].startswith(Extractor._LEAD_IN)  # 与真实请求同一条前缀路径

    await ex.warm_prefix()                     # 同一实例（=同一领域版本）只探一次
    assert ex.probe_calls == 1

    await ex.warm_prefix(force=True)           # 显式要求才重探
    assert ex.probe_calls == 2


@pytest.mark.asyncio
async def test_warm_prefix_failure_is_swallowed():
    """预热是优化不是功能：端点挂了也不能拖累抽取。"""
    ex = _ProbeExtractor(fail=True)
    assert (await ex.warm_prefix()).input_tokens == 0   # 不抛异常，返回空用量


@pytest.mark.asyncio
async def test_pipeline_warms_only_when_asked():
    """默认关（收益未确立就不静默多烧一次请求）；显式开才预热。"""
    text = "华为技术有限公司宣布与贵阳市政府合作。" * 5
    chunks = Chunker(max_units=40, overlap_units=0).chunk_text(text)
    assert len(chunks) >= 2

    default_off = _ProbeExtractor()
    await ChunkedExtractionPipeline(default_off, concurrency=2).run(
        Document(id="doc", text=text), chunks)
    assert default_off.probe_calls == len(chunks)          # 没有多发探测

    ex = _ProbeExtractor()
    out = await ChunkedExtractionPipeline(ex, concurrency=2, warm_prefix=True).run(
        Document(id="doc", text=text), chunks)
    assert ex.probe_calls == 1 + len(chunks)               # 1 次预热 + N 次抽块
    assert ex.sent[0].endswith("（预热）")                  # 探测走同一条前缀路径

    # 预热的开销确实计入了本篇成本（宁可多记不漏记）
    plain = _ProbeExtractor()
    out2 = await ChunkedExtractionPipeline(plain, concurrency=2).run(
        Document(id="doc", text=text), chunks)
    assert out.usage.input_tokens == out2.usage.input_tokens + 100
    assert out.usage.cached_input_tokens == out2.usage.cached_input_tokens + 60


@pytest.mark.asyncio
async def test_pipeline_skips_warmup_for_single_chunk():
    """单块文档预热等于白烧一次前缀，即使开了开关也跳过。"""
    text = "华为技术有限公司。"
    chunks = Chunker(max_units=200).chunk_text(text)
    assert len(chunks) == 1
    ex = _ProbeExtractor()
    await ChunkedExtractionPipeline(ex, warm_prefix=True).run(
        Document(id="doc", text=text), chunks)
    assert ex.probe_calls == 1


def test_prefix_version_differs_per_domain():
    """指纹不同 = 领域配置变了/换了领域，缓存要重新建，排查时以此为凭。"""
    a = _ProbeExtractor()
    b = _ProbeExtractor.__new__(_ProbeExtractor)  # 不跑 __init__，手工造两个领域
    dom = DomainSpec(
        code="finance", name="金融",
        types=(TypeSpec(code="ORG", label="机构", guide="公司/银行等"),))
    b.domain = dom
    b._system_prompt = build_system_prompt(dom)
    b.model_name = a.model_name
    assert a.prefix_version != b.prefix_version
    assert a.prefix_version.startswith("general:")


# ---------------------------------------------------------------------------
# 4. 输出侧成本开关（实测：一次简单问答也烧 458~796 reasoning token）
# ---------------------------------------------------------------------------
@pytest.fixture()
def clean_settings(monkeypatch):
    """清掉 get_settings 的缓存，测试结束后再清一次，避免污染其他用例。"""
    from moray import config

    config.get_settings.cache_clear()
    for var in ("ARK_REASONING_EFFORT", "ARK_THINKING"):
        monkeypatch.delenv(var, raising=False)
    yield config
    config.get_settings.cache_clear()


def test_no_env_means_no_extra_request_fields(clean_settings):
    """不设环境变量 = 不往请求体里塞任何新字段（模型层每个参数都 None 才不传）。"""
    params = clean_settings.build_chat_model().parameters
    assert params is not None            # agentscope 会补一个全 None 的默认对象
    assert params.model_dump(exclude_none=True) == {}


def test_reasoning_effort_is_plumbed_through(clean_settings):
    clean_settings.os.environ["ARK_REASONING_EFFORT"] = "minimal"
    clean_settings.get_settings.cache_clear()
    params = clean_settings.build_chat_model().parameters
    assert params is not None and params.reasoning_effort == "minimal"


def test_thinking_switch_is_plumbed_through(clean_settings):
    clean_settings.os.environ["ARK_THINKING"] = "disabled"
    clean_settings.get_settings.cache_clear()
    assert clean_settings.build_chat_model().parameters.thinking_enable is False


def test_invalid_switch_fails_fast(clean_settings):
    clean_settings.os.environ["ARK_REASONING_EFFORT"] = "ultra"
    clean_settings.get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="不支持"):
        clean_settings.build_chat_model()
