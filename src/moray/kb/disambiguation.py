"""
实体消歧（L09 上）
==================

解决 L04/L05 暴露出的老问题：「华为 / 华为云 / 华为技术有限公司」被当成
不同实体。消歧发生在【入库之前 / 入库之后复核】，对每个新实体判断：
它到底是不是知识库里某个已有实体的另一种叫法？

决策分三档（置信度从高到低）：
    same        同一实体     -> 自动合并（迁移别名/出处/关系外键）
    different   不同实体     -> 作为新实体保留，不合并
    uncertain   拿不准       -> 进人工审核队列（HITL），等人裁决

判定器（Judge）是一个可替换策略：
    · RuleBasedJudge：纯规则 + 向量阈值，离线、确定、零花费，默认；
    · LLMJudge：让消歧 Agent 综合类型/别名/上下文裁决（需要联网，花 token）。
    规则判定器负责两端（特别像/特别不像），中间"暧昧地带"才升级给 LLM 或人，
    这样既省钱又稳妥——能确定的不打扰模型和审核员。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..embedding import Embedder, cosine, get_embedder
from .store import EntityStore, SimilarEntity

# 三档决策
SAME = "same"
DIFFERENT = "different"
UNCERTAIN = "uncertain"


@dataclass
class DisambiguationDecision:
    """对一对候选（新实体 vs 已有实体）的判定。"""

    candidate: SimilarEntity
    verdict: str            # same / different / uncertain
    similarity: float
    reason: str

    @property
    def auto_merge(self) -> bool:
        return self.verdict == SAME


class Judge(Protocol):
    """判定器接口：给定新实体信息和一个候选，返回三档决策。"""

    async def judge(
        self,
        *,
        name: str,
        type: str,
        aliases: list[str],
        candidate: SimilarEntity,
    ) -> DisambiguationDecision: ...


def _name_related(name: str, candidate: SimilarEntity) -> bool:
    """确定性的名字关联：任一方是对方子串，或命中对方别名。"""
    a, b = name.strip(), candidate.name.strip()
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    return any(a == al or a in al or al in a for al in candidate.aliases if al)


# 通用公司后缀：不是区分实体的业务词，判定时先剥掉。
# 「华为」->「华为技术有限公司」多出的只是"技术有限公司"，仍指同一公司。
_ORG_SUFFIX_MARKERS = (
    "股份有限公司", "有限责任公司", "有限公司", "集团", "控股",
    "技术", "科技", "公司",
)

# 剥掉通用后缀后，仍多出的【业务区别词】才暗示是另一个组织（母子公司/品牌）。
# 「华为云计算技术有限公司」相对「华为技术有限公司」多出"云计算/计算"——不同实体。
_DISTINCTIVE_ORG_MARKERS = (
    "云计算", "云科技", "云", "投资", "金融", "资本", "国际", "网络",
    "软件", "半导体", "汽车", "置业", "物流", "地产", "医药", "能源",
    "分公司", "子公司", "银行", "保险", "证券",
)


def _strip_org_suffix(name: str) -> str:
    out = name
    for suf in _ORG_SUFFIX_MARKERS:
        out = out.replace(suf, "")
    return out


def _looks_like_different_org(name_a: str, name_b: str) -> bool:
    """两个高度相似的组织名，剥掉通用后缀后业务核心不同 -> 疑似不同组织。

    各自剥掉"有限公司/技术/科技…"通用词得到业务核心（如 华为 / 华为云计算）：
      · 核心相同 -> 只是后缀写法差异，同一实体；
      · 长核心包含短核心，且多出部分带业务区别词（云计算/投资/金融…）-> 不同组织；
      · 核心几乎不相干 -> 不算"母子陷阱"（交给相似度/其它规则），返回 False。
    不依赖连续子串，能处理「华为云计算技术有限公司」这种插心结构。
    """
    ca, cb = _strip_org_suffix(name_a), _strip_org_suffix(name_b)
    if ca == cb:
        return False
    short, long_ = (ca, cb) if len(ca) <= len(cb) else (cb, ca)
    if short and short in long_:
        extra = long_.replace(short, "", 1)
        return any(m in extra for m in _DISTINCTIVE_ORG_MARKERS)
    # 插心结构（如 华为技术 / 华为云计算技术）：长核心去掉短核心的字后看区别词
    leftover = long_
    for ch in short:
        leftover = leftover.replace(ch, "", 1)
    return any(m in leftover for m in _DISTINCTIVE_ORG_MARKERS)


class RuleBasedJudge:
    """规则 + 向量阈值判定器（离线、可复现）。

    阈值（实测 bge-small-zh 中文实体名）：
      sim >= auto_merge_thr  且名字关联/同类型 -> same（自动合并）
      sim <= reject_thr                         -> different（明显不是）
      中间                                       -> uncertain（人工/LLM）
    类型冲突（如同名的 PER vs ORG）是强"不同"信号，压到 uncertain 以下。
    """

    def __init__(
        self,
        *,
        auto_merge_thr: float = 0.80,
        reject_thr: float = 0.62,
    ) -> None:
        self.auto_merge_thr = auto_merge_thr
        self.reject_thr = reject_thr

    async def judge(
        self, *, name: str, type: str, aliases: list[str],
        candidate: SimilarEntity,
    ) -> DisambiguationDecision:
        sim = candidate.similarity
        same_type = type == candidate.type
        exact = name == candidate.name or name in (candidate.aliases or [])

        # ① 类型冲突是最强的"不是同一实体"信号，优先于名字匹配：
        #    同名「苹果」可能是 PER/水果，也可能是 ORG（苹果公司），绝不能自动并。
        if not same_type:
            if exact or sim >= self.auto_merge_thr:
                return DisambiguationDecision(
                    candidate, UNCERTAIN, sim,
                    f"名字相同/极似但类型冲突({type}≠{candidate.type})，需人工确认")
            return DisambiguationDecision(
                candidate, DIFFERENT, sim,
                f"类型不同({type}≠{candidate.type})且相似度一般")

        # ② 同类型下精确别名命中：铁定同一实体
        if exact:
            return DisambiguationDecision(
                candidate, SAME, sim, f"规范名/别名精确匹配「{candidate.name}」")

        related = _name_related(name, candidate)

        # ③ 同类型 + 高相似 + 名字关联：还需排除"母子公司/不同组织"陷阱。
        #    只在"短简称 ↔ 长全称"且长名多出部分【不带区别性组织词】时自动并；
        #    若双方都是长名字、或长名多出"云计算/控股/子公司"等词，进人工。
        if sim >= self.auto_merge_thr and related:
            a, b = name.strip(), candidate.name.strip()
            short, long_ = (a, b) if len(a) <= len(b) else (b, a)
            # 双方都较长（>=6）且不是简单包含，谨慎处理
            if _looks_like_different_org(short, long_):
                return DisambiguationDecision(
                    candidate, UNCERTAIN, sim,
                    f"高相似但长名含区别性组织词（疑似母子公司/关联但不同实体），需人工确认")
            # 短方足够短（典型简称，如 华为/腾讯/阿里）才放心自动并
            if len(short) <= 5 or sim >= 0.95:
                return DisambiguationDecision(
                    candidate, SAME, sim,
                    f"同类型、简称↔全称、相似度 {sim:.2f}>={self.auto_merge_thr}")
            return DisambiguationDecision(
                candidate, UNCERTAIN, sim,
                f"高相似但双方均非短简称，谨慎起见转人工（sim={sim:.2f}）")

        # ④ 相似度太低：不同实体
        if sim <= self.reject_thr:
            return DisambiguationDecision(
                candidate, DIFFERENT, sim,
                f"相似度 {sim:.2f}<={self.reject_thr}，判为不同实体")

        # ⑤ 暧昧地带：交人/LLM
        tail = "且名字不构成包含关系" if not related else "需区分简称与关联实体"
        return DisambiguationDecision(
            candidate, UNCERTAIN, sim,
            f"同类型、相似度 {sim:.2f} 处于暧昧区"
            f"({self.reject_thr:.2f},{self.auto_merge_thr:.2f})，{tail}")


class LLMJudge:
    """消歧 Agent 判定器（联网、花 token）。

    让模型基于"类型/别名/上下文"对一对候选做 same/different/uncertain 裁决，
    仍要求输出受 JSON schema 约束。离线环境不要用——课程默认走 RuleBasedJudge。
    这里只做轻量封装；具体 Agent 构造复用 config.build_chat_model。
    """

    def __init__(self, model=None, *, auto_escalate: bool = True) -> None:
        self._model = model
        self.auto_escalate = auto_escalate

    def _build_model(self):
        if self._model is not None:
            return self._model
        from ..config import build_chat_model
        self._model = build_chat_model(stream=False)
        return self._model

    async def judge(
        self, *, name: str, type: str, aliases: list[str],
        candidate: SimilarEntity,
    ) -> DisambiguationDecision:
        from agentscope.agent import Agent
        from agentscope.message import UserMsg
        from pydantic import BaseModel, Field

        sim = candidate.similarity

        class Verdict(BaseModel):
            same_entity: bool = Field(description="两个指称是否指向现实世界同一对象")
            confident: bool = Field(description="是否有把握；没把握给 false")
            reason: str = Field(description="一句中文理由")

        agent = Agent(
            name="Disambiguator",
            system_prompt=(
                "你是实体消歧专家。判断两个实体指称是否指向现实世界中【同一个】"
                "对象（如简称/全称/别名关系）。要求：类型不同通常不是同一实体；"
                "母子公司、品牌与公司是不同实体，不能合并。只依据给出的信息。"
            ),
            model=self._build_model(),
        )
        prompt = (
            f"实体A：名称「{name}」类型 {type} 别名 {aliases}\n"
            f"实体B：名称「{candidate.name}」类型 {candidate.type} "
            f"别名 {candidate.aliases}\n"
            f"向量相似度：{sim:.2f}（仅供参考）\n"
            "请判断 A 与 B 是否同一实体。"
        )
        reply = await agent.reply(
            UserMsg(name="user", content=prompt), structured_schema=Verdict)
        v = Verdict.model_validate(reply.structured_output)

        if not v.confident:
            verdict = UNCERTAIN
        else:
            verdict = SAME if v.same_entity else DIFFERENT
        return DisambiguationDecision(candidate, verdict, sim, v.reason)


@dataclass
class DisambiguationReport:
    """对一个新实体的消歧结果。"""

    name: str
    action: str                       # merged / created / queued
    target_id: int | None = None      # merged->已有id；created->新id
    review_id: int | None = None
    decisions: list[DisambiguationDecision] = None

    def __post_init__(self):
        if self.decisions is None:
            self.decisions = []


class Disambiguator:
    """组合"向量召回 + 判定器 + 审核队列"的消歧服务。"""

    def __init__(
        self,
        store: EntityStore,
        embedder: Embedder | None = None,
        judge: Judge | None = None,
        *,
        candidate_k: int = 5,
        recall_distance: float = 0.5,   # 相似度 > 0.5 才召回为候选
    ) -> None:
        self.store = store
        self.embedder = embedder or get_embedder()
        self.judge = judge or RuleBasedJudge()
        self.candidate_k = candidate_k
        self.recall_distance = recall_distance

    async def candidates_for(
        self, name: str, *, domain_code: str = "general", type: str | None = None
    ) -> list[SimilarEntity]:
        """向量召回可能重复的已有实体（精确通道优先）。"""
        exact = await self.store.find_by_name_or_alias(name, domain_code=domain_code)
        if exact:
            return [SimilarEntity(
                id=exact["id"], name=exact["name"], type=exact["type"],
                domain_code=exact["domain_code"], distance=0.0, aliases=[])]
        vec = self.embedder.embed_one(name)
        return await self.store.similar_entities(
            vec, domain_code=domain_code, type=type,
            k=self.candidate_k, max_distance=self.recall_distance)

    async def resolve_new_entity(
        self, *, name: str, type: str, aliases: list[str],
        domain_code: str = "general", document_id: str | None = None,
    ) -> DisambiguationReport:
        """对一个即将入库的新实体做消歧，返回该对上应使用的【规范实体 id】。

        流程：召回候选 -> 逐候选判定 ->
          命中 same  -> 直接返回已有 id（调用方把别名/出处挂到它上面）；
          仅 uncertain -> 把最相似的一条入审核队列，并先返回新 id（暂不合并）；
          全 different/无候选 -> 返回 None 表示"确实是新实体"。
        """
        cands = await self.candidates_for(name, domain_code=domain_code, type=type)
        decisions: list[DisambiguationDecision] = []
        best_uncertain: DisambiguationDecision | None = None

        for cand in cands:
            d = await self.judge.judge(
                name=name, type=type, aliases=aliases, candidate=cand)
            decisions.append(d)
            if d.verdict == SAME:
                return DisambiguationReport(
                    name=name, action="merged", target_id=cand.id, decisions=decisions)
            if d.verdict == UNCERTAIN and (
                best_uncertain is None or d.similarity > best_uncertain.similarity
            ):
                best_uncertain = d

        if best_uncertain is not None:
            # 只返回"建议与谁合并"；真正建实体+入队由 Ingestor 带着新 id 完成，
            # 保持本方法无写入副作用。best uncertain 决策挂在 decisions 里。
            return DisambiguationReport(
                name=name, action="queued",
                target_id=best_uncertain.candidate.id, decisions=decisions)

        return DisambiguationReport(name=name, action="created", decisions=decisions)

    async def enqueue_for_review(
        self, *, survivor_id: int, duplicate_id: int,
        survivor_name: str, duplicate_name: str,
        similarity: float, reason: str,
        domain_code: str = "general", document_id: str | None = None,
    ) -> int:
        return await self.store.enqueue_review(
            survivor_id=survivor_id, duplicate_id=duplicate_id,
            survivor_name=survivor_name, duplicate_name=duplicate_name,
            similarity=similarity, reason=reason,
            domain_code=domain_code, document_id=document_id,
        )
