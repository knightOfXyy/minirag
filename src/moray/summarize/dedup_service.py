"""敌情线索语义消歧服务（规则向量 + LLM 双保险的 moray 侧能力）。

对外提供两块内聚能力，供 talent-api 编排：
  · embed_texts：bge-small-zh 批量编码（本地 ONNX，离线、单例）；
  · judge_pair：对规则判定的"暧昧区"一对线索做 LLM 结构化裁决，映射三档。

LLM 调用收敛在 `_call_dedup_model` 缝，测试 monkeypatch 即可离线验证。
"""

from __future__ import annotations

import math

from agentscope.agent import Agent
from agentscope.message import UserMsg

from ..config import build_chat_model
from ..embedding import get_embedder
from .dedup_schema import DedupJudgeResult, DedupPair

_SYSTEM_PROMPT = (
    "你是敌情线索消歧专家。判断两条敌情线索是否指向【同一个事实/同一条敌情】，"
    "而不是仅看字面是否相同。要点：\n"
    "- 主体、行为、对象、性质一致，仅措辞/语序/详略不同 → 同一事实；\n"
    "- 行为或对象不同、时间地点不同、或一条只是另一条的局部细节 → 不同事实；\n"
    "- 信息不足以下结论时 confident 给 false，交由人工裁决，不要臆测。"
)


def embed_texts(texts: list[str]) -> list[list[float]]:
    """批量编码为 L2 归一化向量；空列表返回空列表。"""
    if not texts:
        return []
    embedder = get_embedder()
    vecs = embedder.embed(texts)
    # fastembed 已归一化，这里 tolist 前再保证是二维
    if len(texts) == 1:
        return [vecs.tolist()]
    return [v.tolist() for v in vecs]


def cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度（纯 Python，输入是否归一化都安全；零向量返回 0）。"""
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


async def _call_dedup_model(pair: DedupPair) -> DedupJudgeResult:
    """LLM 结构化裁决缝：测试 monkeypatch 这里返回假结果。"""
    model = build_chat_model(stream=False)
    agent = Agent(name="ThreatDedupJudge", system_prompt=_SYSTEM_PROMPT, model=model)
    prompt = (
        f"新线索：标题「{pair.new_title}」判定依据「{pair.new_reason}」\n"
        f"存量线索：标题「{pair.existing_title}」判定依据「{pair.existing_reason}」\n"
        f"向量余弦相似度：{pair.similarity:.2f}（仅供参考）\n"
        "请判断两者是否为同一事实。"
    )
    reply = await agent.reply(
        UserMsg(name="user", content=prompt), structured_schema=DedupJudgeResult
    )
    return DedupJudgeResult.model_validate(reply.structured_output)


async def judge_pair(
    *,
    new_title: str,
    new_reason: str,
    existing_title: str,
    existing_reason: str,
    similarity: float,
) -> dict:
    """对暧昧区一对线索做 LLM 裁决，返回 {verdict, reason}（camelCase 透传）。"""
    pair = DedupPair(
        new_title=new_title,
        new_reason=new_reason,
        existing_title=existing_title,
        existing_reason=existing_reason,
        similarity=similarity,
    )
    result = await _call_dedup_model(pair)
    if not result.confident:
        verdict = "suspect"
    else:
        verdict = "duplicate" if result.same else "fresh"
    return {"verdict": verdict, "reason": result.reason}
