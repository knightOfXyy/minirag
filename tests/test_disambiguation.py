"""RuleBasedJudge 纯逻辑单测（不加载模型、不需要数据库）。

直接构造候选 SimilarEntity + 指定相似度，验证三档判定与"母子公司"防护。
"""

import pytest

from moray.kb.disambiguation import (
    DIFFERENT,
    SAME,
    UNCERTAIN,
    RuleBasedJudge,
    _looks_like_different_org,
)
from moray.kb.store import SimilarEntity


def _cand(name="华为技术有限公司", type="ORG", sim=0.9, aliases=None):
    return SimilarEntity(
        id=1, name=name, type=type, domain_code="general",
        distance=1.0 - sim, aliases=aliases or [],
    )


@pytest.mark.asyncio
async def test_exact_alias_is_same():
    j = RuleBasedJudge()
    d = await j.judge(name="华为", type="ORG", aliases=[],
                      candidate=_cand(sim=1.0, aliases=["华为"]))
    assert d.verdict == SAME


@pytest.mark.asyncio
async def test_short_alias_long_fullname_auto_merges():
    # 「华为」(短简称) vs「华为技术有限公司」，剥掉通用后缀后无业务区别词
    j = RuleBasedJudge(auto_merge_thr=0.80)
    d = await j.judge(name="华为", type="ORG", aliases=[],
                      candidate=_cand(sim=0.833))
    assert d.verdict == SAME


@pytest.mark.asyncio
async def test_subsidiary_cloud_not_auto_merged():
    # 华为云计算技术有限公司 vs 华为技术有限公司：多出"云计算"-> 不自动并
    assert _looks_like_different_org(
        "华为技术有限公司", "华为云计算技术有限公司") is True
    j = RuleBasedJudge(auto_merge_thr=0.80)
    d = await j.judge(name="华为云计算技术有限公司", type="ORG", aliases=["华为云"],
                      candidate=_cand(sim=0.881))
    assert d.verdict == UNCERTAIN


@pytest.mark.asyncio
async def test_type_conflict_priority():
    # 同名但类型冲突（PER vs ORG），即便高相似也不能自动合并
    j = RuleBasedJudge()
    d = await j.judge(name="苹果", type="PER", aliases=[],
                      candidate=_cand(name="苹果", type="ORG", sim=0.95))
    assert d.verdict == UNCERTAIN


@pytest.mark.asyncio
async def test_low_similarity_is_different():
    j = RuleBasedJudge()
    d = await j.judge(name="腾讯控股有限公司", type="ORG", aliases=["腾讯"],
                      candidate=_cand(sim=0.58))
    assert d.verdict == DIFFERENT


@pytest.mark.asyncio
async def test_type_conflict_low_sim_different():
    j = RuleBasedJudge()
    d = await j.judge(name="深圳", type="LOC", aliases=[],
                      candidate=_cand(sim=0.45))
    assert d.verdict == DIFFERENT


def test_org_suffix_stripping_logic():
    # 通用后缀不应判为不同组织
    assert _looks_like_different_org("华为", "华为技术有限公司") is False
    # 真正的业务词才判不同
    assert _looks_like_different_org("腾讯", "腾讯投资有限公司") is True
