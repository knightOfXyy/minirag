"""
================================================================================
L04（下）· 抽取服务 v1：真实文档端到端 + 校验重试 + 出处质检 + 结果落盘
================================================================================

这节课把前面散着的积木，组装成一个"企业里真的会这样写"的服务：

    Document ──▶ Extractor.extract() ──▶ ExtractionOutput ──▶ Sink 落盘
                    │  内部：动态领域 schema
                    │       结构化输出
                    │       校验失败自动带错重试
                    ▼
              verify_spans() 出处偏移自动质检

你要建立的认知：
  1. 服务对象 Extractor 按"领域"构造，可反复用于多篇文档；每次抽取新建
     Agent，避免跨文档记忆污染（企业批处理的基本卫生）。
  2. 抽取不是"调一次模型"就结束：要 校验 -> 失败带错重试 -> 转稳定内核。
  3. 出处 span 可以被程序 100% 自动核对，作为零成本质量探针。
  4. 落盘走 Sink 接口：现在写 JSON，L08 换成 PostgresSink 时上层零改动。
  5. token 用量被累计记录，这是成本核算/模型选型的数据基础。

运行： .venv/bin/python lessons/l04b_extraction_service.py
================================================================================
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from moray.extraction import (
    Document,
    Extractor,
    JsonFileSink,
    verify_spans,
)


def load_document(path: Path) -> Document:
    """从文件构造 Document；文档 id 用文件名，正文用于 span 定位。"""
    return Document(
        id=path.stem,                 # news_001
        text=path.read_text(encoding="utf-8").strip(),
        title=path.stem,
        source="news",
        domain_code="general",
    )


async def main() -> None:
    data_path = ROOT / "data" / "samples" / "news_001.txt"
    out_dir = ROOT / "data" / "outputs"
    doc = load_document(data_path)

    print("=" * 76)
    print(f"文档 {doc.id}（领域={doc.domain_code}），正文长度 {len(doc.text)} 字")
    print("=" * 76)
    print(doc.text)

    # ① 构造一个"通用领域"抽取服务（生产中它通常是单例、被多处复用）
    extractor = Extractor("general", max_retries=2)

    # ② 执行抽取（内部完成 结构化输出 + 校验重试 + 转稳定内核）
    output = await extractor.extract(doc)

    # ③ 出处质量探针：自动核对 span 是否切得准
    report = verify_spans(output.result, doc.text)

    # ④ 展示结果
    print("\n" + "-" * 76)
    print(f"✅ 抽取完成：实体 {len(output.result.entities)} 个，"
          f"关系 {len(output.result.relations)} 条")
    print(f"   模型={output.model_name}  尝试次数={output.attempts}")
    print(f"   token 用量：输入 {output.usage.input_tokens} / "
          f"输出 {output.usage.output_tokens}")
    print("-" * 76)

    print("\n【实体】（标准名 + 别名 + 原文提及）")
    for e in output.result.entities:
        aliases = f"  别名={e.aliases}" if e.aliases else ""
        print(f"  [{e.type:5}] {e.name}{aliases}  (conf={e.confidence:.2f})")
        for m in e.mentions:
            span = f"[{m.span.start}:{m.span.end}]" if m.span else "[无span]"
            print(f"          └ {span} 「{m.surface}」")

    print("\n【关系】")
    for r in output.result.relations:
        print(f"  ({r.head}) —[{r.predicate}]→ ({r.tail})  (conf={r.confidence:.2f})")

    print("\n【出处质量】")
    print(f"  可核对提及 {report.total} 个，准确 {report.matched} 个，"
          f"一致率 {report.ratio:.0%}  {'✅' if report.ok else '⚠️ 有偏差'}")
    for bad in report.mismatches:
        print(f"    · {bad['where']}: {bad['reason']}")

    # ⑤ 落盘 + 读回，证明产物可持久化、可重新加载为强类型对象
    sink = JsonFileSink(out_dir)
    saved_path = sink.save(output)
    reloaded = sink.load(doc.id)
    print("\n【持久化】")
    print(f"  已写入：{saved_path.relative_to(ROOT)}")
    print(f"  读回校验：{len(reloaded.result.entities)} 个实体，"
          f"领域={reloaded.domain_code}，强类型还原成功 ✅")

    print("\n# 动手任务：")
    print("# 1. 打开 data/outputs/news_001.json，对照正文核对每个 span。")
    print("# 2. 换 Extractor('finance') 抽同一篇新闻，观察类型如何变化")
    print("#    （新闻里没有合同，体会领域选错时的结果）。")
    print("# 3. 自己往 data/samples 放一篇文章，改 load_document 路径再跑。")


if __name__ == "__main__":
    asyncio.run(main())
