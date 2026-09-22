"""切块单测（§7.9）：页码锚定是硬保证——混合检索的页码引用全靠它。"""

from pathlib import Path

import pymupdf

from nnnu.services.parsing.service import ParsedDocument, ParsedPage, parse_document
from nnnu.services.rag.chunking import chunk_document

MARKER = "MARKER{}END"  # 带结束词，避免 MARKER1 撞上 MARKER10 的子串


def _make_pdf(path: Path, pages: int, *, cjk: bool = False) -> None:
    doc = pymupdf.open()
    font = "china-s" if cjk else "helv"
    for index in range(1, pages + 1):
        page = doc.new_page()
        body = f"第{index}页正文。傅里叶变换把时域信号分解为频域分量之和。" * 12
        page.insert_textbox(pymupdf.Rect(60, 60, 540, 700), body, fontname=font, fontsize=9)
        page.insert_textbox(
            pymupdf.Rect(60, 720, 540, 780), MARKER.format(index), fontname=font, fontsize=9
        )
    doc.save(str(path))


def test_pdf_chunks_never_cross_pages(tmp_path):
    """200 页 PDF：块按页码单调、每块内容只来自它声称的那一页（验收 A 的页码强度）。"""
    path = tmp_path / "big.pdf"
    _make_pdf(path, 200)
    parsed = parse_document(path, "application/pdf")
    assert parsed.ok, parsed.error
    assert parsed.page_count == 200

    chunks = chunk_document(parsed, "kbdoc-big", chunk_size=400, overlap=50)
    assert chunks
    pages = [chunk.page for chunk in chunks]
    assert pages == sorted(pages)

    marked: set[int] = set()
    for chunk in chunks:
        found = {i for i in range(1, 201) if MARKER.format(i) in chunk.text}
        # 页标本身是独立一行，可能自成一块；关键是块里绝不出现别的页的标记
        assert found <= {chunk.page}, (
            f"块 {chunk.chunk_id} 声称第 {chunk.page} 页，却夹带了 {found}"
        )
        marked |= found
    assert set(pages) == set(range(1, 201))  # 200 页都切出了内容
    assert marked == set(range(1, 201))  # 200 页的页标都能在块里找到


def test_chunk_size_and_overlap(tmp_path):
    """块不超过 chunk_size；相邻块（同页）带重叠。"""
    text = "信号处理讲的是滤波与频谱分析。" * 200
    parsed = ParsedDocument(text=text)
    chunks = chunk_document(parsed, "kbdoc-size", chunk_size=300, overlap=60)
    assert len(chunks) > 3
    for chunk in chunks[:-1]:
        assert len(chunk.text) <= 300
    # 相邻块有重叠：前一块的尾巴出现在后一块里
    tail = chunks[0].text[-60:]
    assert tail[-20:] in chunks[1].text


def test_non_pdf_has_no_page(tmp_path):
    """非 PDF 无页码语义：page 全为 None（引用不带页码）。"""
    parsed = ParsedDocument(text="纯文本内容。" * 100)
    chunks = chunk_document(parsed, "kbdoc-txt", chunk_size=120)
    assert chunks
    assert all(chunk.page is None for chunk in chunks)
    assert all(chunk.doc_id == "kbdoc-txt" for chunk in chunks)


def test_empty_and_blank_document_yields_no_chunks():
    assert chunk_document(ParsedDocument(text=""), "kbdoc-empty") == []
    assert chunk_document(ParsedDocument(text="   \n\n  \t "), "kbdoc-blank") == []
    # 页全为空（扫描件无文本层）：不崩，返回空
    parsed = ParsedDocument(text="", pages=[ParsedPage(page=1, text="  ")], page_count=1)
    assert chunk_document(parsed, "kbdoc-scan") == []


def test_chunk_ids_are_unique_and_sequential():
    parsed = ParsedDocument(text="内容。" * 300)
    chunks = chunk_document(parsed, "kbdoc-seq", chunk_size=100, overlap=10)
    ids = [chunk.chunk_id for chunk in chunks]
    assert len(ids) == len(set(ids))
    assert [chunk.seq for chunk in chunks] == list(range(len(chunks)))
