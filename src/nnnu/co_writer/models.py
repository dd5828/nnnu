"""Co-Writer 文档模型与纯函数（§7.13）。

正文**不进 SQLite**：co_writer_docs 表（schema v13）只放元数据，正文落在
data/user/co_writer/<doc_id>.md（读写见 storage.py）。本模块管与存储无关的
纯逻辑：id 形状、上限常量、digest、切片校验、标题/预览、模型输出清洗。
"""

import hashlib
import re
import time
from typing import Literal

from pydantic import BaseModel, Field

from nnnu.core.ids import new_id

# 六档动作白名单：与前端 web/lib/co-writer.ts、API 请求体三处同表
Action = Literal["rewrite", "expand", "shorten", "translate", "tone", "free"]
ACTIONS: tuple[str, ...] = ("rewrite", "expand", "shorten", "translate", "tone", "free")

MAX_CONTENT_CHARS = 200_000  # 一篇长文的上限（与笔记本记录同量级，防手滑贴整本书）
MAX_TITLE_CHARS = 120
MAX_SELECTION_CHARS = 20_000  # 单次改写的选区上限：再大就该拆段改了（还防爆上下文）
MAX_INSTRUCTION_CHARS = 500
CONTEXT_CHARS = 400  # 选中段前后各带多少字进提示词（只帮模型理解语境）
PREVIEW_CHARS = 80  # 列表卡片上的预览长度
PREVIEW_HEAD_CHARS = 2000  # 列表只读文件头这么多字符来算预览（不整篇读盘）

DOC_ID_RE = re.compile(r"^cw-[0-9a-f]{8}$")  # new_id("cw") 的形状；防路径穿越

# 取正文首行当标题时剥掉的 markdown 装饰（与 notebooks/service.py 同规则）
_TITLE_STRIP_CHARS = "#*-— \t"

# 模型爱把整段改写结果包在引号里；成对出现才剥（见 clean_model_output）
_QUOTE_PAIRS = (('"', '"'), ("'", "'"), ("“", "”"), ("「", "」"), ("『", "』"))


class CoWriterError(ValueError):
    """Co-Writer 参数非法。code 供路由层映射到 §9.1 错误码（invalid_* / doc_changed）。"""

    def __init__(self, message: str, *, code: str = "invalid_request") -> None:
        super().__init__(message)
        self.code = code


class CoWriterDoc(BaseModel):
    """文档元数据（正文在文件里；列表/重命名只碰这张表）。"""

    id: str
    title: str = ""
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

    @classmethod
    def new(cls, **kwargs) -> "CoWriterDoc":
        return cls(id=new_id("cw"), **kwargs)


def is_valid_doc_id(doc_id: str) -> bool:
    """doc_id 只认 cw-xxxxxxxx 形状：它直接拼进文件路径，先卡死形状再谈穿越。"""
    return bool(DOC_ID_RE.match(doc_id))


def content_digest(text: str) -> str:
    """正文摘要：accept 前比对，防「确认的是旧版本」的静默覆盖。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def validate_content(content: str) -> str:
    """正文校验（空允许：刚建的文档就是空的）。超限抛 invalid_content。"""
    if len(content) > MAX_CONTENT_CHARS:
        raise CoWriterError(f"正文最多 {MAX_CONTENT_CHARS} 个字符", code="invalid_content")
    return content


def _first_line(text: str, limit: int, fallback: str) -> str:
    """首个非空行（剥标题符号）；超长截断加省略号。"""
    for line in text.splitlines():
        stripped = line.strip().strip(_TITLE_STRIP_CHARS)
        if stripped:
            if len(stripped) > limit:
                return stripped[: limit - 1] + "…"
            return stripped
    return fallback


def title_of(raw: str | None, content: str) -> str:
    """文档标题：显式给了就用（截断），没给就取正文首行。"""
    text = (raw or "").strip().strip(_TITLE_STRIP_CHARS)
    return (text or _first_line(content, MAX_TITLE_CHARS, "未命名文档"))[:MAX_TITLE_CHARS]


def preview_of(content: str) -> str:
    """列表卡片预览：正文首行摘要；空文档给空串（前端显示空态）。"""
    return _first_line(content, PREVIEW_CHARS, "")


def validate_selection(content: str, start: int, end: int, original: str) -> None:
    """改写前的切片校验：范围形状 → invalid_selection；与现文对不上 → doc_changed。

    对不上通常意味着自动保存把前后端之间的正文写新了（或用户切走了又改了文档），
    调用方按 409 处理，让用户重新选。
    """
    if start < 0 or end <= start or end > len(content) or not original:
        raise CoWriterError("选中的范围无效", code="invalid_selection")
    if end - start > MAX_SELECTION_CHARS:
        raise CoWriterError(
            f"单次最多改写 {MAX_SELECTION_CHARS} 个字符，请拆成几段", code="invalid_selection"
        )
    if content[start:end] != original:
        raise CoWriterError("文档内容已变动，请重新选择要改写的文字", code="doc_changed")


def validate_action(action: str) -> str:
    if action not in ACTIONS:
        raise CoWriterError(
            f"未知动作 {action!r}，允许：{'、'.join(ACTIONS)}", code="invalid_action"
        )
    return action


def validate_instruction(action: str, instruction: str) -> str:
    """指令校验：free 必须写指令；一律限长。"""
    text = (instruction or "").strip()
    if len(text) > MAX_INSTRUCTION_CHARS:
        raise CoWriterError(f"指令最多 {MAX_INSTRUCTION_CHARS} 个字符", code="invalid_action")
    if action == "free" and not text:
        raise CoWriterError("自由修改要写明改动要求", code="invalid_action")
    return text


def clean_model_output(text: str, *, original: str = "") -> str:
    """清洗模型输出：剥整段围栏、剥首尾成对引号（只在成对时才动手）。

    原文自己就带围栏/引号时（模型照抄了包装）不剥，免得把内容本身切掉。
    """
    cleaned = (text or "").strip()
    cleaned = _strip_fence(cleaned, original.strip())
    cleaned = _strip_quotes(cleaned, original.strip())
    return cleaned


def _strip_fence(text: str, original: str) -> str:
    if original.startswith("```") and original.endswith("```"):
        return text
    lines = text.splitlines()
    if len(lines) >= 2 and lines[0].lstrip().startswith("```") and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1]).strip()
    if len(lines) == 1 and text.startswith("```") and text.endswith("```") and len(text) > 6:
        return text[3:-3].strip()
    return text


def _strip_quotes(text: str, original: str) -> str:
    for left, right in _QUOTE_PAIRS:
        if len(text) > len(left) + len(right) and text.startswith(left) and text.endswith(right):
            if original.startswith(left) and original.endswith(right):
                return text  # 原文本来就带这对引号，模型是照抄，不是包装
            inner = text[len(left) : len(text) - len(right)].strip()
            return inner or text
    return text
