"""抽取服务测试：不联网，用假返回验证"校验失败自动重试"逻辑。"""

from types import SimpleNamespace

import pytest

from moray.extraction import Document, Extractor, ExtractionError, verify_spans
from moray.schema import Entity, ExtractionResult


class _FakeExtractor(Extractor):
    """覆盖真正的模型调用：按预设的返回序列逐次返回。"""

    def __init__(self, replies):
        super().__init__("general", max_retries=2)
        self._replies = list(replies)
        self._calls = 0
        self.sent_contents: list[str] = []

    def _build_agent(self):
        # 用 SimpleNamespace：真实 extract() 会在 agent 上挂 _audit_source_text
        return SimpleNamespace()

    async def _call_structured(self, agent, content):
        self.sent_contents.append(content)
        reply = self._replies[self._calls]
        self._calls += 1
        usage = SimpleNamespace(input_tokens=10, output_tokens=5)
        return SimpleNamespace(structured_output=reply, usage=usage)


def _valid_output(name="华为", etype="ORG"):
    return {
        "entities": [{"name": name, "type": etype}],
        "relations": [],
    }


@pytest.mark.asyncio
async def test_success_on_first_try():
    ex = _FakeExtractor([_valid_output()])
    out = await ex.extract(Document(id="d1", text="华为"))
    assert out.attempts == 1
    assert out.result.entities[0].name == "华为"
    # token 被累计
    assert out.usage.input_tokens == 10
    # 第一次提示词不含"校验"重试语义
    assert "没有通过校验" not in ex.sent_contents[0]


@pytest.mark.asyncio
async def test_retry_then_success():
    bad = {
        # 非法领域类型 -> 动态模型校验失败
        "entities": [{"name": "某主体", "type": "企业集团"}],
        "relations": [],
    }
    ex = _FakeExtractor([bad, _valid_output()])
    out = await ex.extract(Document(id="d2", text="x"))
    assert out.attempts == 2  # 第一次失败、第二次成功
    assert out.result.entities[0].type == "ORG"
    # 第二次确实把精确的字段级错误信息回喂给了模型（含出错字段与合法枚举）
    retry_msg = ex.sent_contents[1]
    assert "没有通过校验" in retry_msg
    assert "entities.0.type" in retry_msg
    assert "'ORG'" in retry_msg


@pytest.mark.asyncio
async def test_all_attempts_fail_raises():
    bad = {"entities": [{"name": "x", "type": "错误类型"}], "relations": []}
    ex = _FakeExtractor([bad, bad, bad])  # 1 + max_retries(2) = 3 次
    with pytest.raises(ExtractionError):
        await ex.extract(Document(id="d3", text="x"))
    assert ex._calls == 3


def test_verify_spans_detects_off_by_one():
    """质量探针能识别字符偏移差一位的错误。"""
    text = "公司创始人任正非到访"
    # 故意把 end 设小 1：记录 text 是"公司创始人任正非"，但切片少一个字
    result = ExtractionResult(
        entities=[
            Entity(
                name="任正非",
                type="PER",
                mentions=[],
            )
        ],
    )
    # 直接构造一条带错误 span 的关系来测探针
    from moray.schema import Relation, Span

    result.relations.append(
        Relation(
            head="任正非",
            predicate="创立",
            tail="x",
            evidence_span=Span(start=0, end=7, text="公司创始人任正非"),  # 0:7 少一字
        )
    )
    report = verify_spans(result, text)
    assert report.total == 1
    assert report.matched == 0
    assert not report.ok
    assert "与原文不符" in report.mismatches[0]["reason"]


def test_verify_spans_all_good():
    text = "华为技术有限公司"
    result = ExtractionResult(
        entities=[
            Entity(
                name="华为技术有限公司",
                type="ORG",
                mentions=[{"surface": "华为技术有限公司",
                           "span": {"start": 0, "end": 8, "text": "华为技术有限公司"}}],
            )
        ],
    )
    report = verify_spans(result, text)
    assert report.ok and report.ratio == 1.0
