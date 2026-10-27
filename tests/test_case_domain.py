"""波次 4：case_domain 影子域包（data/domains/case_domain.json）的离线契约测试。

域包是 langextract 双路对比（minirag 影子抽取）的领域配置，经
`python -m moray.config_center.cli import data/domains/case_domain.json`
落 DB 配置中心后被 /extract 按 code 解析。本测试不连 DB，只钉死形状：

  · 六类 TypeSpec 的 code 必须与 langextract 主表六类逐字一致（零映射）；
  · 每类主属性 key 对齐 langextract entity_dict 现网字典；
  · relation_predicates 必须是 langextract 65 谓词白名单的子集（两侧词表
    同源，对比器才能按同一 predicate_key 语义展示差异）。
"""

import json
from pathlib import Path

from moray.schema import domain_from_dict, domain_to_dict

SPEC_PATH = Path(__file__).resolve().parent.parent / "data" / "domains" / "case_domain.json"

# 与 langextract entity_dict 六类逐字一致（波次 4 定案：零映射）
EXPECTED_TYPES = {
    "person", "organization", "clue", "case", "special_case", "archive_case",
}

# 各类主属性键（源自 langextract entity_dict 现网行，测试内嵌避免跨仓依赖）
PRIMARY_ATTRS = {
    "person": "chinese_name",
    "organization": "org_name",
    "clue": "clue_name",
    "case": "case_name",
    "special_case": "special_case_name",
    "archive_case": "archive_case_name",
}

# langextract 0013 的 65 谓词白名单（同源快照；影子只引导其中的高频子集）
WHITELIST_65 = {
    "kinship", "accomplice", "colleague", "introduce", "same_person", "work_in",
    "found", "invests", "control", "member_of", "contact_of", "suspect_of",
    "victim_of", "reporter_of", "witness_of", "informant_of", "related_to_case",
    "lead_special", "special_member", "special_target", "clue_target_person",
    "clue_informant", "clue_person", "archive_subject_person",
    "archive_mention_person", "archive_owner_person", "org_invest", "parent_sub",
    "org_subordinate", "org_cooperate", "same_org", "org_involved_case",
    "org_victim_case", "org_reporter_case", "org_handle_case", "org_lead_special",
    "org_assist_special", "org_target_special", "clue_target_org",
    "clue_informant_org", "clue_org", "archive_subject_org", "archive_owner_org",
    "archive_include_org", "case_related", "case_merge", "case_upstream",
    "case_same_group", "case_belong_special", "case_in_special", "clue_source_case",
    "clue_to_case", "case_archive", "case_include", "special_sub",
    "special_cooperate", "clue_source_special", "special_archive",
    "clue_same_origin", "clue_merge", "clue_same_target", "clue_archive",
    "archive_ref", "archive_same_volume", "archive_next",
}


def _spec():
    return json.loads(SPEC_PATH.read_text(encoding="utf-8"))


def test_domain_parses_and_codes_align():
    domain = domain_from_dict(_spec())
    assert domain.code == "case_domain"
    assert {t.code for t in domain.types} == EXPECTED_TYPES
    assert domain.relation_predicates and set(domain.relation_predicates) <= WHITELIST_65


def test_primary_attrs_present_and_required():
    domain = domain_from_dict(_spec())
    for code, primary in PRIMARY_ATTRS.items():
        t = domain.get(code)
        assert t is not None and t.guide, code
        attr = next((a for a in t.attributes if a.key == primary), None)
        assert attr is not None and attr.required, f"{code}.{primary}"


def test_roundtrip_lossless():
    domain = domain_from_dict(_spec())
    again = domain_from_dict(domain_to_dict(domain))
    assert again == domain, "domain_to_dict/from_dict 必须无损（配置中心 JSONB 存取依赖）"
