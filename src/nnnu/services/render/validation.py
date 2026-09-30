"""可视化产出的确定性校验与抠取（§7.7）。

模型产出的代码过不过、从输出里抠哪一段，全在这里说了算（不碰 LLM）：
- 不过时给出的 reason 会喂回同段的「修一次」提示词与 code_retry 修复提示词；
- 前端运行期再炸（浏览器特有的渲染问题）由渲染组件自己兜底成错误卡。

**正文围栏契约**（前端 web/lib/render.ts 同款常量，改这里要同步改那边）：
- 可视化正文 = 标题 + 一句说明 + 恰好一个渲染围栏（```svg / ```echarts / ```mermaid / ```html）；
- 动画正文 = summary + ```nnnu-artifact（载荷见 models.artifact_payload）+ ```python 源码 + 日志节选。
"""

from __future__ import annotations

import html
import json
import re
from typing import Any
from xml.etree import ElementTree

from nnnu.services.render.models import MANIM_RENDER_TYPE

# 渲染围栏的语言标记（用户可见契约）；echarts 的选项是 JSON、动画源码是 Python，
# 各留一个常见别名，模型写成 ```json / ```py 也不当失败处理
ARTIFACT_FENCE = "nnnu-artifact"
FENCE_ALIASES: dict[str, tuple[str, ...]] = {
    "svg": ("svg", "xml"),
    "echarts": ("echarts", "json"),
    "mermaid": ("mermaid",),
    "html": ("html",),
    MANIM_RENDER_TYPE: ("python", "py"),
}

# mermaid 的图类型关键字（首行判定用；`stateDiagram-v2` 这类带后缀，用前缀匹配）
_MERMAID_KEYWORDS: tuple[str, ...] = (
    "graph",
    "flowchart",
    "sequencediagram",
    "classdiagram",
    "statediagram",
    "erdiagram",
    "gantt",
    "pie",
    "journey",
    "mindmap",
    "timeline",
    "gitgraph",
    "xychart",
    "quadrantchart",
    "sankey",
    "block",
)

# 流程图节点形状的定界符（开 → 闭）。**顺序敏感**：同长度的双字符定界符必须排在
# 单字符前面，否则 `A[(x)]` 会先匹配到 `[`，把形状语法误判成标签里有禁忌字符。
_MERMAID_SHAPES: dict[str, str] = {
    "([": "])",
    "[[": "]]",
    "[(": ")]",
    "[/": "/]",
    "{{": "}}",
    "((": "))",
    "[": "]",
    "{": "}",
    "(": ")",
    ">": "]",
}
# 未加引号的标签里这些字符会让 mermaid 解析器报错（对 mermaid 逐例实测；
# `<` `>` 在标签里没事，不在列）
_MERMAID_LABEL_FORBIDDEN = "[]{}()|"
_MERMAID_NODE_RE = re.compile(
    r"([A-Za-z0-9_一-鿿]+)\s*("
    + "|".join(re.escape(opener) for opener in sorted(_MERMAID_SHAPES, key=len, reverse=True))
    + r")"
)
_QUOTED_SEGMENT_RE = re.compile(r'"[^"]*"')

# 交互/复杂效果的降级兜底：一张纯静态 HTML 说明卡（不进 DB，也不依赖前端）
_FALLBACK_HTML_TEMPLATE = """<!doctype html>
<html lang="{lang}">
<head>
<meta charset="utf-8" />
<title>{title}</title>
</head>
<body style="margin:0;display:flex;align-items:center;justify-content:center;
min-height:100vh;background:#0f172a;color:#e2e8f0;font-family:system-ui,sans-serif">
<div style="max-width:36rem;padding:2rem;text-align:center">
<h1 style="font-size:1.1rem">{title}</h1>
<p style="color:#94a3b8;line-height:1.6">{note}</p>
</div>
</body>
</html>"""

# 抠 JSON 对象（模型爱在 JSON 前后加解释或围栏，先剥围栏再找花括号）
_FENCE_ANY_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)
# 基类名以 Scene 结尾即可（Scene / ThreeDScene / scene.Scene / MovingCameraScene）
_SCENE_CLASS_RE = re.compile(r"class\s+(\w+)\s*\(\s*(?:[\w.]+\.)*\w*Scene\s*[,)]")

# 需求里出现这些词就直接定类型（分析段 JSON 解析失败时的确定性兜底）
_REQUEST_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (MANIM_RENDER_TYPE, ("动画", "animate", "animation", "manim")),
    ("mermaid", ("流程图", "流程", "时序图", "状态图", "flowchart", "sequence diagram", "mermaid")),
    ("echarts", ("图表", "曲线", "函数图", "折线", "柱状", "饼图", "统计", "chart", "plot")),
    ("html", ("交互", "互动", "可操作", "interactive", "playground")),
    ("svg", ("示意图", "结构图", "几何", "diagram", "schematic")),
)


def extract_fenced(text: str, langs: tuple[str, ...]) -> str | None:
    """抠出第一个 ```<lang> 围栏的内容；容忍未闭合（吃到文末）。"""
    for lang in langs:
        match = re.search(rf"```{re.escape(lang)}[ \t]*\r?\n(.*?)(?:\r?\n```|\Z)", text, re.DOTALL)
        if match is None:
            continue
        body = match.group(1).strip(" \r\n")
        if body:
            return body
    return None


def extract_render_code(text: str, render_type: str) -> str | None:
    """按渲染类型抠代码块（含别名，见 FENCE_ALIASES）。"""
    aliases = FENCE_ALIASES.get(render_type)
    if aliases is None:
        return None
    return extract_fenced(text, aliases)


def extract_artifact_json(text: str) -> dict[str, Any] | None:
    """抠 ```nnnu-artifact 围栏里的 JSON 载荷（后端自查用；前端解析在 render.ts）。"""
    body = extract_fenced(text, (ARTIFACT_FENCE,))
    if body is None:
        return None
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def validate_visualization(code: str, render_type: str) -> tuple[bool, str]:
    """确定性校验（ok, reason）；reason 会进日志与修复提示词，写中文。"""
    if not code.strip():
        return False, "产出为空"
    if render_type == "svg":
        return _validate_svg(code)
    if render_type == "echarts":
        return _validate_echarts(code)
    if render_type == "mermaid":
        return _validate_mermaid(code)
    if render_type == "html":
        return _validate_html(code)
    return False, f"未知渲染类型 {render_type!r}"


def _validate_svg(code: str) -> tuple[bool, str]:
    try:
        root = ElementTree.fromstring(code)
    except ElementTree.ParseError as exc:
        return False, f"SVG 不是合法 XML：{exc}"
    tag = root.tag.rsplit("}", 1)[-1]
    if tag != "svg":
        return False, f"根元素是 <{tag}> 而不是 <svg>"
    return True, ""


def _validate_echarts(code: str) -> tuple[bool, str]:
    try:
        data = json.loads(code)
    except json.JSONDecodeError as exc:
        return False, f"选项不是合法 JSON：{exc}"
    if not isinstance(data, dict):
        return False, "选项顶层不是 JSON 对象"
    series = data.get("series")
    if not isinstance(series, list) or not series:
        return False, "选项缺少非空的 series 数组"
    return True, ""


def _validate_mermaid(code: str) -> tuple[bool, str]:
    for line in code.splitlines():
        stripped = line.strip()
        # 空行与 %% 指令/注释行跳过；首行有效内容必须是图类型关键字
        if not stripped or stripped.startswith("%%"):
            continue
        head = stripped.split()[0].lower()
        if not any(head.startswith(keyword) for keyword in _MERMAID_KEYWORDS):
            return False, f"首行 {stripped[:60]!r} 不是 mermaid 图类型关键字"
        break
    else:
        return False, "内容为空"
    # 流程图一族再查节点标签里的禁忌字符（其它图类型的 {} [] 可能是语法本体）
    if head.startswith(("graph", "flowchart")):
        defect = _mermaid_label_defect(code)
        if defect is not None:
            return False, defect
    return True, ""


def _mermaid_label_defect(code: str) -> str | None:
    """找第一个标签里带禁忌字符的节点（行号 + 片段），全干净返回 None。

    实测结论（对 mermaid 解析器逐例验证，见 tests/test_render_validation.py）：
    未加引号的节点标签里出现 `[ ] { } ( ) |` 就会解析失败（`A[swap arr[j]]`、
    `A[a | b]`、`H{arr(j) > 1 ?}` 都挂）；把整段标签包上双引号即通过。
    `<` `>` 在标签里没事，不在此列。
    """
    for lineno, line in enumerate(code.splitlines(), start=1):
        for match in _MERMAID_NODE_RE.finditer(line):
            opener = match.group(2)
            label_start = match.end()
            label_end = _find_mermaid_closer(line, label_start, _MERMAID_SHAPES[opener])
            if label_end is None:  # 定界符跨行或没闭合——不在这里判，交给模型/前端
                continue
            unquoted = _QUOTED_SEGMENT_RE.sub("", line[label_start:label_end])
            hits = sorted({ch for ch in unquoted if ch in _MERMAID_LABEL_FORBIDDEN})
            if hits:
                bad = "".join(hits)
                return (
                    f"mermaid 第 {lineno} 行节点标签里有解析器会报错的字符 {bad!r}"
                    f"（片段：{line.strip()[:60]}）；"
                    '把标签用双引号整个包起来（如 A["swap arr[j]"]），或去掉这些字符'
                )
    return None


def _find_mermaid_closer(text: str, start: int, closer: str) -> int | None:
    """从 start 起找第一个不在双引号里的 closer 位置（引号里的定界符不算数）。"""
    in_quotes = False
    index = start
    while index < len(text):
        char = text[index]
        if char == '"':
            in_quotes = not in_quotes
        elif not in_quotes and text.startswith(closer, index):
            return index
        index += 1
    return None


def _validate_html(code: str) -> tuple[bool, str]:
    lowered = code.lower()
    structural = ("<html", "<body", "<div", "<svg", "<canvas", "<table", "<main", "<section")
    if not any(tag in lowered for tag in structural):
        return False, "没有可识别的结构标签（div/svg/canvas/table/section 任一）"
    return True, ""


def parse_analysis(text: str) -> dict[str, Any]:
    """分析段输出 → JSON 字典；解析不出来返回空字典（调用方走 guess_render_type 兜底）。"""
    body = text
    fences = _FENCE_ANY_RE.findall(text)
    if fences:
        body = "\n".join(fences)
    match = _JSON_OBJECT_RE.search(body)
    if match is None:
        return {}
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def guess_render_type(request_text: str) -> str:
    """分析段不可用时的确定性路由（关键词表，见 _REQUEST_KEYWORDS）。"""
    lowered = request_text.lower()
    for render_type, keywords in _REQUEST_KEYWORDS:
        if any(keyword in lowered for keyword in keywords):
            return render_type
    return "svg"  # 兜底给最通用的示意图


def extract_scene_class(code: str) -> str | None:
    """找 manim 的场景类名（真渲染命令行要用）；找不到返回 None。"""
    match = _SCENE_CLASS_RE.search(code)
    return match.group(1) if match else None


def build_fallback_html(heading: str, note: str, lang: str = "zh") -> str:
    """生成兜底 HTML（内容由调用方经提示词渲染，本函数只管装箱与转义）。"""
    return _FALLBACK_HTML_TEMPLATE.format(
        lang=html.escape(lang), title=html.escape(heading), note=html.escape(note)
    )
