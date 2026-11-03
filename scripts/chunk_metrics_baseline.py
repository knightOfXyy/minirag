"""工单 01 验收：用现有真实样本跑一遍分块度量，产出可复跑的基线数字。

用法（需要真实 LLM 凭证，见 .env）：
    python scripts/chunk_metrics_baseline.py
    python scripts/chunk_metrics_baseline.py --sample data/samples/news_long_002.txt
    MORAY_CHUNK_MAX_UNITS=1500 python scripts/chunk_metrics_baseline.py --repeat 3

输出：
  · 每块一条：输入/输出 token、attempts、结束原因、prefill/decode 耗时、截断；
  · 文档级摘要：调用数、返工率、截断块数、总 prefill/decode、输出 token 均值；
  · JSON 落到 --out（默认 data/gold/chunk_metrics_baseline.json），复跑可 diff。

注意：真实网络数字天然有抖动；--repeat N 取中位数/均值并同时落每次原始值，
对比时看均值与截断计数，不看单次绝对值。本脚本只读样本、写一个 JSON，
不改任何抽取结果/切块默认值。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from moray.config import chunk_settings, get_settings
from moray.extraction import (
    ChunkedExtractionPipeline,
    Chunker,
    Document,
)


async def run_once(text: str, sample_name: str, *, stream: bool):
    # 经动态配置中心同样的构造路径，拿到挂了埋点子类的真实 Extractor。
    # 生产默认 stream=False（与本单"不改抽取行为"一致）：此时网关一次性返回，
    # 首 token 时间点在协议里不存在，prefill/decode 如实记 None。
    # --stream 用流式请求实测 prefill/decode，作为"分段耗时有多大"的参考基线；
    # 是否把生产切到流式留给后续工单决策（请求形态变化，需黄金集回归）。
    from moray.extraction.extractor import Extractor

    extractor = Extractor("general", max_retries=1)
    if stream:
        # 走流式构造：仅为基准测量，把 _build_agent 换成流式模型（方法须是同步的，
        # agent.reply 自己是 async）；不改变提示词/校验/切块任何一环。
        from agentscope.agent import Agent

        from moray.config import build_chat_model

        def _build_agent_stream():
            return Agent(
                name="Extractor",
                system_prompt=extractor._system_prompt,
                model=build_chat_model(stream=True),
                middlewares=extractor._middlewares)

        extractor._build_agent = _build_agent_stream

    cs = chunk_settings()
    chunker = Chunker(max_units=cs["max_units"], overlap_units=cs["overlap_units"])
    chunks = chunker.chunk_text(text)
    pipeline = ChunkedExtractionPipeline(
        extractor, concurrency=cs["concurrency"])
    t0 = time.perf_counter()
    output = await pipeline.run(
        Document(id=f"baseline-{sample_name}", text=text, source="baseline"),
        chunks, raise_on_chunk_error=False)
    wall_s = time.perf_counter() - t0
    dm = output.document_metrics
    assert dm is not None
    return {
        "sample": sample_name,
        "mode": "stream" if stream else "non_stream",
        "n_chunks": len(chunks),
        "wall_s": round(wall_s, 4),
        "chunk_settings": cs,
        "summary": dm.summary(),
        "chunks": [c.to_dict() for c in dm.chunks],
        "output_tokens_mean": (
            round(statistics.mean(
                [c.output_tokens for c in dm.chunks]), 2)
            if dm.chunks else 0),
    }


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True,
            stderr=subprocess.DEVNULL).strip()
    except Exception:  # noqa: BLE001 非 git 环境也要能跑
        return "unknown"


def _baseline_meta() -> dict:
    """基线可审计所需的来源信息：换模型/换 prompt 后才能判断是否可比。"""
    s = get_settings()
    return {
        "provider": s.get("provider"),
        "chat_model": s.get("chat_model"),
        "generated_at": datetime.now(UTC).isoformat(),
        "git_sha": _git_sha(),
    }


def _print_report(rep: dict) -> None:
    s = rep["summary"]
    print(f"\n样本 {rep['sample']}：{rep['n_chunks']} 块，"
          f"墙钟 {rep['wall_s']}s（concurrency={rep['chunk_settings']['concurrency']}）")
    print("-" * 88)
    print(f"{'chunk':<14}{'in':>6}{'out':>7}{'cached':>8}"
          f"{'att':>4}{'finish':>10}{'prefill_s':>11}{'decode_s':>10}")
    for c in rep["chunks"]:
        pre = "None" if c["prefill_s"] is None else f"{c['prefill_s']:.3f}"
        dec = "None" if c["decode_s"] is None else f"{c['decode_s']:.3f}"
        flag = " TRUNC" if c["truncated"] else ""
        print(f"{c['chunk_id'][-12:]:<14}{c['input_tokens']:>6}"
              f"{c['output_tokens']:>7}{c['cached_input_tokens']:>8}"
              f"{c['attempts']:>4}{c['finish_reason']:>10}"
              f"{pre:>11}{dec:>10}{flag}")
    print("-" * 88)
    print(f"调用数={s['chunk_calls']} 总尝试={s['total_attempts']} "
          f"返工率={s['rework_rate']} 截断块={s['truncated_chunks']} "
          f"(信号 {s['truncation_signals']})")
    print(f"总 prefill={s['prefill_s']}s 总 decode={s['decode_s']}s "
          f"总等待={s['wait_s']}s")
    print(f"输入 token={s['input_tokens']} 输出 token={s['output_tokens']} "
          f"输出均值/块={rep['output_tokens_mean']}")


async def amain() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", default="data/samples/news_long_002.txt")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--out", default="data/gold/chunk_metrics_baseline.json")
    ap.add_argument("--repeat-tokens", type=int, default=12,
                    help="把样本正文重复拼接的次数，制造稳定的多块文档")
    ap.add_argument("--stream", action="store_true",
                    help="用流式请求实测 prefill/decode（默认非流式，分段为 None）")
    args = ap.parse_args()

    base_text = Path(args.sample).read_text(encoding="utf-8").strip()
    text = "\n".join(base_text for _ in range(args.repeat_tokens))

    runs = []
    for i in range(args.repeat):
        print(f"--- run {i + 1}/{args.repeat} ({'stream' if args.stream else 'non-stream'}) ---")
        rep = await run_once(text, Path(args.sample).name, stream=args.stream)
        runs.append(rep)
        _print_report(rep)

    out = {"meta": _baseline_meta(), "runs": runs}
    if args.repeat > 1:
        def _agg(field):
            vals = [r["summary"][field] for r in runs]
            return {"mean": round(statistics.mean(vals), 4),
                    "stdev": round(statistics.pstdev(vals), 4),
                    "values": vals}
        out["aggregate"] = {
            f: _agg(f) for f in
            ("prefill_s", "decode_s", "wait_s",
             "input_tokens", "output_tokens", "truncated_chunks")}
    Path(args.out).write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n基线已写入 {args.out}")


if __name__ == "__main__":
    asyncio.run(amain())
