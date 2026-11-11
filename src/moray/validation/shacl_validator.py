"""SHACL 校验封装（pyshacl）。

关键工程决策（见 eval-schemaorg-pyshacl.md 实测）：
- 形状/词汇进程级常驻、只加载一次：避免每次请求重复解析；
- 默认 inference="none" 且不把全量本体塞进每次校验：纯项目形状单批 ~4ms，
  而挂全量本体做 RDFS 推理有 ~1s/次固定开销；
- 完全离线：只从包内 vocab/ 读本地文件，pyshacl 不联网；
- 结构化违约：解析 validation report 为 Violation 列表，供前端字段级高亮；
- 降级：校验器自身异常不阻断主流程，返回 degraded=True 的空违约结果，
  由调用方回退到仅 Pydantic 结构校验。
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

import pyshacl
from rdflib import Graph, Namespace
from rdflib.namespace import RDF

from .rdf_mapper import entities_to_graph
from .schemas import ValidationRequest, ValidationResponse, Violation

logger = logging.getLogger(__name__)

_VOCAB_DIR = Path(__file__).resolve().parent / "vocab"
_SHACL = Namespace("http://www.w3.org/ns/shacl#")

# 领域代码 → 形状文件名；未登记领域回退通用形状
_DOMAIN_SHAPES: dict[str, str] = {
    "general": "talent-shapes.ttl",
}
_DEFAULT_SHAPES = "talent-shapes.ttl"


@lru_cache(maxsize=8)
def _load_shapes(domain_code: str) -> Graph:
    """进程级常驻加载某领域的 SHACL 形状图（只解析一次）。"""
    fname = _DOMAIN_SHAPES.get(domain_code, _DEFAULT_SHAPES)
    path = _VOCAB_DIR / fname
    g = Graph()
    g.parse(path.as_posix(), format="turtle")
    return g


def _local_name(uri: object) -> str:
    """取 IRI 末段，如 http://schema.org/name → name。"""
    s = str(uri)
    for sep in ("/", "#"):
        if sep in s:
            s = s.rsplit(sep, 1)[-1]
    return s


def _first(graph: Graph, subject, predicate):
    """安全取单个对象：不存在返回 None（避免 next(iter()) 抛 StopIteration）。"""
    for obj in graph.objects(subject, predicate):
        return obj
    return None


def _parse_violations(report: Graph) -> list[Violation]:
    """把 pyshacl 的 validation report 图解析成结构化 Violation 列表。"""
    violations: list[Violation] = []
    for result in report.subjects(RDF.type, _SHACL.ValidationResult):
        focus = _first(report, result, _SHACL.focusNode)
        path = _first(report, result, _SHACL.resultPath)
        component = _first(report, result, _SHACL.sourceConstraintComponent)
        message = _first(report, result, _SHACL.resultMessage)
        value = _first(report, result, _SHACL.value)
        violations.append(
            Violation(
                focus_node=str(focus) if focus is not None else "",
                path=_local_name(path) if path is not None else "",
                constraint=_local_name(component) if component is not None else "Constraint",
                message=str(message) if message is not None else "",
                value=None if value is None else str(value),
            )
        )
    return violations


async def validate_extraction(request: ValidationRequest) -> ValidationResponse:
    """校验一次抽取产物，返回结构化结果；校验器故障时降级而非抛错。"""
    entity_count = len(request.entities)
    relation_count = len(request.relations)
    try:
        data_graph = entities_to_graph(request.entities, request.relations)
        shapes = _load_shapes(request.domain_code)
        conforms, report_graph, _ = pyshacl.validate(
            data_graph,
            shacl_graph=shapes,
            inference="none",  # 实测：关闭推理，毫秒级；需要时另开常驻本体通道
            debug=False,
        )
        return ValidationResponse(
            conforms=bool(conforms),
            violations=_parse_violations(report_graph),
            entity_count=entity_count,
            relation_count=relation_count,
            degraded=False,
        )
    except Exception as exc:  # noqa: BLE001 校验器故障不外泄、不阻断导入主流程
        logger.warning("SHACL 校验失败，降级为仅结构校验: %r", exc)
        return ValidationResponse(
            conforms=True,  # 降级时不拦截，交给 Pydantic/人工兜底
            violations=[],
            entity_count=entity_count,
            relation_count=relation_count,
            degraded=True,
        )
