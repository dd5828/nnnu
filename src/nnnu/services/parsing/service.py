"""文档解析（§4/§7.1 附件清单）：P2 最小版，P4 KB 复用扩展。

引擎选择（MIME → 引擎）：
- PDF → PyMuPDF 逐页提取（页级引用定位页码）；
- DOCX/XLSX/PPTX → markitdown 转 Markdown（无页码语义）；
- TXT/MD/CSV/代码 → 直读（utf-8 → gbk 回退 → replace 兜底）；
- 图片 → kind=image，无文本（视觉模型读图，见 chat 能力注入）。

解析统一为 ParsedDocument；PDF 之外 pages 为空、text 为全文。
同步函数由调用方经 asyncio.to_thread 调度（解析可能耗时数秒）。
"""

import logging
from pathlib import Path

import fitz  # PyMuPDF：PDF 页文本提取
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class ParsedPage(BaseModel):
    page: int  # 从 1 起
    text: str


class ParsedDocument(BaseModel):
    kind: str = "text"  # text | image
    text: str = ""  # 全文（PDF 时为全页拼接；图片为空）
    pages: list[ParsedPage] = Field(default_factory=list)
    page_count: int = 0
    ok: bool = True
    error: str | None = None


def parse_document(path: Path, mime: str) -> ParsedDocument:
    """按 MIME 选择引擎解析；异常统一转为 ok=False 结果（解析失败不致命）。"""
    _warn_unsupported_engine()
    try:
        if mime == "application/pdf":
            return _parse_pdf(path)
        if mime in _OFFICE_MIMES:
            return _parse_markitdown(path)
        if mime.startswith("image/"):
            return ParsedDocument(kind="image")
        return _parse_plain(path)
    except Exception as exc:  # 解析失败：上传仍成功，缓存标 error（§7.1 解析缓存清理入口）
        return ParsedDocument(ok=False, error=f"{type(exc).__name__}: {exc}")


def _warn_unsupported_engine() -> None:
    """kb.parse_engine 目前只有「自动」（按 MIME 选引擎）这一条路。

    设置卡里留着这个下拉是为了 P14 接多引擎，选别的值不报错、按自动走，
    但明确告警——免得用户以为换了引擎（尤其 PDF：markitdown 转出来没有页码，
    而页码是引用的地基）。
    """
    from nnnu.services.settings.service import get_settings_service

    try:
        engine = str(get_settings_service().load_area("kb").get("parse_engine") or "")
    except (KeyError, TypeError):
        return
    if engine:
        logger.warning("kb.parse_engine=%s 暂未实现，按「自动」处理（P14 接多引擎）", engine)


_OFFICE_MIMES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


def _parse_pdf(path: Path) -> ParsedDocument:
    pages: list[ParsedPage] = []
    with fitz.open(path) as doc:
        for index, page in enumerate(doc, start=1):
            text = page.get_text("text").strip()
            pages.append(ParsedPage(page=index, text=text))
    text = "\n\n".join(p.text for p in pages)
    return ParsedDocument(text=text, pages=pages, page_count=len(pages))


def _parse_markitdown(path: Path) -> ParsedDocument:
    from markitdown import MarkItDown

    result = MarkItDown().convert(str(path))
    return ParsedDocument(text=result.text_content.strip())


def _parse_plain(path: Path) -> ParsedDocument:
    raw = path.read_bytes()
    for encoding in ("utf-8", "gbk"):
        try:
            return ParsedDocument(text=raw.decode(encoding).strip())
        except UnicodeDecodeError:
            continue
    return ParsedDocument(text=raw.decode("utf-8", errors="replace").strip())
