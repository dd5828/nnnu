"""切块（§7.10）：事件边界优先、CJK 句界兜底、块间 10% 重叠。

单元是 `(ref, text)`：ref 是引用池里的行号引用体（如 `chat/2026-09.jsonl#12`），
text 是该事件渲染出的一行正文。切块只在两种地方落刀：

1. 事件边界（单元之间）——绝大多数情况；
2. 单个事件超过预算时按句界（。！？；!?;\n）再切——切出来的每片仍带原 ref，
   「该行内容支撑该事实」的对应关系不因切块而丢。

相邻块带 10% 重叠：跨块的语义（上一条说一半、下一条补完）不至于被拦腰截断；
重叠导致的重复事实由入库时的归一化去重兜住（不是靠模型自觉）。
"""

OVERLAP_RATIO = 0.1

_SENTENCE_END = "。！？；!?;\n"


def split_sentences(text: str) -> list[str]:
    """按中英句末标点切句；句末标点跟在前句尾；换行也算界。"""
    pieces: list[str] = []
    buffer: list[str] = []
    for char in text:
        buffer.append(char)
        if char in _SENTENCE_END:
            pieces.append("".join(buffer))
            buffer = []
    if buffer:
        pieces.append("".join(buffer))
    return pieces


def _split_long_unit(ref: str, text: str, budget: int) -> list[tuple[str, str]]:
    """单事件超预算：按句界聚成 ≤budget 的若干片（每片都带原 ref）。"""
    groups: list[list[str]] = []
    current: list[str] = []
    size = 0
    for piece in split_sentences(text):
        if current and size + len(piece) > budget:
            groups.append(current)
            current, size = [], 0
        current.append(piece)
        size += len(piece)
    if current:
        groups.append(current)
    if not groups:
        groups = [[""]]  # 病态输入（空文本）也保底占位，别丢 ref
    return [(ref, "".join(group)) for group in groups]


def chunk_units(
    units: list[tuple[str, str]], budget: int, *, overlap_ratio: float = OVERLAP_RATIO
) -> list[list[tuple[str, str]]]:
    """把事件单元按字符预算切块；返回的每块保持原次序。"""
    budget = max(1, int(budget))
    flat: list[tuple[str, str]] = []
    for ref, text in units:
        if len(text) <= budget:
            flat.append((ref, text))
        else:
            flat.extend(_split_long_unit(ref, text, budget))

    overlap_budget = int(budget * overlap_ratio)
    chunks: list[list[tuple[str, str]]] = []
    current: list[tuple[str, str]] = []
    size = 0
    for unit in flat:
        if current and size + len(unit[1]) > budget:
            chunks.append(current)
            overlap: list[tuple[str, str]] = []
            overlap_size = 0
            for previous in reversed(current):
                if overlap_budget <= 0 or overlap_size + len(previous[1]) > overlap_budget:
                    break
                overlap.insert(0, previous)
                overlap_size += len(previous[1])
            current = [*overlap, unit]
            size = overlap_size + len(unit[1])
        else:
            current.append(unit)
            size += len(unit[1])
    if current:
        chunks.append(current)
    return chunks
