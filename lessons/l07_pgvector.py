"""
================================================================================
L07 · 向量嵌入 + pgvector：给实体知识库装上"语义记忆"
================================================================================

前置（已在环境里完成，重建时照做）：
  1. 启动 PostgreSQL + pgvector：
       docker run -d --name moray-pgvector \
         -e POSTGRES_USER=moray -e POSTGRES_PASSWORD=moray_dev_2026 \
         -e POSTGRES_DB=moray_kb -p 5433:5432 pgvector/pgvector:pg16
  2. 嵌入模型走本地 bge-small-zh-v1.5（ONNX，免 torch）。
     国内下载需要两个环境变量（embedder.py 已内置默认值）：
       HF_ENDPOINT=https://hf-mirror.com   HF_HUB_DISABLE_XET=1

这节课你要建立的认知：
  1. Embedding：把文本映射成 512 维向量，语义相近的文本向量也相近。
     "华为" 与 "华为技术有限公司" 字面不同但向量距离很近——这正是解决
     L04/L05 里"华为系实体被拆成 4 个"的关键。
  2. 余弦相似度 / 余弦距离：归一化后点积即余弦相似度；
     pgvector 用 <=> 算余弦距离，distance 越小越像，similarity=1-distance。
  3. 精确匹配 + 向量召回双通道：先用规范名/别名精确命中（确定且免费），
     命不中再走向量模糊匹配——这是 L09 实体消歧的检索基础。
  4. 向量要和实体一起【持久化】在数据库里，SQL 一条 ORDER BY 就能做最近邻。

涉及代码：
  src/moray/embedding/embedder.py   本地 bge 嵌入器（单例、归一化、批量）
  src/moray/kb/schema.sql           entities/aliases/mentions 表 + ivfflat 索引
  src/moray/kb/store.py             EntityStore 写入/向量检索/别名精确匹配

运行： .venv/bin/python lessons/l07_pgvector.py
================================================================================
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from moray.embedding import cosine, get_embedder
from moray.kb import EntityStore, close_pool, init_pool

DEMO_ENTITIES = [
    # (规范名, 类型, 别名)
    ("华为技术有限公司", "ORG", ["华为", "Huawei"]),
    ("华为云计算技术有限公司", "ORG", ["华为云"]),
    ("腾讯控股有限公司", "ORG", ["腾讯"]),
    ("阿里巴巴集团控股有限公司", "ORG", ["阿里巴巴", "阿里"]),
    ("任正非", "PER", []),
    ("深圳", "LOC", []),
]


async def step_embedding() -> None:
    print("=" * 78)
    print("第 1 步：本地 bge 嵌入（离线、512 维、归一化）")
    print("=" * 78)
    emb = get_embedder()
    v = emb.embed(["华为技术有限公司", "华为", "腾讯控股有限公司", "深圳"])
    print("向量形状:", v.shape, " 模长(归一化后):", [round(float((x @ x) ** 0.5), 3) for x in v])
    print("\n语义相似度（余弦）：")
    pairs = [("华为技术有限公司", "华为"), ("华为技术有限公司", "腾讯控股有限公司"),
             ("华为技术有限公司", "深圳")]
    words = sorted({w for p in pairs for w in p})
    vecs = {t: emb.embed_one(t) for t in words}
    for a, b in pairs:
        print(f"  {a:<12} vs {b:<12} = {cosine(vecs[a], vecs[b]):.3f}")


async def step_pgvector() -> None:
    print("\n" + "=" * 78)
    print("第 2 步：实体 + 向量写入 PostgreSQL，再做语义检索")
    print("=" * 78)
    emb = get_embedder()
    pool = await init_pool()
    store = EntityStore(pool)
    await store.init_schema()
    await pool.execute("TRUNCATE entities RESTART IDENTITY CASCADE")  # 教学库重置

    for name, etype, aliases in DEMO_ENTITIES:
        await store.upsert_entity(
            name=name, type=etype, embedding=emb.embed_one(name),
            aliases=aliases, confidence=0.95,
        )
    print(f"已写入 {await store.count()} 个实体（含向量、别名）")

    async def search(query: str, etype: str | None = None, thr: float = 0.6):
        hits = await store.similar_entities(
            emb.embed_one(query), type=etype, k=3, max_distance=1 - thr
        )
        print(f"\n向量检索「{query}」(相似度阈值>{thr}):")
        for h in hits:
            print(f"  sim={h.similarity:.3f}  [{h.type:4}] {h.name}  别名{h.aliases}")
        return hits

    await search("华为公司")
    await search("阿里")
    await search("腾讯", etype="ORG")

    # 双通道：别名精确匹配
    print("\n第 3 步：精确通道（规范名/别名）——确定且不用算向量")
    for q in ("华为", "华为技术有限公司", "不存在的名字"):
        hit = await store.find_by_name_or_alias(q)
        print(f"  精确匹配「{q}」-> {hit['name'] if hit else None}")

    # 演示相似度阈值在消歧中的意义
    print("\n第 4 步：为 L09 消歧做准备——阈值怎么划？")
    hits = await store.similar_entities(emb.embed_one("华为"), k=5)
    for h in hits:
        verdict = "高可信，可自动合并" if h.similarity > 0.85 else \
                  "候选，需进一步判定" if h.similarity > 0.6 else "太不像，忽略"
        print(f"  sim={h.similarity:.3f} {h.name:<16} {verdict}")
    print("👉 L09 会把高可信自动合并、中间地带交给消歧 Agent/人工。")

    await close_pool(pool)


async def main() -> None:
    await step_embedding()
    try:
        await step_pgvector()
    except Exception as exc:  # noqa: BLE001
        print("\n⚠️ 数据库连接失败，请确认容器在运行：")
        print("   docker start moray-pgvector  或按文件头注释 docker run 启动")
        print("  原始错误：", repr(exc)[:200])

    print("\n# 动手任务：")
    print("# 1. 插入几个更难的对（如「字节跳动」vs「字节」vs「抖音集团」），")
    print("#    观察相似度，思考只靠 0.85 阈值会不会误合并。")
    print("# 2. 用 type 过滤前后对比：PER/ORG 同名（如「苹果」公司 vs 水果）。")
    print("# 3. docker exec -it moray-pgvector psql -U moray -d moray_kb")
    print("#    进入后 \\d entities 查看表结构，感受向量列与索引。")


if __name__ == "__main__":
    asyncio.run(main())
