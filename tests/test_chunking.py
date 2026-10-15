"""分块器测试：偏移不变量、不丢字、重叠、超长句硬切（纯本地，不联网）。"""

from moray.extraction import Chunker, estimate_tokens, split_sentences


def test_token_estimation():
    assert estimate_tokens("华为") == 2          # 两个汉字
    assert estimate_tokens("hello world") == 2  # 两个英文词
    assert estimate_tokens("，。") == 2          # 两个标点


def test_sentence_offsets_cover_text():
    text = "华为在深圳。马云到访了杭州！真的吗？"
    spans = split_sentences(text)
    # 拼起来应与原文一致（允许句子间无遗漏）
    rebuilt = "".join(text[s:e] for s, e in spans)
    assert rebuilt == text
    # 首句边界正确
    assert text[spans[0][0]:spans[0][1]] == "华为在深圳。"


def test_chunk_offsets_are_exact_slices():
    text = "华为在深圳。阿里巴巴总部在杭州。任正非是创始人。字节在北京。" * 5
    chunks = Chunker(max_units=20, overlap_units=5).chunk_text(text)
    assert len(chunks) > 1
    for c in chunks:
        # 核心不变量：chunk.text 必须恰好等于原文切片
        assert text[c.start:c.end] == c.text
        assert c.n_units <= 20 or len(c.text) <= 20 + 2  # overlap 可能略微越过
    # 序号连续、part_id 透传
    assert [c.index for c in chunks] == list(range(len(chunks)))
    assert all(c.part_id == "doc" for c in chunks)


def test_chunks_cover_first_and_last_content():
    text = "第一句话在这里。第二句话也在这里。第三句话收尾了。"
    chunks = Chunker(max_units=8, overlap_units=2).chunk_text(text)
    # 第一个块必须包含开头，最后一个块必须包含结尾
    assert chunks[0].start == 0
    assert chunks[-1].end == len(text.rstrip()) or chunks[-1].end >= len(text) - 1


def test_overlap_repeats_sentence():
    # 很小的预算 + 非零 overlap，相邻块文本应有重复（重叠句）
    text = "甲乙丙丁。戊己庚辛。壬癸子丑。寅卯辰巳。"
    chunks = Chunker(max_units=6, overlap_units=5).chunk_text(text)
    assert len(chunks) >= 3
    # 至少存在一对相邻块，后者包含前者末尾的某个句子
    repeats = 0
    for a, b in zip(chunks, chunks[1:]):
        if any(sent and sent in b.text for sent in a.text.split("。")):
            repeats += 1
    assert repeats >= 1


def test_very_long_sentence_hard_split():
    text = "字" * 100 + "。"
    chunks = Chunker(max_units=10, overlap_units=0).chunk_text(text)
    # 超长单句被硬切成多块，且每块仍是原文精确切片
    assert len(chunks) >= 5
    for c in chunks:
        assert text[c.start:c.end] == c.text
