"""Book 活书引擎模型与纯校验（§7.14）。

表在 schema v14（见 services/sessions/schema.py）；一章一页，块是 book_pages.blocks
里 JSON 数组的成员（不单表，每块独立 blk- id 供编辑/作答寻址）。本模块只管与存储、
LLM 都无关的部分：枚举、上限常量、素材选择规范化、spine 与块 payload 的确定性校验。

spine 校验的两个入口：`spine_issues()` 扫原始 dict（含上游词汇混入、验收三件套这类
「能收下但要说出来」的问题）；`parse_spine()` 做收编（截断/补缺/过滤），只有结构上
没法用（顶层不是对象、一个章节都没有）才抛 BookError。
"""

import time
import unicodedata
from typing import Any, Literal

from pydantic import BaseModel, Field

from nnnu.core.ids import new_id

# 书状态：paused 是 §14「编译中断可续」的落点（进程死了/用户暂停都停在这）
BookStatus = Literal["draft", "compiling", "paused", "ready", "error"]
BOOK_STATUSES: tuple[str, ...] = ("draft", "compiling", "paused", "ready", "error")

# 块状态：pending 待生成 / compiling 正在生成 / done 成稿 / error 生成失败（不炸整书）
BlockStatus = Literal["pending", "compiling", "done", "error"]
BLOCK_STATUSES: tuple[str, ...] = ("pending", "compiling", "done", "error")

# §7.14 十二类块。与前端 web/lib/book.ts、prompts/{zh,en}/book.yaml 的 blocks.<type>
# 键三处同表；表结构没有 CHECK 约束，加类型不用迁移（沿用 notebooks 的类型白名单惯例）。
BlockType = Literal[
    "text",
    "callout",
    "quiz",
    "flashcard",
    "timeline",
    "code",
    "figure",
    "interactive_html",
    "animation",
    "concept_graph",
    "deep_dive",
    "note",
]
BLOCK_TYPES: tuple[str, ...] = (
    "text",
    "callout",
    "quiz",
    "flashcard",
    "timeline",
    "code",
    "figure",
    "interactive_html",
    "animation",
    "concept_graph",
    "deep_dive",
    "note",
)

# 「figure 家族」的 payload 都是含围栏的正文 Markdown（P7 渲染契约即围栏，前端
# Markdown.tsx 自动分发）：各类型至少要有一条对应围栏，深度校验在编译期过
# services/render/validation.py（本模块只查围栏在不在）。
FENCE_TAGS: dict[str, tuple[str, ...]] = {
    "figure": ("svg", "echarts", "mermaid"),
    "interactive_html": ("html",),
    "animation": ("nnnu-artifact",),
}

CALLOUT_VARIANTS: tuple[str, ...] = ("info", "tip", "warn", "important")
CONTENT_TYPES: tuple[str, ...] = ("theory", "derivation", "history", "practice", "concept")
QUESTION_FILTERS: tuple[str, ...] = ("all", "wrong", "ids")

MAX_TITLE_CHARS = 120  # 书名/章题上限（与笔记本名同一个量级）
MAX_SUMMARY_CHARS = 400
MAX_OBJECTIVES = 6
MAX_CHAPTERS = 16  # 上限而非目标：提示词里要 3-6 章，16 只是防跑飞
MAX_BLOCKS_PER_CHAPTER = 8
MAX_SOURCE_IDS = 20  # 每类素材一次最多勾这么多（够用，又不是把整个库塞爆）
MATERIAL_BUDGET_CHARS = 24_000  # 素材 digest 总预算（inputs.py 截断用）
CHARS_PER_TOKEN = 3.5  # 中文为主的经验比值（estimate.py 粗算输入 token）
MAX_MARKDOWN_CHARS = 20_000
MAX_ERROR_CHARS = 500
MAX_FLASHCARDS = 12  # 提示词要 3-8 张，校验放宽到 12
MAX_QUIZ_OPTIONS = 8
MAX_TIMELINE_EVENTS = 24
MAX_GRAPH_NODES = 40
MAX_GRAPH_EDGES = 80

# 每类块的输出 token 预算：估算按它算钱，生成时也按它设 max_tokens（略高于预算防截断）
OUTPUT_TOKEN_BUDGET: dict[str, int] = {
    "text": 900,
    "callout": 300,
    "deep_dive": 1200,
    "quiz": 700,
    "flashcard": 800,
    "timeline": 600,
    "code": 900,
    "figure": 1400,
    "interactive_html": 1400,
    "animation": 1600,
    "concept_graph": 600,
    "note": 0,  # 用户手写，零 LLM
}

_CHAPTER_KEYS = {
    "key",
    "title",
    "summary",
    "objectives",
    "content_type",
    "blocks_plan",
    "source_refs",
}
_PLAN_KEYS = {"type", "focus"}
_GRAPH_NODE_KEYS = {"id", "label", "group"}
_GRAPH_EDGE_KEYS = {"source", "target", "label"}


class BookError(ValueError):
    """Book 参数非法。code 供路由层映射到 §9.1 错误码（照 CoWriterError 手法）。"""

    def __init__(self, message: str, *, code: str = "invalid_request") -> None:
        super().__init__(message)
        self.code = code


# ---- 小工具 ----


def _text(raw: Any, limit: int) -> str:
    """任意值归一成去空白字符串并截断（LLM 输出常用；None/非字符串按空串）。"""
    return (raw if isinstance(raw, str) else "").strip()[:limit]


def _id_list(raw: Any, limit: int = MAX_SOURCE_IDS) -> list[str]:
    """素材 id 列表：strings、去重、保序、截断。"""
    if not isinstance(raw, list):
        return []
    seen: list[str] = []
    for item in raw:
        if isinstance(item, str) and item.strip() and item.strip() not in seen:
            seen.append(item.strip())
    return seen[:limit]


def _as_list(raw: Any) -> list[Any]:
    """LLM 输出里本该是数组的字段可能是字符串——按空数组处理，别拿字符当元素迭代。"""
    return raw if isinstance(raw, list) else []


# ---- 书名与素材选择 ----


def _clean_title(raw: Any, what: str) -> str:
    normalized = unicodedata.normalize("NFC", (raw if isinstance(raw, str) else "").strip())
    if not normalized:
        raise BookError(f"{what}不能为空", code="invalid_title")
    if len(normalized) > MAX_TITLE_CHARS:
        raise BookError(f"{what}最多 {MAX_TITLE_CHARS} 个字符", code="invalid_title")
    if any(unicodedata.category(char) == "Cc" for char in normalized):
        raise BookError(f"{what}不能包含控制字符", code="invalid_title")
    return normalized


def validate_title(title: str) -> str:
    """书名校验并归一（NFC）：与笔记本名同规则。"""
    return _clean_title(title, "书名")


def validate_chapter_title(title: str) -> str:
    """章节名校验并归一（spine 编辑改名用）。"""
    return _clean_title(title, "章节名")


def validate_sources(raw: Any) -> dict[str, Any]:
    """素材选择规范化；至少一类非空。形状：

    {"kbs": [kb-...], "notebooks": [nb-...], "sessions": [sess-...],
     "questions": {"filter": "all"|"wrong"|"ids", "ids": [...]}}
    """
    if not isinstance(raw, dict):
        raise BookError("素材选择形状不对", code="invalid_sources")
    kbs = _id_list(raw.get("kbs"))
    notebooks = _id_list(raw.get("notebooks"))
    sessions = _id_list(raw.get("sessions"))
    questions: dict[str, Any] | None = None
    raw_questions = raw.get("questions")
    if isinstance(raw_questions, dict):
        filt = raw_questions.get("filter", "all")
        if filt not in QUESTION_FILTERS:
            raise BookError(f"题库筛选只认 {'、'.join(QUESTION_FILTERS)}", code="invalid_sources")
        ids = _id_list(raw_questions.get("ids"), limit=200)
        if filt == "ids" and not ids:
            raise BookError("按题选集时至少要有一道题", code="invalid_sources")
        questions = {"filter": filt, "ids": ids}
    if not (kbs or notebooks or sessions or questions):
        raise BookError("至少选一类素材（知识库 / 笔记本 / 题库 / 会话）", code="invalid_sources")
    return {"kbs": kbs, "notebooks": notebooks, "questions": questions, "sessions": sessions}


# ---- spine（章节树 + 概念图） ----


class SourceRef(BaseModel):
    """块/章的来源引用（展示 + 页聊天接地说明用）。"""

    kind: Literal["kb", "notebook", "question", "session"]
    ref: str = ""
    label: str = ""


class BlockPlan(BaseModel):
    type: str
    focus: str = ""


class Chapter(BaseModel):
    key: str
    title: str
    summary: str = ""
    objectives: list[str] = Field(default_factory=list)
    content_type: str = "theory"
    blocks_plan: list[BlockPlan] = Field(default_factory=list)
    source_refs: list[SourceRef] = Field(default_factory=list)


class ConceptNode(BaseModel):
    id: str
    label: str
    group: str = ""


class ConceptEdge(BaseModel):
    source: str
    target: str
    label: str = ""


class ConceptGraph(BaseModel):
    nodes: list[ConceptNode] = Field(default_factory=list)
    edges: list[ConceptEdge] = Field(default_factory=list)


class Spine(BaseModel):
    """books.spine 列的完整形状：章节树 + 概念图 + 生成参数（语言/素材规模）。

    language/material_chars 是服务侧记录的编译参数（LLM 原始输出里没有）：
    前者定整本书的输出语言，后者给估算做输入 token 的基数（material_chars
    是素材 digest 的总字符数，每章按章数均分粗算）。spine 编辑（PATCH）时
    由服务侧回填，不能被编辑器抹掉。
    """

    chapters: list[Chapter] = Field(default_factory=list)
    concept_graph: ConceptGraph = Field(default_factory=ConceptGraph)
    language: str = "zh"
    material_chars: int = 0


def spine_issues(raw: Any) -> list[str]:
    """扫原始 spine dict 的「问题清单」（中文、可直接拼进修复提示词）。

    只报问题不修复；收编动作在 parse_spine。上游词汇混入、三件套缺失这类
    「能收下但要说出来」的都在这——spine.py 拿它做一次修复重试。
    """
    if not isinstance(raw, dict):
        return ["spine 顶层不是 JSON 对象"]
    issues: list[str] = []
    chapters = raw.get("chapters")
    if not isinstance(chapters, list) or not chapters:
        return ["spine 里没有 chapters（一个章节都没有）"]
    if len(chapters) > MAX_CHAPTERS:
        issues.append(f"章节数 {len(chapters)} 超过上限 {MAX_CHAPTERS}，多余的会被丢掉")
    planned: set[str] = set()
    for index, chapter in enumerate(chapters, start=1):
        tag = f"第 {index} 章"
        if not isinstance(chapter, dict):
            issues.append(f"{tag}不是对象")
            continue
        unknown = set(chapter) - _CHAPTER_KEYS
        if unknown:
            issues.append(
                f"{tag}出现未定义的键 {'、'.join(sorted(unknown))}（要按 book.yaml 的键名）"
            )
        if not _text(chapter.get("title"), MAX_TITLE_CHARS):
            issues.append(f"{tag}缺 title")
        if not _text(chapter.get("summary"), MAX_SUMMARY_CHARS):
            issues.append(f"{tag}缺 summary")
        objectives = chapter.get("objectives")
        if not isinstance(objectives, list) or not objectives:
            issues.append(f"{tag}缺 objectives（不能用别的键名代替）")
        content_type = chapter.get("content_type")
        if content_type not in CONTENT_TYPES:
            issues.append(f"{tag}的 content_type 不在 {'、'.join(CONTENT_TYPES)} 里")
        plans = chapter.get("blocks_plan")
        if not isinstance(plans, list) or not plans:
            issues.append(f"{tag}没有 blocks_plan")
            continue
        if len(plans) > MAX_BLOCKS_PER_CHAPTER:
            issues.append(f"{tag}的块数 {len(plans)} 超过上限 {MAX_BLOCKS_PER_CHAPTER}")
        for plan in plans:
            if not isinstance(plan, dict):
                issues.append(f"{tag}的块计划里有非对象项")
                continue
            plan_unknown = set(plan) - _PLAN_KEYS
            if plan_unknown:
                issues.append(
                    f"{tag}的块计划出现未定义的键 {'、'.join(sorted(plan_unknown))}（只要 type/focus）"
                )
            block_type = plan.get("type")
            if block_type not in BLOCK_TYPES:
                issues.append(f"{tag}用了未知块类型 {block_type!r}，允许：{'、'.join(BLOCK_TYPES)}")
            else:
                planned.add(block_type)
    for required in ("quiz", "flashcard", "figure"):
        if required not in planned:
            issues.append(f"整书计划里没有 {required} 块（验收要求 quiz/flashcard/figure 各 ≥1）")
    graph = raw.get("concept_graph")
    if graph is not None:
        issues.extend(_graph_issues(graph))
    return issues


def _graph_issues(graph: Any) -> list[str]:
    if not isinstance(graph, dict):
        return ["concept_graph 不是对象"]
    issues: list[str] = []
    nodes = graph.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        issues.append("concept_graph 没有 nodes")
        return issues
    node_ids: set[str] = set()
    for node in nodes:
        if not isinstance(node, dict):
            issues.append("concept_graph 里有非对象节点")
            continue
        unknown = set(node) - _GRAPH_NODE_KEYS
        if unknown:
            issues.append(
                f"概念图节点出现未定义的键 {'、'.join(sorted(unknown))}（只要 id/label/group）"
            )
        node_id = _text(node.get("id"), 80)
        if not node_id:
            issues.append("概念图节点缺 id")
        elif node_id in node_ids:
            issues.append(f"概念图节点 id 重复：{node_id}")
        else:
            node_ids.add(node_id)
        if not _text(node.get("label"), 80):
            issues.append(f"概念图节点 {node_id or '?'} 缺 label")
    edges = graph.get("edges")
    if edges is not None and not isinstance(edges, list):
        issues.append("concept_graph 的 edges 不是数组")
        return issues
    for edge in edges or []:
        if not isinstance(edge, dict):
            issues.append("概念图里有非对象边")
            continue
        unknown = set(edge) - _GRAPH_EDGE_KEYS
        if unknown:
            issues.append(
                f"概念图边出现未定义的键 {'、'.join(sorted(unknown))}（只要 source/target/label）"
            )
        for end in ("source", "target"):
            ref = _text(edge.get(end), 80)
            if ref not in node_ids:
                issues.append(f"概念图边的 {end}={ref!r} 不在节点表里")
    return issues


def parse_spine(raw: Any) -> tuple[Spine, list[str]]:
    """把 LLM 的 spine 原始输出收编成 Spine 模型；issues 是收编时放宽掉的问题。

    结构上没法用（顶层不是对象/一个章节都没有/所有章节的块计划都无效）才抛
    BookError（code=invalid_spine）；其余一律「收下 + 记账」，返回的 issues 供
    调用方决定修复重试或原样继续。
    """
    if not isinstance(raw, dict):
        raise BookError("spine 顶层不是 JSON 对象", code="invalid_spine")
    issues = spine_issues(raw)
    chapters_raw = raw.get("chapters")
    if not isinstance(chapters_raw, list) or not chapters_raw:
        raise BookError("spine 里一个章节都没有", code="invalid_spine")
    chapters: list[Chapter] = []
    seen_keys: set[str] = set()
    for index, chapter in enumerate(chapters_raw[:MAX_CHAPTERS], start=1):
        chapter = chapter if isinstance(chapter, dict) else {}
        key = _text(chapter.get("key"), 40) or f"ch-{index}"
        if key in seen_keys:
            key = f"ch-{index}"
        seen_keys.add(key)
        content_type = chapter.get("content_type")
        plans = [
            BlockPlan(type=plan["type"], focus=_text(plan.get("focus"), 200))
            for plan in _as_list(chapter.get("blocks_plan"))
            if isinstance(plan, dict) and plan.get("type") in BLOCK_TYPES
        ][:MAX_BLOCKS_PER_CHAPTER]
        chapters.append(
            Chapter(
                key=key,
                title=_text(chapter.get("title"), MAX_TITLE_CHARS) or f"第 {index} 章",
                summary=_text(chapter.get("summary"), MAX_SUMMARY_CHARS),
                objectives=[
                    text
                    for objective in _as_list(chapter.get("objectives"))[:MAX_OBJECTIVES]
                    if (text := _text(objective, 200))
                ],
                content_type=content_type if content_type in CONTENT_TYPES else "theory",
                blocks_plan=plans,
                source_refs=_source_refs(chapter.get("source_refs")),
            )
        )
    if not any(chapter.blocks_plan for chapter in chapters):
        raise BookError("spine 所有章节的块计划都无效", code="invalid_spine")
    raw_chars = raw.get("material_chars")
    language = _text(raw.get("language"), 8)
    return (
        Spine(
            chapters=chapters,
            concept_graph=_parse_graph(raw.get("concept_graph")),
            language=language if language in ("zh", "en") else "zh",
            material_chars=raw_chars if isinstance(raw_chars, int) and raw_chars > 0 else 0,
        ),
        issues,
    )


def _source_refs(raw: Any) -> list[SourceRef]:
    """来源引用收编：kind 不在白名单的整条丢掉（LLM 可能写上游的 chat/manual）。"""
    refs: list[SourceRef] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        if kind not in ("kb", "notebook", "question", "session"):
            continue
        refs.append(
            SourceRef(
                kind=kind,
                ref=_text(item.get("ref"), 120),
                label=_text(item.get("label"), 120),
            )
        )
    return refs[:12]


def _parse_graph(raw: Any) -> ConceptGraph:
    """概念图收编：悬空边丢、重复节点丢、超限截断（问题已由 spine_issues 记账）。"""
    if not isinstance(raw, dict):
        return ConceptGraph()
    nodes: list[ConceptNode] = []
    node_ids: set[str] = set()
    for node in _as_list(raw.get("nodes")):
        if not isinstance(node, dict):
            continue
        node_id = _text(node.get("id"), 80)
        if not node_id or node_id in node_ids:
            continue
        node_ids.add(node_id)
        nodes.append(
            ConceptNode(
                id=node_id,
                label=_text(node.get("label"), 80) or node_id,
                group=_text(node.get("group"), 40),
            )
        )
        if len(nodes) >= MAX_GRAPH_NODES:
            break
    edges: list[ConceptEdge] = []
    for edge in _as_list(raw.get("edges")):
        if not isinstance(edge, dict):
            continue
        source, target = _text(edge.get("source"), 80), _text(edge.get("target"), 80)
        if source not in node_ids or target not in node_ids:
            continue
        edges.append(ConceptEdge(source=source, target=target, label=_text(edge.get("label"), 60)))
        if len(edges) >= MAX_GRAPH_EDGES:
            break
    return ConceptGraph(nodes=nodes, edges=edges)


# ---- 块 ----


class Block(BaseModel):
    """页内一个类型化块（存在 book_pages.blocks 的 JSON 里）。

    focus 是编译/重生成时「这一块写什么」的聚焦点（来自 spine 的 blocks_plan）：
    块一旦建成，续跑与单块重生成都靠它认任务——不存的话恢复编译时就没处找了。
    新建的待生成块 payload 是空对象（校验只对非空 payload 跑）：内容要么由编译侧
    生成时写入，要么由用户编辑时经 validate_block_payload 归一。
    """

    id: str
    type: str
    status: BlockStatus = "pending"
    title: str = ""
    focus: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    source_refs: list[SourceRef] = Field(default_factory=list)
    error: str = ""
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

    @classmethod
    def new(
        cls,
        *,
        block_type: str,
        payload: dict[str, Any] | None = None,
        title: str = "",
        focus: str = "",
        source_refs: list[dict[str, Any]] | None = None,
    ) -> "Block":
        if block_type not in BLOCK_TYPES:
            raise BookError(
                f"未知块类型 {block_type!r}，允许：{'、'.join(BLOCK_TYPES)}",
                code="invalid_block_type",
            )
        return cls(
            id=new_id("blk"),
            type=block_type,
            title=_text(title, MAX_TITLE_CHARS),
            focus=_text(focus, 200),
            # 空 payload = 待生成占位（还没内容可校验）；有内容就走按类型的校验
            payload=validate_block_payload(block_type, payload) if payload else {},
            source_refs=_source_refs(source_refs),
        )


def validate_block_payload(block_type: str, payload: Any) -> dict[str, Any]:
    """按类型校验并归一 payload（编译生成与用户编辑都走这里）。

    只做确定性校验（形状/上限/引用完整性）；figure 家族的渲染合法性在编译期
    另过 services/render/validation.py（那是 LLM 代码的深水区，不在这）。
    """
    if not isinstance(payload, dict):
        raise BookError("块 payload 不是对象", code="invalid_payload")
    if block_type in ("text", "callout", "deep_dive", "note"):
        return _markdown_payload(block_type, payload)
    if block_type in FENCE_TAGS:
        return _fence_payload(block_type, payload)
    if block_type == "quiz":
        return _quiz_payload(payload)
    if block_type == "flashcard":
        return _flashcard_payload(payload)
    if block_type == "timeline":
        return _timeline_payload(payload)
    if block_type == "code":
        return _code_payload(payload)
    if block_type == "concept_graph":
        return _graph_payload(payload)
    raise BookError(f"未知块类型 {block_type!r}", code="invalid_block_type")


def _markdown_payload(block_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    markdown = payload.get("markdown")
    if not isinstance(markdown, str):
        raise BookError(f"{block_type} 块缺 markdown 正文", code="invalid_payload")
    if len(markdown) > MAX_MARKDOWN_CHARS:
        raise BookError(f"块正文最多 {MAX_MARKDOWN_CHARS} 个字符", code="invalid_payload")
    if block_type == "note":
        return {"markdown": markdown}  # 手写块允许先空着
    if not markdown.strip():
        raise BookError(f"{block_type} 块的正文不能为空", code="invalid_payload")
    if block_type == "callout":
        variant = payload.get("variant") or "info"
        if variant not in CALLOUT_VARIANTS:
            variant = "info"
        return {"markdown": markdown, "variant": variant}
    if block_type == "deep_dive":
        sources = [
            text for item in _as_list(payload.get("sources"))[:8] if (text := _text(item, 200))
        ]
        return {"markdown": markdown, "sources": sources}
    return {"markdown": markdown}


def _fence_payload(block_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    markdown = payload.get("markdown")
    if not isinstance(markdown, str) or not markdown.strip():
        raise BookError(f"{block_type} 块的正文不能为空", code="invalid_payload")
    if len(markdown) > MAX_MARKDOWN_CHARS:
        raise BookError(f"块正文最多 {MAX_MARKDOWN_CHARS} 个字符", code="invalid_payload")
    tags = FENCE_TAGS[block_type]
    if not any(f"```{tag}" in markdown for tag in tags):
        allowed = "/".join(f"```{tag}" for tag in tags)
        raise BookError(f"{block_type} 块要含一条 {allowed} 围栏", code="invalid_payload")
    return {"markdown": markdown}


def _quiz_payload(payload: dict[str, Any]) -> dict[str, Any]:
    stem = _text(payload.get("stem"), 2000)
    if not stem:
        raise BookError("quiz 块缺题干", code="invalid_payload")
    options: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in _as_list(payload.get("options")):
        if not isinstance(item, dict):
            continue
        key = _text(item.get("key"), 4).upper()
        text = _text(item.get("text"), 600)
        if not key or not text or key in seen:
            continue
        seen.add(key)
        options.append({"key": key, "text": text})
        if len(options) >= MAX_QUIZ_OPTIONS:
            break
    if len(options) < 2:
        raise BookError("quiz 块至少要两个选项", code="invalid_payload")
    answer_key = [
        text.upper() for item in _as_list(payload.get("answer_key")) if (text := _text(item, 4))
    ]
    if not answer_key or any(key not in seen for key in answer_key):
        raise BookError("quiz 的 answer_key 必须是选项里的 key", code="invalid_payload")
    return {
        "stem": stem,
        "options": options,
        "answer_key": sorted(set(answer_key)),
        "explanation": _text(payload.get("explanation"), 2000),
    }


def _flashcard_payload(payload: dict[str, Any]) -> dict[str, Any]:
    cards: list[dict[str, str]] = []
    for item in _as_list(payload.get("cards")):
        if not isinstance(item, dict):
            continue
        front, back = _text(item.get("front"), 500), _text(item.get("back"), 1000)
        if front and back:
            cards.append({"front": front, "back": back})
        if len(cards) >= MAX_FLASHCARDS:
            break
    if not cards:
        raise BookError("flashcard 块一张有效卡都没有", code="invalid_payload")
    return {"cards": cards}


def _timeline_payload(payload: dict[str, Any]) -> dict[str, Any]:
    events: list[dict[str, str]] = []
    for item in _as_list(payload.get("events")):
        if not isinstance(item, dict):
            continue
        when, title = _text(item.get("when"), 80), _text(item.get("title"), 200)
        if when and title:
            events.append({"when": when, "title": title, "detail": _text(item.get("detail"), 800)})
        if len(events) >= MAX_TIMELINE_EVENTS:
            break
    if not events:
        raise BookError("timeline 块一条有效事件都没有", code="invalid_payload")
    return {"events": events}


def _code_payload(payload: dict[str, Any]) -> dict[str, Any]:
    code = payload.get("code")
    if not isinstance(code, str) or not code.strip():
        raise BookError("code 块缺代码正文", code="invalid_payload")
    if len(code) > MAX_MARKDOWN_CHARS:
        raise BookError(f"代码最多 {MAX_MARKDOWN_CHARS} 个字符", code="invalid_payload")
    return {
        "language": _text(payload.get("language"), 40) or "text",
        "code": code,
        "explanation": _text(payload.get("explanation"), 2000),
    }


def _graph_payload(payload: dict[str, Any]) -> dict[str, Any]:
    graph = _parse_graph(payload)
    if not graph.nodes:
        raise BookError("concept_graph 块至少要一个节点", code="invalid_payload")
    return {
        "nodes": [node.model_dump() for node in graph.nodes],
        "edges": [edge.model_dump() for edge in graph.edges],
    }


# ---- 书 / 页 / 消息 / 作答 ----


class Book(BaseModel):
    id: str
    title: str
    sources: dict[str, Any] = Field(default_factory=dict)
    spine: dict[str, Any] = Field(default_factory=dict)
    status: BookStatus = "draft"
    fingerprints: dict[str, Any] = Field(default_factory=dict)
    error: str = ""
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

    @classmethod
    def new(cls, *, title: str, sources: Any, **kwargs: Any) -> "Book":
        return cls(
            id=new_id("bk"),
            title=validate_title(title),
            sources=validate_sources(sources),
            **kwargs,
        )


class BookPage(BaseModel):
    id: str
    book_id: str
    chapter_key: str
    page_no: int
    blocks: list[Block] = Field(default_factory=list)
    visited: bool = False
    bookmarked: bool = False
    updated_at: float = Field(default_factory=time.time)

    @classmethod
    def new(cls, *, book_id: str, chapter_key: str, page_no: int) -> "BookPage":
        return cls(id=new_id("bp"), book_id=book_id, chapter_key=chapter_key, page_no=page_no)


class PageMessage(BaseModel):
    """书页聊天的一条消息（book_page_messages）。"""

    id: str
    page_id: str
    role: Literal["user", "assistant"]
    content_md: str = ""
    citations: list[dict[str, Any]] = Field(default_factory=list)
    created_at: float = Field(default_factory=time.time)

    @classmethod
    def new(
        cls,
        *,
        page_id: str,
        role: Literal["user", "assistant"],
        content_md: str,
        citations: list[dict[str, Any]] | None = None,
    ) -> "PageMessage":
        return cls(
            id=new_id("bmsg"),
            page_id=page_id,
            role=role,
            content_md=content_md,
            citations=citations or [],
        )


class Attempt(BaseModel):
    """一次测验作答（book_attempts）：判分零 LLM，看选项 key 集合是否相等。"""

    id: str
    book_id: str
    page_id: str
    block_id: str
    answer: list[str] = Field(default_factory=list)
    correct: bool = False
    created_at: float = Field(default_factory=time.time)

    @classmethod
    def new(
        cls,
        *,
        book_id: str,
        page_id: str,
        block_id: str,
        answer: list[str],
        correct: bool,
    ) -> "Attempt":
        return cls(
            id=new_id("bat"),
            book_id=book_id,
            page_id=page_id,
            block_id=block_id,
            answer=answer,
            correct=correct,
        )
