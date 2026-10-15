"""
文本分块器（L05）
=================

大文件无法一次性送进模型，需要切成"模型吃得下、语义尽量完整"的块。

设计要点：
1. 用【token 预算】而不是字符数切块——模型上下文按 token 计。
   默认给一个不依赖外部库的保守估算器（CJK 单字/英文单词/标点各算一个 unit），
   生产中可替换成模型自带 tokenizer（DeepSeek/ARK BPE），接口不变。
2. 只在【句子边界】切，不把一句话劈成两半；超长单句再退化为按长度硬切。
3. 块间带 overlap（把上一块末尾的句子带进下一块），缓解实体被切成两半。
4. 每个 Chunk 记住自己在【原文】中的 [start, end) 字符偏移——
   这是块内 span 换算回全局 span、保证溯源准确的关键。
5. 分块是纯函数式的：输入文本，输出带偏移的块，不丢字、不改字
   （text[start:end] 必须恰好等于 chunk.text，有测试守护这条不变量）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# token 估算：不追求精确，追求"不超限"。宁可估多一点（块更小、更安全）。
# ---------------------------------------------------------------------------
_UNIT_RE = re.compile(
    r"[\u4e00-\u9fff]"      # 一个汉字算 1 个 unit
    r"|[A-Za-z0-9]+(?:\.[0-9]+)?"  # 一个英文/数字词算 1 个
    r"|[^\sA-Za-z0-9\u4e00-\u9fff]"  # 一个标点算 1 个
)


def estimate_tokens(text: str) -> int:
    """保守的 token 估算。中文偏密，切块预算按它设得略小即可。"""
    return len(_UNIT_RE.findall(text))


# 句子结束符（中英）+ 换行。保留结束符在句子里，便于直接还原原文。
_SENT_RE = re.compile(r"[^。！？!?；;\n]+[。！？!?；;\n]*")


@dataclass(frozen=True)
class Chunk:
    """原文中的一个块。[start, end) 是相对所属文本的字符偏移（左闭右开）。"""

    id: str
    text: str
    start: int
    end: int
    part_id: str = ""        # 所属文档/文章 ID（跨文章不归并）
    index: int = 0           # 该 part 内的序号

    @property
    def n_units(self) -> int:
        return estimate_tokens(self.text)


def split_sentences(text: str) -> list[tuple[int, int]]:
    """把文本切成句子，返回每句在原文中的 [start, end)。

    用游标 + 正则而不是 text.split，保证偏移永远精确。
    """
    sentences: list[tuple[int, int]] = []
    pos = 0
    while pos < len(text):
        m = _SENT_RE.match(text, pos)
        if not m:
            break
        if m.end() == pos:  # 理论上不会，防御死循环
            pos += 1
            continue
        sentences.append((m.start(), m.end()))
        pos = m.end()
    return sentences


class Chunker:
    """按 token 预算 + 句子边界贪婪打包的分块器。"""

    def __init__(
        self,
        max_units: int = 1500,
        overlap_units: int = 200,
        token_counter=estimate_tokens,
    ) -> None:
        self.max_units = max_units
        self.overlap_units = overlap_units
        self.count = token_counter

    def chunk_text(
        self,
        text: str,
        *,
        part_id: str = "doc",
        id_prefix: str = "doc",
    ) -> list[Chunk]:
        sentences = split_sentences(text)
        chunks: list[Chunk] = []
        bucket: list[tuple[int, int]] = []
        bucket_units = 0
        overlap_seed: list[tuple[int, int]] = []  # 上一块遗留的重叠句

        def flush(idx_counter: int) -> int:
            nonlocal bucket, bucket_units, overlap_seed
            if not bucket:
                return idx_counter
            start = bucket[0][0]
            end = bucket[-1][1]
            chunks.append(
                Chunk(
                    id=f"{id_prefix}#c{idx_counter:04d}",
                    text=text[start:end],
                    start=start,
                    end=end,
                    part_id=part_id,
                    index=idx_counter,
                )
            )
            idx_counter += 1
            # 为下一块准备 overlap：从当前桶末尾倒着取，累计不超过预算
            seed: list[tuple[int, int]] = []
            used = 0
            for s, e in reversed(bucket):
                u = self.count(text[s:e])
                if used + u > self.overlap_units:
                    break
                seed.insert(0, (s, e))
                used += u
            overlap_seed = seed
            bucket = []
            bucket_units = 0
            return idx_counter

        counter = 0
        # 第一块以空 overlap 开始；之后每块先带上一块尾部
        pending = list(overlap_seed)
        for s, e in sentences:
            sent = text[s:e]
            u = self.count(sent)

            # 单句本身超长 -> 先把已有内容成块，再硬切这句
            if u > self.max_units:
                if bucket:
                    counter = flush(counter)
                    pending = []
                for hs, he in self._hard_split(text, s, e):
                    chunks.append(
                        Chunk(
                            id=f"{id_prefix}#c{counter:04d}",
                            text=text[hs:he], start=hs, end=he,
                            part_id=part_id, index=counter,
                        )
                    )
                    counter += 1
                continue

            if bucket_units + u > self.max_units and bucket:
                counter = flush(counter)
                pending = list(overlap_seed)
                bucket = list(pending)
                bucket_units = sum(self.count(text[a:b]) for a, b in bucket)

            bucket.append((s, e))
            bucket_units += u

        if bucket:
            counter = flush(counter)

        # 不变量自检：每块 text 必须恰好是原文切片
        for c in chunks:
            assert text[c.start:c.end] == c.text, f"块 {c.id} 偏移不自洽"
        return chunks

    def _hard_split(
        self, text: str, start: int, end: int
    ) -> list[tuple[int, int]]:
        """对超长单句按字符预算硬切（极少触发）。"""
        out = []
        cur = start
        while cur < end:
            probe = cur
            units = 0
            while probe < end and units < self.max_units:
                probe += 1
                units = self.count(text[cur:probe])
            out.append((cur, probe))
            cur = probe
        return out
