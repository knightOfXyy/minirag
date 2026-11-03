"""分块级抽取度量（工单 01）
============================

本模块只负责【度量与信号】，不参与任何抽取结果或切块决策。

为什么单独建一层（而不是把字段塞进 ExtractionOutput.usage）：
  * Usage 是"累计 token"语义，跨尝试、跨块相加后丢失了"哪一次调用被截断、
    哪一块返了工"的信息——而这正是后续调大块预算（工单 04）唯一要盯的信号；
  * 看板/作业结果/API 三条消费方要的是同一份规整记录，先在内存里聚合成
    DocumentMetrics 再外发，避免每处各算一遍返工率/截断计数。

字段语义（务必与 tests/test_chunk_metrics.py 对齐）：
  * wait_s     该块在并发信号量上排队等待的时间（从 run 开始调度到真正发出）；
  * prefill_s  首 token 耗时（请求发出 -> 第一个输出 token）；
  * decode_s   解码耗时（首 token -> 调用结束）。
非流式网关一次性返回、拿不到首 token 时间点时，prefill_s/decode_s 如实记
``None``，**绝不用总延迟冒充**——下游聚合会跳过 None，而不是拿 0 拉低均值。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 输出撞上长度上限而被截断的结束原因。
# OpenAI 兼容网关（火山方舟/DeepSeek）统一返回 "length"；保留常见别名兜底，
# 不同供应商的字面量差异都在这里收敛，识别逻辑只此一处。
LENGTH_FINISH_REASONS = frozenset({"length", "max_tokens", "max_output_tokens"})
FINISH_LENGTH = "length"
# 模型/中间件没有回传结束原因时的显式占位（不允许静默当成 stop）。
FINISH_UNKNOWN = "unknown"


def is_length_finish_reason(reason: str | None) -> bool:
    """结束原因是否表示"输出因长度上限被截断"。

    截断的 JSON 会在后续 schema 校验处伪装成普通校验失败、触发返工；
    不在这里认出来，看板只会看到"返工率高"，永远查不到真正的静默杀手。
    """
    if not reason:
        return False
    return str(reason).strip().lower() in LENGTH_FINISH_REASONS


@dataclass
class ChunkCallMetrics:
    """一个分块一次完整抽取（可能含多次校验返工尝试）的度量记录。

    核心字段（finish_reason/attempts/token）无默认值：调用方必须显式提供。
    拿不到结束原因时传 FINISH_UNKNOWN，而不是省略——防止"缺字段被静默当成功"。
    """

    chunk_id: str
    attempts: int
    finish_reason: str
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    wait_s: float
    prefill_s: float | None
    decode_s: float | None
    # 该块各次尝试的结束原因（attempts 条）；末位即 finish_reason
    attempt_finish_reasons: list[str] = field(default_factory=list)
    # 该块是否出现过任何一次截断（可能截断后又返工成功，仍要计数暴露）
    truncated_attempts: int = 0
    error: str = ""

    @property
    def truncated(self) -> bool:
        """该块最终一次返回是否因长度被截断。"""
        return is_length_finish_reason(self.finish_reason)

    @property
    def reworked(self) -> bool:
        """该块是否发生过校验返工（尝试次数 > 1）。"""
        return self.attempts > 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "attempts": self.attempts,
            "finish_reason": self.finish_reason,
            "truncated": self.truncated,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "wait_s": round(self.wait_s, 6),
            "prefill_s": self.prefill_s,
            "decode_s": self.decode_s,
            "reworked": self.reworked,
            "truncated_attempts": self.truncated_attempts,
            "attempt_finish_reasons": list(self.attempt_finish_reasons),
            "error": self.error,
        }


def _sum(values: list[float | None]) -> float | None:
    """只累加真实测到的分段耗时；全部 None（测不到）时保持 None。

    不能用 0 代替：0 会被下游当成"几乎不耗时的实测值"拉低均值/P50/P99。
    """
    known = [v for v in values if v is not None]
    return round(sum(known), 6) if known else None


def merge_call_metrics(records: list[ChunkCallMetrics]) -> ChunkCallMetrics:
    """把同一块的多轮抽取调用合并成一条块级记录。

    分块路径里一块通常只调一次 Extractor；短文档的旧闭环会因审校返工
    再调一次，但它们仍然是"同一块文本"。合并后 chunk_calls 仍等于块数，
    而 attempts/两次 token/截断信号都不会丢。
    """
    if not records:
        raise ValueError("records 不能为空")
    if len(records) == 1:
        return records[0]
    first, last = records[0], records[-1]
    return ChunkCallMetrics(
        chunk_id=first.chunk_id,
        attempts=sum(r.attempts for r in records),
        finish_reason=last.finish_reason,
        input_tokens=sum(r.input_tokens for r in records),
        output_tokens=sum(r.output_tokens for r in records),
        cached_input_tokens=sum(r.cached_input_tokens for r in records),
        wait_s=sum(r.wait_s for r in records),
        # 任一轮测到就累加；全测不到才为 None
        prefill_s=(round(sum(r.prefill_s for r in records
                             if r.prefill_s is not None), 6)
                   if any(r.prefill_s is not None for r in records) else None),
        decode_s=(round(sum(r.decode_s for r in records
                            if r.decode_s is not None), 6)
                  if any(r.decode_s is not None for r in records) else None),
        attempt_finish_reasons=[
            reason for r in records for reason in r.attempt_finish_reasons],
        truncated_attempts=sum(r.truncated_attempts for r in records),
        error=(last.error or ""),
    )


@dataclass
class DocumentMetrics:
    """一篇文档（= 一次分块流水线 run）的分块度量集合与文档级聚合。"""

    document_id: str
    n_chunks: int = 0
    chunks: list[ChunkCallMetrics] = field(default_factory=list)
    # 提示缓存预热探测也会真实烧 token/时间，单独记账不混进任何一块
    warmup: dict[str, Any] | None = None

    def add(self, record: ChunkCallMetrics) -> None:
        self.chunks.append(record)

    @property
    def truncated_chunks(self) -> int:
        """最终结果被截断的块数（看板截断护栏的主计数）。"""
        return sum(1 for c in self.chunks if c.truncated)

    @property
    def truncation_signals(self) -> int:
        """出现过截断信号的块数（含返工后救回的块）。

        与 truncated_chunks 的区别：一块可能先 length、返工后 stop——
        最终没截断，但"预算撞过顶"这件事必须被看见。按块计数，不是按尝试次数。
        """
        return sum(
            1 for c in self.chunks
            if c.truncated_attempts
            or any(is_length_finish_reason(r) for r in c.attempt_finish_reasons)
        )

    @property
    def reworked_chunks(self) -> int:
        return sum(1 for c in self.chunks if c.reworked)

    def summary(self) -> dict[str, Any]:
        """文档级摘要：进作业结果/看板数据面的那一行关键数字。"""
        n = len(self.chunks)
        total_attempts = sum(c.attempts for c in self.chunks)
        return {
            "document_id": self.document_id,
            "n_chunks": self.n_chunks or n,
            # 分块 LLM 调用块数（每块对应一次完整抽取）
            "chunk_calls": n,
            # 含校验返工的实际模型尝试总次数
            "total_attempts": total_attempts,
            "truncated_chunks": self.truncated_chunks,
            "truncation_signals": self.truncation_signals,
            # 返工率 = 发生过返工的块 / 块数
            "rework_rate": round(self.reworked_chunks / n, 6) if n else 0.0,
            "reworked_chunks": self.reworked_chunks,
            "wait_s": _sum([c.wait_s for c in self.chunks]),
            "prefill_s": _sum([c.prefill_s for c in self.chunks]),
            "decode_s": _sum([c.decode_s for c in self.chunks]),
            "input_tokens": sum(c.input_tokens for c in self.chunks),
            "output_tokens": sum(c.output_tokens for c in self.chunks),
            "cached_input_tokens": sum(
                c.cached_input_tokens for c in self.chunks),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "n_chunks": self.n_chunks or len(self.chunks),
            "summary": self.summary(),
            "chunks": [c.to_dict() for c in self.chunks],
            "warmup": self.warmup,
        }
