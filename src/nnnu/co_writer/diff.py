"""行级 diff（§7.13）：difflib 只做展示，不参与写回。

accept 写入的是模型改好的整段原文，diff 仅给前端画红绿行——所以这里不需要
什么花哨的对齐算法，行级 SequenceMatcher 足够，且对 LaTeX 长段落也便宜。
"""

import difflib

# op 形状固定，前端 web/lib/co-writer.ts 的同名类型跟着走
DiffOp = dict[str, str]  # {"tag": "eq"|"add"|"del", "text": 行文本（无换行符）}


def diff_lines(original: str, edited: str) -> list[DiffOp]:
    """按行比对，输出 op 序列：替换块显示为「先删后增」两段（方便红绿对照）。

    splitlines() 让 CRLF 与 LF 归一相认：行尾风格换了不该显示成整篇改动。
    """
    old = original.splitlines()
    new = edited.splitlines()
    matcher = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
    ops: list[DiffOp] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("equal", "delete"):
            ops.extend(
                {"tag": "eq" if tag == "equal" else "del", "text": line} for line in old[i1:i2]
            )
        elif tag == "insert":
            ops.extend({"tag": "add", "text": line} for line in new[j1:j2])
        else:  # replace：先整段删、再整段增
            ops.extend({"tag": "del", "text": line} for line in old[i1:i2])
            ops.extend({"tag": "add", "text": line} for line in new[j1:j2])
    return ops


def diff_stats(ops: list[DiffOp]) -> dict[str, int]:
    """行数统计：界面上显示「-2 行 +3 行」。"""
    return {
        "added": sum(1 for op in ops if op["tag"] == "add"),
        "deleted": sum(1 for op in ops if op["tag"] == "del"),
        "kept": sum(1 for op in ops if op["tag"] == "eq"),
    }
