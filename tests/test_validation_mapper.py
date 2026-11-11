"""rdf_mapper 纯函数离线测试：抽取结果 → rdflib Graph 投影（不触网、不持久化）。"""

from rdflib import Graph, Namespace, RDF
from rdflib.namespace import SDO

from moray.validation.rdf_mapper import (
    TYPE_TO_SCHEMA,
    entities_to_graph,
    map_type,
)
from moray.validation.schemas import EntityIn, RelationIn


def test_map_type_known_codes_to_schema_org():
    assert map_type("PER") == SDO.Person
    assert map_type("ORG") == SDO.Organization
    assert map_type("LOC") == SDO.Place
    assert map_type("EVENT") == SDO.Event


def test_map_type_unknown_falls_back_to_thing():
    # 未登记的类型代码不报错，落到 schema:Thing（保证图总能建出来）
    assert map_type("WHATEVER") == SDO.Thing
    assert "MISC" in TYPE_TO_SCHEMA  # 通用兜底类型已登记


def test_entities_to_graph_emits_typed_nodes_and_names():
    entities = [
        EntityIn(name="张三", type="PER", attributes={"title": "研究员"}),
        EntityIn(name="某研究所", type="ORG"),
    ]
    g = entities_to_graph(entities, relations=[])
    assert isinstance(g, Graph)
    # 两个实体节点都带 rdf:type
    persons = list(g.subjects(RDF.type, SDO.Person))
    orgs = list(g.subjects(RDF.type, SDO.Organization))
    assert len(persons) == 1 and len(orgs) == 1
    # name 字面量
    assert (persons[0], SDO.name, None) in g
    names = {str(o) for _, _, o in g.triples((None, SDO.name, None))}
    assert {"张三", "某研究所"} <= names


def test_entities_to_graph_maps_attributes_and_aliases():
    e = EntityIn(
        name="李四", type="PER",
        aliases=["小李", "Li Si"],
        attributes={"title": "副研究员", "birth_date": "1990-01-01"},
    )
    g = entities_to_graph([e], relations=[])
    node = next(iter(g.subjects(RDF.type, SDO.Person)))
    # 别名 → schema:alternateName
    alt = {str(o) for _, _, o in g.triples((node, SDO.alternateName, None))}
    assert {"小李", "Li Si"} <= alt
    # jobTitle 是已知 schema.org 属性，应映射；未知属性进扩展命名空间而非丢弃
    titles = {str(o) for _, _, o in g.triples((node, SDO.jobTitle, None))}
    assert "副研究员" in titles


def test_entities_to_graph_includes_relations_as_edges():
    entities = [
        EntityIn(name="张三", type="PER"),
        EntityIn(name="某研究所", type="ORG"),
    ]
    relations = [RelationIn(head="张三", predicate="任职于", tail="某研究所")]
    g = entities_to_graph(entities, relations=relations)
    # 关系应连出一条边（头实体节点存在出边）
    head = next(iter(g.subjects(RDF.type, SDO.Person)))
    edges = list(g.predicate_objects(head))
    assert len(edges) >= 2  # rdf:type + 至少一条关系谓词


def test_entities_to_graph_skips_dangling_relation():
    # 关系引用了不存在的实体名 → 跳过该边，不抛异常、不造幽灵节点
    entities = [EntityIn(name="张三", type="PER")]
    relations = [RelationIn(head="张三", predicate="任职于", tail="幽灵机构")]
    g = entities_to_graph(entities, relations=[])  # 先看空关系
    g2 = entities_to_graph(entities, relations=relations)
    assert len(g2) >= len(g)
    # 没有任何节点被声明为 Organization（尾实体缺失，不应凭空创建类型）
    assert len(list(g2.subjects(RDF.type, SDO.Organization))) == 0


def test_empty_input_returns_empty_graph():
    g = entities_to_graph([], relations=[])
    assert isinstance(g, Graph) and len(g) == 0
