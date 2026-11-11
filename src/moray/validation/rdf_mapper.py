"""把抽取结果（Entity/Relation）临时投影为 rdflib Graph。

设计约束：
- 纯函数、离线：只在请求内构造 RDF 图，校验完即弃，不写库、不联网；
- 类型对齐 schema.org：moray 的类型代码（PER/ORG/...）映射到 schema: 类；
- 已知属性走 schema.org 标准谓词，领域专有/未知属性进独立扩展命名空间，
  既享受标准词汇对齐，又不丢失 JSONB 属性包里的信息；
- 关系尾实体缺失时跳过该边，不凭空造节点。
"""

from __future__ import annotations

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, SDO

from .schemas import EntityIn, RelationIn

# 项目扩展命名空间：schema.org 没有的领域专有属性挂这里，避免污染标准词汇。
# 注意：SHACL 形状文件里用同名前缀 attr:，两者 IRI 必须完全一致，否则形状静默不匹配。
EX = Namespace("http://talent.local/attr/")

# moray 领域类型代码 → schema.org 类
TYPE_TO_SCHEMA: dict[str, URIRef] = {
    "PER": SDO.Person,
    "ORG": SDO.Organization,
    "LOC": SDO.Place,
    "EVENT": SDO.Event,
    "MISC": SDO.Thing,
}

# 领域属性键 → schema.org 标准属性（白名单，命中走标准谓词）
# 同时兼容 snake_case 与 camelCase 两种键（talent 档案用 camelCase）
ATTR_TO_SCHEMA: dict[str, URIRef] = {
    "title": SDO.jobTitle,
    "job_title": SDO.jobTitle,
    "position": SDO.jobTitle,
    "email": SDO.email,
    "description": SDO.description,
    "birth_date": SDO.birthDate,
    "birthDate": SDO.birthDate,
    "founded_date": SDO.foundingDate,
    "industry": SDO.industry,
    "address": SDO.address,
    "telephone": SDO.telephone,
    "legal_representative": EX.legalRepresentative,
}


def map_type(type_code: str) -> URIRef:
    """类型代码映射到 schema.org 类；未登记代码兜底到 schema:Thing。"""
    return TYPE_TO_SCHEMA.get((type_code or "").strip().upper(), SDO.Thing)


def _node_iri(index: int) -> URIRef:
    """用稳定的本地占位 IRI 标识实体（仅请求内有效，不对外持久化）。"""
    return URIRef(f"http://talent.local/entity/{index}")


def _attr_predicate(key: str) -> URIRef:
    """属性键 → 谓词：白名单走 schema.org，其余进扩展命名空间。"""
    mapped = ATTR_TO_SCHEMA.get(key.strip())
    if mapped is not None:
        return mapped
    return EX[key.strip()]


def entities_to_graph(
    entities: list[EntityIn], relations: list[RelationIn]
) -> Graph:
    """把实体与关系投影为 RDF 图。"""
    g = Graph()
    g.bind("schema", SDO)
    g.bind("ex", EX)

    # 名称 → 节点 IRI，供关系连边
    name_to_iri: dict[str, URIRef] = {}
    for idx, ent in enumerate(entities):
        iri = _node_iri(idx)
        name_to_iri[ent.name] = iri
        g.add((iri, RDF.type, map_type(ent.type)))
        if ent.name:
            g.add((iri, SDO.name, Literal(ent.name)))
        if ent.description:
            g.add((iri, SDO.description, Literal(ent.description)))
        for alias in ent.aliases:
            if alias:
                g.add((iri, SDO.alternateName, Literal(alias)))
        for key, value in (ent.attributes or {}).items():
            if value is None or value == "":
                continue
            g.add((iri, _attr_predicate(str(key)), Literal(str(value))))

    # 关系连边：头尾都能在本批实体中找到才连，否则跳过（防幽灵节点）
    for rel in relations:
        head = name_to_iri.get(rel.head)
        tail = name_to_iri.get(rel.tail)
        if head is None or tail is None:
            continue
        pred = EX[f"rel/{rel.predicate.strip()}"]
        g.add((head, pred, tail))

    return g
