"""
规则抽取器：解析"已标注语料"（L05）
====================================

像人民日报 199801 这类语料，每个词都带词性标签：
    中共中央/nt  总书记/n  、/w  江/nr  泽民/nr
其中实体类型已经现成：
    nr 人名(可连续多 token)  ns 地名  nt 机构名  nz 其他专名

对这种数据用 LLM 是浪费（慢、贵、还可能错）。这里用确定性解析直接产出
ExtractionResult，作为：
  · 免费、可复现的离线抽取路径；
  · L15 评测 LLM 抽取质量的黄金标准（ground truth）来源。

它和 LLMExtractor(=Extractor) 实现同一个高层约定：输入 Document，
产出 ExtractionResult，因此上层（落盘/归并/评测）可以无差别替换。
"""

from __future__ import annotations

import re

from ..schema import Entity, EntityMention, ExtractionResult, Span
from .document import Document

# 词性 -> 我们的通用领域类型
TAG_TO_TYPE = {
    "nr": "PER",
    "ns": "LOC",
    "nt": "ORG",
    "nz": "MISC",
}

# 行首形如 19980101-01-001-002/m 的句子 ID
_SENT_ID_RE = re.compile(r"^\s*(\d{8}-\d{2}-\d{3}-\d{3})/m\s*")
_TOKEN_RE = re.compile(r"(\S+?)/([A-Za-z]+)\b")


def parse_tagged_line(line: str) -> tuple[str, list[tuple[str, str, int, int]]]:
    """解析一行：返回 (句子ID, [(词面, 词性, 全局start, 全局end)])。

    偏移按【剥掉行首 ID 之后的纯文本】计算，用游标逐字符推进，
    多 token 连续同标签（江/nr 泽民/nr）会合并成一个实体。
    """
    m = _SENT_ID_RE.match(line)
    sent_id = m.group(1) if m else ""
    body = line[m.end():] if m else line

    tokens: list[tuple[str, str, int, int]] = []
    cursor = 0
    # 逐 token 扫描，同时在 body 里定位真实字符位置（兼容多空格）
    for tm in _TOKEN_RE.finditer(body):
        word, tag = tm.group(1), tm.group(2).lower()
        # tm.span() 是在 body 上的位置，直接可用
        tokens.append((word, tag, tm.start(1), tm.end(1)))
        cursor = tm.end()
    return sent_id, tokens


def extract_from_tagged_text(text: str) -> ExtractionResult:
    """把整份标注语料解析成 ExtractionResult（连续同标签自动合并）。

    所有 span 都换算成相对【整份 text】的全局字符偏移，
    使 verify_spans 等基于全局偏移的工具可直接使用。
    """
    # (type, 标准名) -> Entity
    entities: dict[tuple[str, str], Entity] = {}

    line_start = 0
    for raw_line in text.splitlines(keepends=True):
        # 当前行（不含换行）的全局区间
        line_len = len(raw_line.rstrip("\n").rstrip("\r"))
        line_text = raw_line[:line_len]
        m = _SENT_ID_RE.match(line_text)
        body_start_in_line = m.end() if m else 0
        body = line_text[body_start_in_line:]

        # 在 body 上找 token，再平移成全局偏移
        tokens: list[tuple[str, str, int, int]] = []
        for tm in _TOKEN_RE.finditer(body):
            gs = line_start + body_start_in_line + tm.start(1)
            ge = line_start + body_start_in_line + tm.end(1)
            tokens.append((tm.group(1), tm.group(2).lower(), gs, ge))

        run: list[tuple[str, int, int]] = []
        run_tag = ""

        def flush(tag: str, seg: list[tuple[str, int, int]]) -> None:
            if not seg or tag not in TAG_TO_TYPE:
                return
            etype = TAG_TO_TYPE[tag]
            surface = "".join(w for w, _, _ in seg).strip()
            if not surface:
                return
            start, end = seg[0][1], seg[-1][2]
            # 多 token 连续实体（江/nr 泽民/nr）在原文里夹着标签，
            # 因此 span.text 记录"原文真实切片"，干净实体名放在 surface。
            source_slice = text[start:end]
            key = (etype, surface)
            ent = entities.get(key)
            if ent is None:
                ent = Entity(name=surface, type=etype, confidence=1.0)
                entities[key] = ent
            if not any(
                mm.span and mm.span.start == start
                for mm in ent.mentions
            ):
                ent.mentions.append(
                    EntityMention(
                        surface=surface,
                        span=Span(start=start, end=end, text=source_slice),
                    )
                )

        for word, tag, s, e in tokens:
            if tag == run_tag and tag in TAG_TO_TYPE:
                run.append((word, s, e))
            else:
                flush(run_tag, run)
                run_tag = tag
                run = [(word, s, e)] if tag in TAG_TO_TYPE else []
        flush(run_tag, run)

        line_start += len(raw_line)

    return ExtractionResult(entities=list(entities.values()), relations=[])


def clean_tagged_line(line: str) -> str:
    """把一行标注语料还原成干净句子：剥掉行首 ID 和所有 /词性 标签。

    例：'.../m  中共中央/nt  总书记/n  、/w' -> '中共中央总书记、'
    """
    m = _SENT_ID_RE.match(line)
    body = line[m.end():] if m else line
    return "".join(tm.group(1) for tm in _TOKEN_RE.finditer(body))


def clean_tagged_text(text: str) -> str:
    """把整份标注语料还原为干净原文（每行一句），供 LLM 抽取使用。"""
    lines = [clean_tagged_line(ln) for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln.strip())


class RuleBasedExtractor:
    """与 Extractor 同名方法的轻量适配器（不调用模型）。"""

    domain_code = "general"

    async def extract(self, document: Document) -> ExtractionResult:  # noqa: D401
        return extract_from_tagged_text(document.text)
