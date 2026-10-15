"""规则抽取器测试：/nr /ns /nt 解析、连续 token 合并、全局偏移可回切原文。"""

from moray.extraction import clean_tagged_text, extract_from_tagged_text
from moray.extraction.rule_extractor import clean_tagged_line, parse_tagged_line


SAMPLE = (
    "19980101-01-001-002/m  中共中央/nt  总书记/n  、/w  国家/n  主席/n  "
    "江/nr  泽民/nr  抵达/v  深圳/ns  。/w\n"
    "19980101-01-001-003/m  华为/nt  公司/n  在/p  深圳/ns  投资/v  。/w\n"
)


def test_parse_line_strips_sentence_id():
    sent_id, tokens = parse_tagged_line(SAMPLE.splitlines()[0])
    assert sent_id == "19980101-01-001-002"
    # 江、泽民两个 token 都被解析出来
    words = [w for w, tag, _, _ in tokens]
    assert "江" in words and "泽民" in words


def test_consecutive_nr_merges_into_person():
    result = extract_from_tagged_text(SAMPLE)
    persons = {e.name: e for e in result.entities if e.type == "PER"}
    # 江/nr 泽民/nr 连续合并
    assert "江泽民" in persons


def test_type_mapping():
    result = extract_from_tagged_text(SAMPLE)
    types = {e.name: e.type for e in result.entities}
    assert types.get("中共中央") == "ORG"
    assert types.get("华为") == "ORG"
    assert types.get("深圳") == "LOC"


def test_global_spans_slice_back_to_source():
    """全局偏移必须能从整份原文精确切回 span.text。"""
    result = extract_from_tagged_text(SAMPLE)
    for e in result.entities:
        for m in e.mentions:
            assert m.span is not None
            # span.text 记录原文真实切片（多 token 实体中间可能夹标签）
            assert SAMPLE[m.span.start:m.span.end] == m.span.text
    # 单 token 实体：原文切片就是干净实体名
    sz = next(e for e in result.entities if e.name == "深圳")
    assert all(m.span.text == "深圳" for m in sz.mentions)


def test_repeated_same_entity_aggregates_mentions():
    result = extract_from_tagged_text(SAMPLE)
    sz = next(e for e in result.entities if e.name == "深圳")
    # 深圳出现两行两次 -> 一个实体下挂多个 mention
    assert len(sz.mentions) == 2
    assert sz.confidence == 1.0


def test_clean_strips_id_and_pos_tags():
    line = "19980101-01-001-002/m  中共中央/nt  总书记/n  、/w  江/nr  泽民/nr"
    assert clean_tagged_line(line) == "中共中央总书记、江泽民"


def test_clean_text_multiline():
    out = clean_tagged_text(SAMPLE)
    assert "/" not in out          # 标签全部剥除
    assert "江泽民" in out
    assert "\n" in out             # 每行一句保留
