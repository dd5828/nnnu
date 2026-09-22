"""搜索域（§7.2）：四提供商、配置解析、web_search/paper_search/web_fetch 工具。"""

import httpx
import pytest

from nnnu.core.tool_protocol import ToolContext
from nnnu.services.search import service as search_service
from nnnu.services.search.providers import (
    BochaProvider,
    DuckDuckGoProvider,
    SearxNGProvider,
    SerpApiCompatProvider,
)
from nnnu.services.secrets.store import get_secrets_store
from nnnu.services.settings.service import get_settings_service
from nnnu.tools.builtin.paper_search_tool import PaperSearchTool, _search_arxiv
from nnnu.tools.builtin.web_fetch import WebFetchTool, _validate_url
from nnnu.tools.builtin.web_search import WebSearchTool

DDG_HTML = """
<html><body>
<a rel="nofollow" class="result-link" href="https://example.com/a">结果一</a>
<td class="result-snippet">这是第一条结果的摘要</td>
<a rel="nofollow" class="result-link" href="https://example.com/b">结果二</a>
<td class="result-snippet">这是第二条结果的摘要</td>
</body></html>
"""

ARXIV_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2301.00001v1</id>
    <published>2023-01-05T00:00:00Z</published>
    <title>RAG Survey 2023</title>
    <summary>A comprehensive survey of retrieval augmented generation.</summary>
    <author><name>Alice</name></author>
    <author><name>Bob</name></author>
  </entry>
</feed>
"""


def _transport(handler):
    return httpx.MockTransport(handler)


def _ctx(**args) -> ToolContext:
    return ToolContext(turn_id="turn-test", session_id="sess-test", args=dict(args))


# ---- 提供商 ----


async def test_duckduckgo_parses_lite_html():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "lite.duckduckgo.com" in str(request.url)
        return httpx.Response(200, text=DDG_HTML)

    response = await DuckDuckGoProvider().search("测试", 5, transport=_transport(handler))
    assert response.error is None
    assert [hit.title for hit in response.hits] == ["结果一", "结果二"]
    assert "第一条结果" in response.hits[0].snippet


async def test_duckduckgo_http_error():
    response = await DuckDuckGoProvider().search(
        "测试", 5, transport=_transport(lambda r: httpx.Response(500))
    )
    assert response.error is not None
    assert "500" in response.error


async def test_searxng_requires_base_url():
    response = await SearxNGProvider().search("测试", 5)
    assert response.error is not None
    assert "实例地址" in response.error


async def test_searxng_parses_results():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/search")
        return httpx.Response(
            200, json={"results": [{"title": "t1", "url": "https://a", "content": "c1"}]}
        )

    response = await SearxNGProvider().search(
        "测试", 5, base_url="https://searx.example.com", transport=_transport(handler)
    )
    assert response.error is None
    assert response.hits[0].title == "t1"


async def test_bocha_requires_key():
    response = await BochaProvider().search("测试", 5)
    assert response.error is not None
    assert "API 密钥" in response.error


async def test_bocha_parses_webpages():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer sk-bocha"
        return httpx.Response(
            200,
            json={
                "code": 200,
                "data": {
                    "webPages": {
                        "value": [
                            {
                                "name": "博查结果",
                                "url": "https://b",
                                "snippet": "s",
                                "summary": "长摘要",
                            }
                        ]
                    }
                },
            },
        )

    response = await BochaProvider().search(
        "测试", 5, api_key="sk-bocha", transport=_transport(handler)
    )
    assert response.error is None
    assert response.hits[0].title == "博查结果"
    assert response.hits[0].content == "长摘要"


async def test_serpapi_compat_parses_organic():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "serpapi.com" in str(request.url)
        return httpx.Response(
            200, json={"organic_results": [{"title": "o1", "link": "https://o", "snippet": "os"}]}
        )

    response = await SerpApiCompatProvider().search(
        "测试", 5, api_key="sk-serp", transport=_transport(handler)
    )
    assert response.hits[0].title == "o1"


# ---- 配置解析（settings + secrets） ----


async def test_service_uses_configured_provider_and_key(tmp_home):
    get_settings_service().save_area("models", {"search_provider": "bocha"})
    store = get_secrets_store()
    store.set_pending("search", "bocha", "sk-from-settings")
    store.promote("search", "bocha")

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("Authorization")
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"code": 200, "data": {"webPages": {"value": []}}})

    response = await search_service.web_search("测试", transport=_transport(handler))
    assert response.provider == "bocha"
    assert captured["auth"] == "Bearer sk-from-settings"
    assert "bochaai.com" in captured["url"]


async def test_service_defaults_to_duckduckgo(tmp_home):
    response = await search_service.web_search(
        "测试", transport=_transport(lambda r: httpx.Response(200, text=DDG_HTML))
    )
    assert response.provider == "duckduckgo"
    assert len(response.hits) == 2


# ---- 工具层 ----


async def test_web_search_tool_output_and_sources(tmp_home):
    tool = WebSearchTool()
    # 用 monkeypatch 换掉服务函数，验证工具层的格式化与 citation 结构
    original = search_service.web_search

    from nnnu.services.search.types import SearchHit, SearchResponse

    async def fake_search(query, max_results=5, *, transport=None):
        return SearchResponse(
            query=query,
            provider="duckduckgo",
            hits=[SearchHit(title="标题A", url="https://a.example", snippet="摘要A")],
        )

    search_service.web_search = fake_search  # type: ignore[method-assign]
    try:
        result = await tool.run(_ctx(query="傅里叶变换"))
    finally:
        search_service.web_search = original
    assert result.ok is True
    assert "标题A" in result.output
    assert "https://a.example" in result.output
    assert result.detail == {
        "sources": [{"doc_id": "https://a.example", "kb": "web", "page": None, "snippet": "摘要A"}]
    }


async def test_paper_search_parses_atom():
    papers = await _search_arxiv(
        "RAG", 3, transport=_transport(lambda r: httpx.Response(200, text=ARXIV_ATOM))
    )
    assert len(papers) == 1
    paper = papers[0]
    assert paper["title"] == "RAG Survey 2023"
    assert paper["arxiv_id"] == "2301.00001"
    assert paper["url"] == "https://arxiv.org/abs/2301.00001"
    assert paper["authors"] == ["Alice", "Bob"]
    assert paper["year"] == "2023"


async def test_paper_search_tool_sources():
    tool = PaperSearchTool()
    import nnnu.tools.builtin.paper_search_tool as module

    original = module._search_arxiv

    async def fake(query, max_results, *, sort_by="relevance", years_limit=None, transport=None):
        return [
            {
                "title": "论文一",
                "authors": ["张三"],
                "year": "2025",
                "abstract": "研究摘要",
                "url": "https://arxiv.org/abs/2501.00001",
                "arxiv_id": "2501.00001",
                "published": "2025-01-01T00:00:00Z",
            }
        ]

    module._search_arxiv = fake  # type: ignore[attr-defined]
    try:
        result = await tool.run(_ctx(query="RAG", max_results=3))
    finally:
        module._search_arxiv = original
    assert result.ok is True
    assert "论文一" in result.output
    assert result.detail["sources"][0]["kb"] == "arxiv"


async def test_web_fetch_rejects_private_hosts():
    assert _validate_url("http://127.0.0.1/admin") is not None
    assert _validate_url("http://localhost/x") is not None
    assert _validate_url("http://192.168.1.1/x") is not None
    assert _validate_url("ftp://example.com") is not None
    assert _validate_url("not-a-url") is not None


async def test_web_fetch_tool_fetches(tmp_home, monkeypatch):
    class FakeResult:
        text_content = "# 抓到的内容\n正文段落"

    class FakeMarkItDown:
        def __init__(self, *args, **kwargs):
            pass

        def convert(self, source):
            assert source == "https://example.com/page"
            return FakeResult()

    monkeypatch.setattr("markitdown.MarkItDown", FakeMarkItDown)
    tool = WebFetchTool()
    result = await tool.run(_ctx(url="https://example.com/page", max_chars=5000))
    assert result.ok is True
    assert "抓到的内容" in result.output


# ---- 搜索密钥草稿流（域 "search" 分槽） ----


async def test_search_secret_draft_flow(tmp_home):
    svc = get_settings_service()
    svc.save_draft("models", {"search_provider": "bocha", "search_api_key": "sk-bocha-12345678"})
    draft = svc.load_draft("models")
    assert draft["search_api_key"] == "set"
    store = get_secrets_store()
    # 与 LLM 密钥分域分槽，互不串
    assert store.get_pending("search", "bocha") == "sk-bocha-12345678"
    assert store.get_pending("llm", "bocha") is None

    async def ok_probe(candidate):
        return None

    await svc.apply_draft("models", probe_fn=ok_probe)
    assert store.get("search", "bocha") == "sk-bocha-12345678"
