"""瘦身结构化模型/提示词测试：开关打开时模型只输出逻辑实体与关系。"""

from moray.schema import (
    GENERAL_DOMAIN,
    build_extraction_model,
    build_system_prompt,
    to_core_result,
)


def test_full_schema_keeps_offsets_and_confidence_by_default():
    """默认（瘦身关）：模型 schema 仍含 mentions/confidence 与关系证据/置信度。"""
    model = build_extraction_model(GENERAL_DOMAIN)
    props = model.model_json_schema()
    entity_fields = set(model.model_fields["entities"].annotation.__args__[0].model_fields)
    assert {"mentions", "confidence", "aliases", "attributes"} <= entity_fields

    rel_fields = set(props["$defs"][next(k for k in props["$defs"] if k.endswith("Relation"))]["properties"])
    assert {"head", "predicate", "tail", "evidence_span", "confidence"} <= rel_fields


def test_slim_schema_omits_offsets_and_confidence():
    """瘦身开：实体只剩 name/type/aliases/attributes；关系只剩 head/predicate/tail。"""
    model = build_extraction_model(GENERAL_DOMAIN, slim=True)
    dyn_entity = model.model_fields["entities"].annotation.__args__[0]
    entity_fields = set(dyn_entity.model_fields)
    assert entity_fields == {"name", "type", "aliases", "attributes"}

    # 关系模型是瘦身后的内部模型（无证据偏移、无置信度）
    dyn_rel = model.model_fields["relations"].annotation.__args__[0]
    assert set(dyn_rel.model_fields) == {"head", "predicate", "tail"}


def test_slim_schema_accepts_logical_only_output():
    """瘦身后模型可以只给逻辑字段；to_core_result 仍产出稳定内核（span 为空待回填）。"""
    model = build_extraction_model(GENERAL_DOMAIN, slim=True)
    dyn = model.model_validate({
        "entities": [
            {"name": "华为", "type": "ORG", "aliases": ["华为公司"],
             "attributes": {"industry": "通信"}},
        ],
        "relations": [{"head": "任正非", "predicate": "创立", "tail": "华为"}],
    })
    core = to_core_result(dyn)
    e = core.entities[0]
    assert e.name == "华为" and e.type == "ORG"
    assert e.aliases == ["华为公司"]
    assert e.attributes == {"industry": "通信"}
    # 模型不再给出现位置/置信度 -> 内核字段取稳定默认，等待本地回填
    assert e.mentions == []
    assert e.confidence == 0.0
    r = core.relations[0]
    assert (r.head, r.predicate, r.tail) == ("任正非", "创立", "华为")
    assert r.evidence_span is None and r.confidence == 0.0


def test_slim_system_prompt_does_not_request_offsets():
    """瘦身提示词不再要求模型给出现位置，且明确位置由本地回填。"""
    prompt = build_system_prompt(GENERAL_DOMAIN, slim=True)
    # 不再出现「mentions 记录……字符偏移」这种要求模型输出位置的指令
    assert "mentions" not in prompt
    assert "记录每次原文出现" not in prompt
    # 明确告知不要输出出现位置/置信度（本地回填）
    assert "本地确定性回填" in prompt
    # 仍要求抽取逻辑实体与关系
    assert "标准名" in prompt

    # 默认提示词保持原样（仍要求 mentions 与偏移）
    full = build_system_prompt(GENERAL_DOMAIN)
    assert "mentions" in full and "记录每次原文出现" in full


def test_slim_output_payload_is_at_least_30pct_smaller():
    """瘦身目标（工单 01 度量验收的离线代理）：输出体积显著下降，目标 >=30%。

    用「模型本应输出的满负载结构」比较 JSON 字节数（输出 token 的确定性代理）：
    full 为每个实体重复输出每次 mention 的 surface/span.text/start/end 与置信度、
    每条关系重复证据句；slim 只输出逻辑实体/关系。出现越多，省得越多。
    """
    import json

    ents = [
        ("阿里巴巴", ["阿里", "BABA"]),
        ("杭州蚂蚁科技集团有限责任公司", ["蚂蚁集团", "蚂蚁金服"]),
        ("浙江菜鸟供应链管理有限公司", ["菜鸟", "菜鸟网络"]),
        ("马云", []),
        ("杭州", []),
    ]
    rels = [("马云", "创立", "阿里巴巴"),
            ("阿里巴巴", "总部位于", "杭州"),
            ("菜鸟", "子公司", "阿里巴巴")]

    # payload 由真实 schema 模型 dump 出来（不是手写 JSON），保证比较的正是
    # 模型实际被要求生成/实际会返回的字段形状。
    full_model = build_extraction_model(GENERAL_DOMAIN)
    slim_model = build_extraction_model(GENERAL_DOMAIN, slim=True)

    def full_payload(n_occ):
        data = {
            "entities": [
                {"name": n, "type": "ORG", "aliases": al,
                 "mentions": [
                     {"surface": n,
                      "span": {"start": i * 10 + k, "end": i * 10 + k + len(n),
                               "text": n}}
                     for k in range(n_occ)],
                 "confidence": 0.93, "attributes": {}}
                for i, (n, al) in enumerate(ents)],
            "relations": [
                {"head": h, "predicate": p, "tail": t,
                 "evidence_span": {"start": 0, "end": 40,
                                   "text": "证据句原文逐字重复通常较长" * 2},
                 "confidence": 0.88}
                for h, p, t in rels],
        }
        return full_model.model_validate(data).model_dump()

    def slim_payload():
        data = {
            "entities": [{"name": n, "type": "ORG", "aliases": al, "attributes": {}}
                         for n, al in ents],
            "relations": [{"head": h, "predicate": p, "tail": t} for h, p, t in rels],
        }
        return slim_model.model_validate(data).model_dump()

    slim = len(json.dumps(slim_payload(), ensure_ascii=False))
    for n_occ in (1, 3, 6):
        full = len(json.dumps(full_payload(n_occ), ensure_ascii=False))
        assert 1 - slim / full >= 0.30, (n_occ, slim, full)

    # schema 定义本身（进 tool/prompt）也应明显更短
    full_schema = len(json.dumps(
        build_extraction_model(GENERAL_DOMAIN).model_json_schema(), ensure_ascii=False))
    slim_schema = len(json.dumps(
        build_extraction_model(GENERAL_DOMAIN, slim=True).model_json_schema(),
        ensure_ascii=False))
    assert 1 - slim_schema / full_schema >= 0.30


def test_full_schema_and_prompt_match_pre_change_golden():
    """验收 7 的「逐字段一致」必须对着改造前的真实快照，而不是自己跟自己比。

    golden 由 262319b（改造前基线）的 build_extraction_model /
    build_system_prompt 直接导出：连字段顺序、JSON Schema 文案、提示词全文
    都逐字节比对；字段顺序变了会在这里立刻红。
    """
    import json
    from pathlib import Path

    golden = json.loads(
        (Path(__file__).parent / "golden" / "full_schema_262319b.json")
        .read_text(encoding="utf-8"))
    schema = build_extraction_model(GENERAL_DOMAIN).model_json_schema()
    assert list(schema["$defs"]["GeneralEntity"]["properties"]) == golden["entity_fields"]
    assert list(schema["$defs"]["Relation"]["properties"]) == golden["relation_fields"]
    assert schema == golden["schema"]
    assert build_system_prompt(GENERAL_DOMAIN) == golden["system_prompt"]
