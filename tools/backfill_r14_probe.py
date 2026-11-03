"""工单 03 r14 评审发现的确定性离线探针（无模型、无 DB）。

逐条打印本轮修复的可观测结果：

* F1：无标点单句块上关系证据配对边配对边限流（时间/峰值内存/候选数）；
* F2：别名存值规范化（回填输出 + Ingestor 落库入参）；
* F3：缓存载荷版本已递增，旧 v1 键不再命中；
* F4：从 ``03-r13-stress-ab.json`` 复算逐篇最差/均值；
* F5：同一非法候选上限只告警一次，后续降为 DEBUG。

用法::

    PYTHONPATH=src python tools/backfill_r14_probe.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import statistics
import time
import tracemalloc
from pathlib import Path

from moray.extraction import cache as cache_mod
from moray.extraction import local_backfill as lb
from moray.extraction.document import ExtractionOutput
from moray.kb.ingestor import Ingestor
from moray.schema import Entity, ExtractionResult, Relation


def _pair_text(repeats: int) -> str:
    return "华为 深圳 " * repeats


def _backfill_pairing(repeats: int):
    text = _pair_text(repeats)
    logical = ExtractionResult(
        entities=[Entity(name="华为", type="ORG"), Entity(name="深圳", type="LOC")],
        relations=[Relation(head="华为", predicate="位于", tail="深圳")],
    )
    tracemalloc.start()
    tracemalloc.reset_peak()
    started = time.perf_counter()
    _filled, report = lb.backfill_provenance(logical, text)
    elapsed = time.perf_counter() - started
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    cands = report.relation_evidence_candidates[
        next(iter(report.relation_evidence_candidates))]
    return text, cands, elapsed, peak / (1024 * 1024)


def probe_f1() -> None:
    print("== F1 无标点块：边配对边限流 ==")
    # 3000 字、500×500：旧实现先物化 250k 条再排序；这里同口径模拟旧路径对照。
    text3000, cands3000, new_elapsed, new_peak = _backfill_pairing(500)
    heads = [i for i in range(0, len(text3000), 6)]
    tails = [i + 3 for i in range(0, len(text3000), 6)]
    tracemalloc.start()
    tracemalloc.reset_peak()
    started = time.perf_counter()
    legacy = [
        (hs, ts, 0, len(text3000), abs(hs - ts))
        for hs in heads for ts in tails
    ]
    legacy.sort(key=lambda p: p[4])
    legacy_elapsed = time.perf_counter() - started
    _current, legacy_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print(f"  3000 字 500×500: 旧={legacy_elapsed:.3f}s/"
          f"{legacy_peak / 1024 / 1024:.1f}MB/{len(legacy)} 对; "
          f"新={new_elapsed:.3f}s/{new_peak:.1f}MB/{len(cands3000)} 条候选")
    assert len(cands3000) == lb.MAX_INTERNAL_RELATION_EVIDENCE_CANDIDATES
    assert cands3000[0]["selected"] is True
    assert cands3000[0]["distance"] == 3

    # 6000 字、1000×1000：只跑新路径，验证最坏形态仍是百毫秒级/低内存。
    _text6000, cands6000, elapsed6000, peak6000 = _backfill_pairing(1000)
    print(f"  6000 字 1000×1000: 新={elapsed6000:.3f}s/{peak6000:.1f}MB/"
          f"{len(cands6000)} 条候选")
    assert len(cands6000) == lb.MAX_INTERNAL_RELATION_EVIDENCE_CANDIDATES


class _CapturingStore:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def upsert_entity(self, **kwargs):
        self.calls.append(kwargs)
        return len(self.calls)


class _StubEmbedder:
    def embed_one(self, _text: str):
        return [0.0]


def probe_f2() -> None:
    print("== F2 别名存值规范化 ==")
    filled, _report = lb.backfill_provenance(
        ExtractionResult(entities=[
            Entity(name=" 腾讯 ", type="ORG",
                   aliases=[" 鹅厂 ", "腾讯", "鹅厂", "", "   "]),
            Entity(name="腾讯", type="ORG", aliases=[" 鹅厂 ", "企鹅帝国"]),
        ]),
        "腾讯在深圳。",
    )
    aliases = filled.entities[0].aliases
    print(f"  backfill 输出: name={filled.entities[0].name!r}, aliases={aliases!r}")
    assert aliases == ["鹅厂", "企鹅帝国"]

    store = _CapturingStore()
    output = ExtractionOutput(
        document_id="doc", domain_code="general", result=filled)
    asyncio.run(Ingestor(store, _StubEmbedder()).ingest(output))
    print(f"  Ingestor 落库入参: name={store.calls[0]['name']!r}, "
          f"aliases={store.calls[0]['aliases']!r}")
    assert store.calls[0]["aliases"] == ["鹅厂", "企鹅帝国"]


def probe_f3() -> None:
    print("== F3 缓存载荷版本 ==")
    current = cache_mod._PAYLOAD_VERSION
    key_v2 = cache_mod.build_cache_key("fp", "m", "正文")
    original = cache_mod._PAYLOAD_VERSION
    cache_mod._PAYLOAD_VERSION = 1
    try:
        key_v1 = cache_mod.build_cache_key("fp", "m", "正文")
    finally:
        cache_mod._PAYLOAD_VERSION = original
    print(f"  _PAYLOAD_VERSION={current}（v1 键与当前键不同：{key_v1 != key_v2}）")
    assert current >= 2
    assert key_v1 != key_v2


def probe_f4() -> None:
    print("== F4 逐篇最差/均值复算 ==")
    data = json.loads(
        Path("docs/evidence/03-r13-stress-ab.json").read_text(encoding="utf-8"))
    per_doc: dict[str, list[float]] = {}
    for repeat in data["repeats"]:
        full = {c["name"]: c["entity_relaxed_f1"] for c in repeat["full"]["cases"]}
        slim = {c["name"]: c["entity_relaxed_f1"] for c in repeat["slim"]["cases"]}
        for name, before in full.items():
            per_doc.setdefault(name, []).append((slim[name] - before) * 100)
    for name, deltas in per_doc.items():
        print(f"  {name}: " + " / ".join(f"{d:+.2f}" for d in deltas)
              + f" pt, 逐篇均值 {statistics.mean(deltas):+.2f}pt")
    worst_name, worst_round = min(
        ((name, i) for name, deltas in per_doc.items()
         for i, _d in enumerate(deltas)),
        key=lambda item: per_doc[item[0]][item[1]],
    )
    worst = per_doc[worst_name][worst_round]
    print(f"  逐篇最差: {worst_name} 第 {worst_round + 1} 轮 {worst:+.2f}pt")
    assert abs(worst - (-1.59)) < 0.01
    assert abs(statistics.mean(per_doc["stress_seed20261104"]) - 0.79) < 0.01


def probe_f5() -> None:
    print("== F5 负数上限只告警一次 ==")
    report = lb.BackfillReport()
    report.relation_evidence_candidates = {"k": [{"selected": True}]}
    records: list[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("moray.extraction.local_backfill")
    handler = _Collect()
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    lb._NEGATIVE_CANDIDATE_LIMIT_WARNED.discard(-7777)
    try:
        report.to_dict(max_relation_evidence_candidates=-7777)
        report.to_dict(max_relation_evidence_candidates=-7777)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)
    levels = [r.levelname for r in records if "-7777" in r.getMessage()]
    print(f"  -7777 两次序列化的日志级别: {levels}")
    assert levels == ["WARNING", "DEBUG"]


if __name__ == "__main__":
    probe_f1()
    probe_f2()
    probe_f3()
    probe_f4()
    probe_f5()
    print("全部探针通过")
