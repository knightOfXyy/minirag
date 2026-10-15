"""
================================================================================
L05 · 大文件怎么办：分块 map-reduce 抽取 + 偏移换算 + 跨块归并
================================================================================

样本是我们自己的干净中文新闻 data/samples/news_long_002.txt（多段落，
同一实体反复出现），适合演示分块与跨块归并，不依赖任何外部文件。

为什么大文件不能直接喂模型（L04 评估过的四堵墙）：
  1) 超上下文 / 请求超时；2) 贵且慢；3) 长文漏抽 + JSON 输出截断；
  4) 分块后 span 是"块内偏移"、同实体在多块重复 —— 必须换算与归并。

本课的 map-reduce：
  原文
   │  Chunker（token 预算 + 句子边界 + overlap，每块记住全局字符偏移）
   ▼
  chunk0 chunk1 chunk2
   │  Map：每块独立 Extractor.extract（限流并发，单块失败互不影响）
   ▼
  remap_spans：块内 span + chunk.start = 文档全局 span（溯源才准）
   ▼
  naive_merge：按(类型,标准名)聚合实体、并集别名与 mention、关系去重
   ▼
  ExtractionResult（跨块的"简称=全称"高级共指消解留到 L09）

两种运行方式：
  .venv/bin/python lessons/l05_chunking.py            # 含真实 LLM 抽取（3 小块）
  OFFLINE=1 .venv/bin/python lessons/l05_chunking.py  # 只跑离线部分，不花 token

生产代码：
  src/moray/extraction/chunking.py      Chunker / estimate_tokens
  src/moray/extraction/pipeline.py      remap_spans / naive_merge / 流水线
  src/moray/extraction/rule_extractor.py 规则法（解析已标注语料，本课末尾演示）
================================================================================
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from moray.extraction import (
    Chunk,
    Chunker,
    ChunkedExtractionPipeline,
    Document,
    Extractor,
    clean_tagged_text,
    estimate_tokens,
    extract_from_tagged_text,
    naive_merge,
    remap_spans,
    verify_spans,
)
from moray.schema import Entity, EntityMention, ExtractionResult, Span

SAMPLE = ROOT / "data" / "samples" / "news_long_002.txt"
OFFLINE = os.environ.get("OFFLINE") == "1"
BUDGET, OVERLAP = 150, 30     # 教学用小块：样本约切成 3 块


# -----------------------------------------------------------------------------
# 第 1 步：分块（纯本地）
# -----------------------------------------------------------------------------
def step_chunking(text: str) -> list[Chunk]:
    print("=" * 78)
    print("第 1 步：分块（token 预算 + 句子边界 + overlap）")
    print("=" * 78)
    print(f"原文 {len(text)} 字符，约 {estimate_tokens(text)} units")

    chunker = Chunker(max_units=BUDGET, overlap_units=OVERLAP)
    t0 = time.time()
    chunks = chunker.chunk_text(text, part_id="news_long_002", id_prefix="n2")
    print(f"切成 {len(chunks)} 块，耗时 {time.time()-t0:.4f}s\n")

    for c in chunks:
        head = c.text.strip().replace("\n", " ")[:34]
        print(f"  {c.id}  全局[{c.start}:{c.end}]  units={c.n_units}")
        print(f"        首句: {head}…")

    # 不变量：每块都是原文的精确切片（这是偏移换算可信的根基）
    assert all(text[c.start:c.end] == c.text for c in chunks), "块偏移不自洽！"
    # 首块含开头、末块含结尾
    assert chunks[0].start == 0 and chunks[-1].end >= len(text.rstrip()) - 1
    print("\n✅ 偏移不变量通过：text[start:end] == chunk.text；首尾覆盖完整。")
    return chunks


# -----------------------------------------------------------------------------
# 第 2 步：偏移换算（用一个手工构造的块结果讲清楚原理，离线可跑）
# -----------------------------------------------------------------------------
def step_remap(text: str, chunks: list[Chunk]) -> None:
    print("\n" + "=" * 78)
    print("第 2 步：块内 span -> 全局 span（remap_spans 的原理）")
    print("=" * 78)

    # 在第 2 个块里手工定位"任正非"，模拟模型在【块内】从 0 数出的偏移
    chunk = next(c for c in chunks if "任正非" in c.text)
    local = chunk.text.index("任正非")
    local_span = Span(start=local, end=local + 3, text="任正非")

    fake = ExtractionResult(entities=[
        Entity(name="任正非", type="PER",
               mentions=[EntityMention(surface="任正非", span=local_span)])
    ])
    remapped = remap_spans(fake, chunk)
    g = remapped.entities[0].mentions[0].span

    print(f"块 {chunk.id} 全局起点 chunk.start = {chunk.start}")
    print(f"模型给的块内偏移 [{local}:{local+3}]")
    print(f"换算后全局偏移 [{g.start}:{g.end}]  -> 回切原文 = 「{text[g.start:g.end]}」")
    assert text[g.start:g.end] == "任正非"
    print("✅ 公式：全局 = 块内 + chunk.start。越界的块内 span 会被置空而不是写错。")


# -----------------------------------------------------------------------------
# 第 3 步：朴素归并（离线构造两个块的部分结果，演示去重与 mention 并集）
# -----------------------------------------------------------------------------
def step_merge(text: str, chunks: list[Chunk]) -> None:
    print("\n" + "=" * 78)
    print("第 3 步：跨块归并 naive_merge（同实体聚合 / 别名并集 / 关系去重）")
    print("=" * 78)

    # 模拟块0抽到"华为技术有限公司(别名 华为)"，块2抽到同一个标准名
    p1 = ExtractionResult(entities=[
        Entity(name="华为技术有限公司", type="ORG", aliases=["华为"],
               mentions=[EntityMention(surface="华为技术有限公司")], confidence=0.9)
    ], relations=[])
    p2 = ExtractionResult(entities=[
        Entity(name="华为技术有限公司", type="ORG",
               mentions=[EntityMention(surface="华为")], confidence=0.95),
        Entity(name="贵阳市政府", type="ORG",
               mentions=[EntityMention(surface="当地")], confidence=0.6),
    ], relations=[])

    merged = naive_merge([p1, p2])
    hw = next(e for e in merged.entities if e.name == "华为技术有限公司")
    print(f"两块各自出现的同名实体被合并为 1 个，置信度取最大值 {hw.confidence}")
    print(f"  别名并集: {hw.aliases}")
    print(f"  mention 数: {len(hw.mentions)}（出现次数被完整保留，供溯源/统计）")
    print("⚠️ '华为' 与 '华为技术有限公司' 字面不同，本步还不会合并；")
    print("   这种简称=全称的【共指消解】需要向量相似度/LLM，是 L09 的内容。")


# -----------------------------------------------------------------------------
# 第 4 步：真实 LLM 分块抽取（map）-> 换算 -> 归并 -> 质检
# -----------------------------------------------------------------------------
async def step_llm_pipeline(text: str, chunks: list[Chunk]) -> None:
    print("\n" + "=" * 78)
    print("第 4 步：真实 LLM 分块 map-reduce（可用 OFFLINE=1 跳过）")
    print("=" * 78)
    if OFFLINE:
        print("OFFLINE=1，已跳过 LLM 抽取。去掉该环境变量即可跑真实模型。")
        return

    doc = Document(id="news_long_002", text=text, source="news", domain_code="general")
    extractor = Extractor("general", max_retries=1)
    pipeline = ChunkedExtractionPipeline(extractor, concurrency=3)

    done = {"n": 0}
    def on_chunk(_res, cid, total):
        done["n"] += 1
        print(f"  · 块 {cid} 完成（{done['n']}/{total}）")

    t0 = time.time()
    output = await pipeline.run(doc, chunks, on_chunk=on_chunk)
    dt = time.time() - t0

    print(f"\n归并后：实体 {len(output.result.entities)} 个，"
          f"关系 {len(output.result.relations)} 条")
    print(f"token：输入 {output.usage.input_tokens} / 输出 {output.usage.output_tokens}，"
          f"耗时 {dt:.1f}s，块数 {output.attempts}")

    print("\n【按类型看实体】")
    for t in ("PER", "ORG", "LOC", "EVENT", "MISC"):
        names = sorted(e.name for e in output.result.entities if e.type == t)
        if names:
            print(f"  {t:5} ({len(names)}): {', '.join(names)}")

    print("\n【关系（前 12 条）】")
    for r in output.result.relations[:12]:
        print(f"  ({r.head}) —[{r.predicate}]→ ({r.tail})")

    # 全局偏移质检：干净文本上，span 应能精确切回原文
    report = verify_spans(output.result, text)
    print(f"\n【出处质量】可核对 {report.total}，准确 {report.matched}，"
          f"一致率 {report.ratio:.0%}")
    for bad in report.mismatches[:5]:
        print("   ·", bad["where"], bad["reason"][:70])


# -----------------------------------------------------------------------------
# 附：规则法解析"已标注语料"（内联一小段，演示它与 LLM 路径的分工）
# -----------------------------------------------------------------------------
def step_rule_extractor() -> None:
    print("\n" + "=" * 78)
    print("附：规则法（输入自带词性标签时，免费且 100% 确定）")
    print("=" * 78)
    tagged = (
        "19980101-01-001-002/m  华为/nt  公司/n  总部/n  位于/v  深圳/ns  。/w\n"
        "19980101-01-001-003/m  任正非/nr  考察/v  贵阳/ns  。/w\n"
    )
    print("带标签输入（样例）：", tagged.strip().splitlines()[0])
    print("清洗成干净原文：", clean_tagged_text(tagged).splitlines()[0])

    res = extract_from_tagged_text(tagged)
    for e in res.entities:
        print(f"  [{e.type:5}] {e.name}  mention×{len(e.mentions)}  conf={e.confidence}")
    print("👉 已标注语料走规则法（可当评测金标准）；干净原文才走 LLM。")


async def main() -> None:
    text = SAMPLE.read_text(encoding="utf-8").strip()
    chunks = step_chunking(text)
    step_remap(text, chunks)
    step_merge(text, chunks)
    await step_llm_pipeline(text, chunks)
    step_rule_extractor()

    print("\n# 动手任务：")
    print("# 1. 先 OFFLINE=1 跑，理解分块/换算/归并不依赖模型。")
    print("# 2. 把 BUDGET 调小到 90，观察块数变多、overlap 重复实体增加。")
    print("# 3. 在归并结果里找'华为'与'华为技术有限公司'被当成两个实体的例子，")
    print("#    带着这个问题进入 L07/L09（向量化 + 共指消解）。")


if __name__ == "__main__":
    asyncio.run(main())
