"""
================================================================================
L14 · 服务化：把抽取知识库包成 HTTP API（FastAPI）
================================================================================

前 13 课的能力都在 Python 进程内；本课用 FastAPI 暴露给外部系统/前端：

  POST /extract                 提交文本 -> L10 抽取审校 ->（可选）入库
  POST /jobs、GET /jobs/{id}     登记/查询批量作业（接 L11 的 worker 处理）
  GET  /entities/locate         自然语言/简称定位实体
  GET  /entities/similar        向量近邻
  GET  /entities/{id}/card      实体知识卡片
  GET  /relations               结构化出/入边查询
  GET  /reviews                 待人工裁决的合并（L09 HITL）
  POST /reviews/{id}/decision   批准/拒绝
  GET  /metrics/dashboard       L13 质量看板
  GET  /healthz                 健康检查

关键工程点：
1) 依赖注入 create_app(deps=|deps_factory=)：
   · 测试：传现成 deps，用 httpx ASGITransport 不起端口调用；
   · 生产：传【异步工厂】，在 FastAPI lifespan（uvicorn 的事件循环）里建
     asyncpg 连接池。绝不能在 asyncio.run() 里建好池再交给 uvicorn——
     连接池绑定创建它的事件循环，换循环会报 "another operation is in progress"。
2) 实时 LLM 抽取默认关闭（POST /extract 返回 501），用 --llm 开启，
   避免查询服务被慢模型拖垮；批量走 /jobs + 独立 worker（L11）。

本脚本默认用 httpx 进程内调用（不起端口、自动注入确定性假抽取器）。
想体验真实 HTTP：
  python -m moray.api.server --port 8000      # 另一终端起服务
  curl http://127.0.0.1:8000/healthz
  python -m moray.api.server --port 8000 --llm  # 开启实时抽取
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

HTTP = "--http" in sys.argv


async def inproc_demo() -> None:
    import numpy as np
    from httpx import ASGITransport, AsyncClient

    from moray.api.app import AppDeps, create_app
    from moray.jobs import JobStore
    from moray.kb import EntityStore, Ingestor, KnowledgeBase, close_pool, init_pool
    from moray.observability import Dashboard, RunRecorder
    from moray.schema import Entity, EntityMention, ExtractionResult, Relation, Span

    TEXT = "腾讯控股有限公司总部位于深圳，马化腾创立了腾讯。"

    class FakeEmbedder:  # 进程内演示不加载 ONNX
        dim = 512

        def __init__(self):
            self._c = {}

        def embed(self, texts):
            single = isinstance(texts, str)
            items = [texts] if single else texts
            out = np.array([self._one(t) for t in items], dtype=np.float32)
            return out[0] if single else out

        def embed_one(self, t):
            return self._one(t).tolist()

        def _one(self, t):
            if t not in self._c:
                v = np.random.default_rng(abs(hash(t)) % 2 ** 32).normal(
                    size=self.dim).astype(np.float32)
                self._c[t] = (v / np.linalg.norm(v)).tolist()
            return np.array(self._c[t])

    async def extractor(text, feedback):
        def m(name):
            i = text.find(name)
            return EntityMention(surface=name, span=Span(start=i, end=i + len(name),
                                                        text=name))
        return ExtractionResult(
            entities=[
                Entity(name="腾讯控股有限公司", type="ORG", aliases=["腾讯"],
                       mentions=[m("腾讯控股有限公司")]),
                Entity(name="深圳", type="LOC", mentions=[m("深圳")]),
                Entity(name="马化腾", type="PER", mentions=[m("马化腾")]),
            ],
            relations=[
                Relation(head="腾讯控股有限公司", predicate="总部位于", tail="深圳"),
                Relation(head="马化腾", predicate="创立", tail="腾讯控股有限公司"),
            ])

    pool = await init_pool()
    store = EntityStore(pool)
    await store.init_schema()
    await pool.execute("TRUNCATE entities RESTART IDENTITY CASCADE")
    await pool.execute("TRUNCATE pipeline_runs RESTART IDENTITY")
    emb = FakeEmbedder()
    jobs = JobStore(pool)
    await jobs.init_schema()
    await pool.execute("TRUNCATE extraction_jobs RESTART IDENTITY")
    dash = Dashboard(pool)
    await dash.init_schema()

    deps = AppDeps(pool=pool, store=store, kb=KnowledgeBase(store, emb),
                   ingestor=Ingestor(store, emb), job_store=jobs,
                   dashboard=dash, extractor_fn=extractor)
    app = create_app(deps)

    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://t") as ac:
        async def show(title, method, url, **kw):
            r = await getattr(ac, method)(url, **kw)
            print(f"\n{title}\n  {method.upper()} {url} -> {r.status_code}")
            import json
            print("  ", json.dumps(r.json(), ensure_ascii=False)[:500])
            return r

        print("=" * 78)
        print("进程内 HTTP 演示（不起端口）")
        print("=" * 78)
        await show("健康检查", "get", "/healthz")
        r = await show("实时抽取+入库", "post", "/extract",
                       json={"text": TEXT, "document_id": "lesson14"})
        await show("别名定位", "get", "/entities/locate",
                   params={"query": "腾讯", "k": 1})
        await show("入边关系（谁创立了腾讯）", "get", "/relations",
                   params={"name": "腾讯", "direction": "in"})
        # 一条看板指标
        await dash.record(RunRecorder(
            document_id="lesson14", n_entities=3, n_relations=2, span_ratio=1.0,
            review_passed=True, n_auto_merged=0, n_queued=0, n_created=3))
        await show("质量看板", "get", "/metrics/dashboard")

        # HITL：登记一条合并并裁决
        a = await store.get_entity_id("腾讯控股有限公司")
        b = await store.upsert_entity(
            name="腾讯公司", type="ORG", embedding=emb.embed_one("腾讯公司"))
        rid = await store.enqueue_review(
            survivor_id=a, duplicate_id=b, survivor_name="腾讯控股有限公司",
            duplicate_name="腾讯公司", similarity=0.9, reason="教学演示")
        await show("待审列表", "get", "/reviews")
        await show("人工批准合并", "post", f"/reviews/{rid}/decision",
                   json={"approved": True})

        # 作业
        jobfile = ROOT / "data/samples/batch/doc_a.txt"
        await show("登记批量作业", "post", "/jobs",
                   json={"document_ref": str(jobfile)})
        await show("查询作业", "get", "/jobs?status=pending")

    await close_pool(pool)
    print("\n" + "=" * 78)
    print("真实 HTTP 模式（需先在另一终端启动）：")
    print("  python -m moray.api.server --port 8000")
    print("  curl 'http://127.0.0.1:8000/entities/locate?query=腾讯'")
    print("  # 交互式 API 文档：http://127.0.0.1:8000/docs")
    print("=" * 78)


def http_demo() -> None:
    """只打印真实 HTTP 的调用清单（需要你自己起服务）。"""
    import json
    import urllib.request

    base = "http://127.0.0.1:8000"
    for path in ["/healthz", "/metrics/dashboard",
                 "/entities/locate?query=腾讯&k=2"]:
        try:
            with urllib.request.urlopen(base + path, timeout=5) as r:
                print(path, "->", r.status, r.read().decode()[:300])
        except Exception as exc:  # noqa: BLE001
            print(path, "-> 服务未启动？", repr(exc)[:120])


if __name__ == "__main__":
    if HTTP:
        http_demo()
    else:
        asyncio.run(inproc_demo())
