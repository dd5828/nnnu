"""引用编号重排（§7.6）：纯函数层，不碰 LLM、不碰库。

这是「报告里每条引用都能点开验证」的地基：模型只写 `[CIT-<k>-<nn>]`，全局号由
`CitationBook` 说了算，对不上任何真实来源的记号直接剥掉。
"""

from nnnu.services.research.citations import (
    MARKER_RE,
    SNIPPET_MAX_CHARS,
    CitationBook,
    _clean_url,
)


def _source(doc_id: str, *, title: str = "标题", snippet: str = "摘要", page: int | None = None):
    source = {"doc_id": doc_id, "title": title, "snippet": snippet}
    if page is not None:
        source["page"] = page
    return source


A = _source("https://example.com/a", title="来源 A", snippet="A 的摘要")
B = _source("https://example.com/b", title="来源 B", snippet="B 的摘要")
C = _source("https://example.com/c", title="来源 C", snippet="C 的摘要")


def test_numbers_are_issued_on_first_citation():
    """登记不发号：正文引用到哪条才发哪个号，顺序就是引用顺序。"""
    book = CitationBook()
    book.add_subtopic(1, [A, B])
    book.add_subtopic(2, [B, C])

    assert book.used == []  # 光登记不算引用
    text = "丙 [CIT-2-2]，甲 [CIT-1-1]，乙 [CIT-2-1]，又见甲 [CIT-1-1]。"
    renumbered, dropped = book.renumber(text)

    # C 先被引用 → 1 号；A → 2 号；B 跨子问题两个记号（CIT-1-2 没写，用 CIT-2-1）→ 复用 3 号
    assert renumbered == "丙 [1]，甲 [2]，乙 [3]，又见甲 [2]。"
    assert dropped == 0
    assert [item["doc_id"] for item in book.used] == [
        "https://example.com/c",
        "https://example.com/a",
        "https://example.com/b",
    ]


def test_uncited_sources_are_dropped_from_the_list():
    """没被引用的来源不发号、不进清单（搜索结果里的噪音页就是这么被挡在参考资料外的）。"""
    book = CitationBook()
    book.add_subtopic(1, [A, B, C])
    book.renumber("只引一条 [CIT-1-2]。")

    assert [item["doc_id"] for item in book.used] == ["https://example.com/b"]
    lines = book.references_lines(
        lambda **kwargs: "{index}. [{title}]({url})".format(**kwargs), heading="## 参考资料"
    )
    assert lines[2] == "1. [来源 B](https://example.com/b)"


def test_page_distinguishes_same_document():
    """同一份 PDF 的不同页是两个来源，不能并成一个号。"""
    book = CitationBook()
    book.add_subtopic(1, [_source("kb-1", page=3), _source("kb-1", page=9)])
    renumbered, _ = book.renumber("[CIT-1-1] 和 [CIT-1-2]")
    assert renumbered == "[1] 和 [2]"
    assert book.source_at(1, 0) is None and book.source_at(1, 3) is None


def test_renumber_replaces_and_counts_dropped():
    book = CitationBook()
    book.add_subtopic(1, [A, B])
    book.add_subtopic(2, [B, C])

    text = "甲说 [CIT-1-1]，乙说 [CIT-1-2]，丙说 [CIT-2-2]，丁说 [CIT-9-9]。"
    renumbered, dropped = book.renumber(text)

    assert renumbered == "甲说 [1]，乙说 [2]，丙说 [3]，丁说 。"
    assert dropped == 1  # 凭空编号的那个被剥掉并计数


def test_renumber_without_markers_is_noop():
    book = CitationBook()
    book.add_subtopic(1, [A])
    assert book.renumber("一段没有记号的正文。") == ("一段没有记号的正文。", 0)
    assert book.used == []  # 剥号不等于引用：一个字都没引就没有清单


def test_references_lines_renders_markdown_list():
    book = CitationBook()
    book.add_subtopic(1, [A, B])
    book.renumber("[CIT-1-1][CIT-1-2]")

    lines = book.references_lines(
        lambda **kwargs: "{index}. [{title}]({url}) — {snippet}".format(**kwargs),
        heading="## 参考资料",
    )

    assert lines[0] == "## 参考资料"
    assert lines[1] == ""
    assert lines[2] == "1. [来源 A](https://example.com/a) — A 的摘要"
    assert lines[3] == "2. [来源 B](https://example.com/b) — B 的摘要"


def test_references_lines_empty_book_is_empty():
    assert CitationBook().references_lines(lambda **k: "", heading="## 参考资料") == []


def test_references_lines_skips_non_http_sources():
    """知识库/附件来源没有可点开的 URL：不进清单（正文里的 [n] 仍在，那是另一回事）。"""
    book = CitationBook()
    book.add_subtopic(1, [_source("kb-1", title="教材", page=3), C])
    book.add_subtopic(2, [_source("附件.pdf", title="附件")])
    book.renumber("[CIT-1-1][CIT-1-2][CIT-2-1]")

    lines = book.references_lines(
        lambda **kwargs: "{index}. [{title}]({url}) — {snippet}".format(**kwargs),
        heading="## 参考资料",
    )

    assert len(lines) == 3  # 表头 + 空行 + 唯一一条带 http URL 的
    assert "https://example.com/c" in lines[2]
    assert "教材" not in "\n".join(lines)
    # 号是引用顺序、不能为了凑连号重排：教材先被引（1 号）、C 第二（2 号），清单就从 2 起
    assert lines[2].startswith("2. ")


def test_references_lines_without_clickable_source_is_empty():
    """引的全是知识库/附件：一条能点的都没有，就不挂空标题。"""
    book = CitationBook()
    book.add_subtopic(1, [_source("kb-1", title="教材", page=3)])
    book.renumber("[CIT-1-1]")
    assert book.references_lines(lambda **k: "", heading="## 参考资料") == []


def test_references_lines_cleans_title_and_snippet():
    book = CitationBook()
    long_snippet = "长" * (SNIPPET_MAX_CHARS + 50)
    book.add_subtopic(
        1, [_source("https://example.com/x", title="教材[上册]", snippet=long_snippet)]
    )
    book.renumber("[CIT-1-1]")
    lines = book.references_lines(
        lambda **kwargs: "{title}|{snippet}".format(**kwargs), heading="## 参考资料"
    )
    # 方括号会截断 markdown 链接语法，必须换掉；摘要超长截断加省略号
    assert lines[2].startswith("教材（上册）|长")
    assert lines[2].endswith("…")
    assert len(lines[2]) < SNIPPET_MAX_CHARS + 20


def test_clean_url_rejects_non_http_and_escapes_parens():
    assert _clean_url("ftp://example.com/a") == ""
    assert _clean_url("kb-1") == ""
    assert _clean_url("  https://example.com/a(b)  ") == "https://example.com/a%28b%29"


def test_marker_regex_ignores_malformed_tokens():
    """只看 `[CIT-数字-数字]`；模型写成别的样子就当普通文本，别误伤。"""
    assert MARKER_RE.findall("[CIT-1-2]") == [("1", "2")]
    assert MARKER_RE.findall("[cit-1-2]") == []
    assert MARKER_RE.findall("[CIT-a-b]") == []
    assert MARKER_RE.findall("[1]") == []
