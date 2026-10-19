"""
入库器 Ingestor（L08）
======================

把抽取流水线产出的 ExtractionResult 写进 pgvector 知识库：
  实体   -> 计算 name 向量 -> upsert entities + aliases + mentions(provenance)
  关系   -> 头尾实体名解析为 id -> upsert relations + evidence

幂等性：
  · 实体按 (domain,type,name) 唯一，重复写入只更新向量/属性、合并别名与出处；
  · 关系按 (domain, source, predicate, target) 唯一；
  · 出处/证据带唯一约束，同一文档重复 ingest 不会产生重复行。

它与 L04 的 JsonFileSink 角色相同（都是 ExtractionOutput 的"出口"），
只是换成了真正的数据库——上层流水线不用改。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..embedding import Embedder, get_embedder
from ..extraction.document import ExtractionOutput
from .store import EntityStore


@dataclass
class IngestReport:
    """一次入库的结果统计（供日志/指标中间件使用）。"""

    entities_written: int = 0
    relations_written: int = 0
    relations_skipped: list[str] = field(default_factory=list)
    entity_ids: dict[str, int] = field(default_factory=dict)


class Ingestor:
    def __init__(
        self,
        store: EntityStore,
        embedder: Embedder | None = None,
        *,
        create_missing_endpoints: bool = True,
    ) -> None:
        self.store = store
        self.embedder = embedder or get_embedder()
        # 关系头/尾若不在实体列表也不在库里时，是否自动建一个占位实体
        self.create_missing_endpoints = create_missing_endpoints

    async def ingest(self, output: ExtractionOutput, *, chunk_id: str | None = None) -> IngestReport:
        result = output.result
        domain = output.domain_code
        doc_id = output.document_id
        report = IngestReport()

        # ---------- 1) 实体 ----------
        for ent in result.entities:
            vector = self.embedder.embed_one(ent.name)
            mentions = [
                {
                    "document_id": doc_id,
                    "surface": m.surface,
                    "start": m.span.start if m.span else None,
                    "end": m.span.end if m.span else None,
                    "chunk_id": chunk_id,
                }
                for m in ent.mentions
            ]
            eid = await self.store.upsert_entity(
                name=ent.name,
                type=ent.type,
                embedding=vector,
                domain_code=domain,
                aliases=ent.aliases,
                attributes=ent.attributes,
                confidence=ent.confidence,
                mentions=mentions,
            )
            report.entity_ids[ent.name] = eid
        report.entities_written = len(result.entities)

        # ---------- 2) 关系 ----------
        for rel in result.relations:
            src_id = await self._resolve(rel.head, domain, report)
            dst_id = await self._resolve(rel.tail, domain, report)
            if src_id is None or dst_id is None:
                report.relations_skipped.append(
                    f"{rel.head} -[{rel.predicate}]-> {rel.tail}"
                )
                continue

            rid = await self.store.upsert_relation(
                source_id=src_id,
                predicate=rel.predicate,
                target_id=dst_id,
                domain_code=domain,
                confidence=rel.confidence,
            )
            if rel.evidence_span is not None:
                sp = rel.evidence_span
                await self.store.add_relation_evidence(
                    rid,
                    document_id=doc_id,
                    ev={
                        "surface": sp.text,
                        "start": sp.start,
                        "end": sp.end,
                        "chunk_id": chunk_id,
                    },
                )
            report.relations_written += 1

        return report

    async def _resolve(
        self, name: str, domain: str, report: IngestReport
    ) -> int | None:
        """把关系里的实体名解析成 id：先看本批，再查库，最后按需建占位实体。"""
        if name in report.entity_ids:
            return report.entity_ids[name]

        eid = await self.store.get_entity_id(name, domain_code=domain)
        if eid is not None:
            report.entity_ids[name] = eid
            return eid

        if self.create_missing_endpoints:
            # 抽取结果里关系提到、但实体列表漏掉的兜底：建 MISC 占位，
            # 便于后续在审核台发现并补类型（不应常见，常见说明抽取 Schema 有缺口）
            vector = self.embedder.embed_one(name)
            eid = await self.store.upsert_entity(
                name=name, type="MISC", embedding=vector, domain_code=domain
            )
            report.entity_ids[name] = eid
            return eid
        return None
