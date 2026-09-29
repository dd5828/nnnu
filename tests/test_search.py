"""搜索域（§7.2）：五提供商、配置解析、web_search/paper_search/web_fetch 工具。"""

import httpx
import pytest

from nnnu.core.tool_protocol import ToolContext
from nnnu.services.search import service as search_service
from nnnu.services.search.providers import (
    BingCnProvider,
    BochaProvider,
    DuckDuckGoProvider,
    SearxNGProvider,
    SerpApiCompatProvider,
)
from nnnu.services.secrets.store import get_secrets_store
from nnnu.services.settings.service import get_settings_service
from nnnu.tools.builtin.paper_search_tool import PaperSearchTool, _query_ladder, _search_arxiv
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

BING_HTML = """
<html><body>
<li class="b_algo">
  <h2><a href="https://example.com/a">结果一标题</a></h2>
  <div class="b_caption"><p class="b_lineclamp2">这是第一条结果的摘要<a target="_blank" class="b_algoReadMore" href="https://example.com/a">阅读更多</a></p></div>
</li>
<li class="b_algo">
  <h2><a href="https://example.com/b?x=1&amp;y=2">结果<b>二</b>标题</a></h2>
  <div class="b_caption"><p class="b_lineclamp2">这是第二条结果的摘要</p></div>
</li>
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


EMPTY_ATOM = '<?xml version="1.0" encoding="UTF-8"?>\n<feed xmlns="http://www.w3.org/2005/Atom"/>\n'


def _transport(handler):
    return httpx.MockTransport(handler)


def _ctx(**args) -> ToolContext:
    return ToolContext(turn_id="turn-test", session_id="sess-test", args=dict(args))


# ---- 提供商 ----


async def test_bing_cn_parses_results():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "cn.bing.com" in str(request.url)
        return httpx.Response(200, text=BING_HTML)

    response = await BingCnProvider().search("测试", 5, transport=_transport(handler))
    assert response.error is None
    assert [hit.title for hit in response.hits] == ["结果一标题", "结果二标题"]
    assert response.hits[0].url == "https://example.com/a"
    # 标题里的标签剥掉、URL 里的实体还原
    assert response.hits[1].url == "https://example.com/b?x=1&y=2"
    assert "这是第一条结果的摘要" in response.hits[0].snippet
    assert "阅读更多" not in response.hits[0].snippet


async def test_bing_cn_truncates_to_max_results():
    response = await BingCnProvider().search(
        "测试", 1, transport=_transport(lambda r: httpx.Response(200, text=BING_HTML))
    )
    assert len(response.hits) == 1


async def test_bing_cn_http_error():
    response = await BingCnProvider().search(
        "测试", 5, transport=_transport(lambda r: httpx.Response(503))
    )
    assert response.error is not None
    assert "503" in response.error


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


async def test_service_defaults_to_bing_cn(tmp_home):
    """未配置 provider 时走免密钥默认（大陆可达的必应中文版，P3 降级项的收口）。"""
    response = await search_service.web_search(
        "测试", transport=_transport(lambda r: httpx.Response(200, text=BING_HTML))
    )
    assert response.provider == "bing_cn"
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
        "sources": [
            {
                "doc_id": "https://a.example",
                "kb": "web",
                "page": None,
                "snippet": "摘要A",
                "title": "标题A",  # §7.6：来源要能被人认出来才敢点
            }
        ]
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


async def test_paper_search_prefers_phrase_then_falls_back():
    """检索式阶梯：先按词组精确匹配，0 条再退回裸写。

    长查询裸写会被 arXiv 拆散成 OR 语义、拿回一堆噪音（实测拿一篇综述的完整标题去搜，
    第一条是毫不相干的论文），加引号才锚得住；但词组太长又会 0 命中，所以两级都要有。
    """
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        expression = request.url.params["search_query"]
        seen.append(expression)
        return httpx.Response(200, text=EMPTY_ATOM if 'all:"' in expression else ARXIV_ATOM)

    papers = await _search_arxiv("RAG evaluation benchmark", 3, transport=_transport(handler))
    assert seen == ['all:"RAG evaluation benchmark"', "all:RAG evaluation benchmark"]
    assert len(papers) == 1  # 短语落空不影响最终结果：宽松那轮的结果照常返回


async def test_paper_search_phrase_hit_takes_one_request():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params["search_query"])
        return httpx.Response(200, text=ARXIV_ATOM)

    papers = await _search_arxiv("RAG survey", 3, transport=_transport(handler))
    assert seen == ['all:"RAG survey"']  # 短语就命中，不多跑一趟
    assert len(papers) == 1


async def test_paper_search_single_token_skips_phrase():
    """单个词没有「词组」可言：直接用裸写，省一次往返。"""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params["search_query"])
        return httpx.Response(200, text=ARXIV_ATOM)

    await _search_arxiv("RAG", 3, transport=_transport(handler))
    assert seen == ["all:RAG"]


def test_query_ladder_cleans_quotes_and_spaces():
    """查询自带的引号会截断 arXiv 语法，先清掉；空白压平后只剩一个词就当单词查询。"""
    assert _query_ladder('agentic "RAG"   2025') == [
        'all:"agentic RAG 2025"',
        "all:agentic RAG 2025",
    ]
    assert _query_ladder(' "RAG" ') == ["all:RAG"]
    assert _query_ladder("") == ["all:"]


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
    source = result.detail["sources"][0]
    assert source["kb"] == "arxiv"
    assert source["title"] == "论文一"  # §7.6：引用面板要靠它给人看，不然只有一串 arxiv 链接


async def test_web_fetch_rejects_private_hosts():
    assert _validate_url("http://127.0.0.1/admin") is not None
    assert _validate_url("http://localhost/x") is not None
    assert _validate_url("http://192.168.1.1/x") is not None
    assert _validate_url("ftp://example.com") is not None
    assert _validate_url("not-a-url") is not None


async def test_web_fetch_tool_fetches(tmp_home, monkeypatch):
    class FakeResult:
        text_content = "# 抓到的内容\n正文段落"
        title = "示例页"

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
    # 抓下来的页面本身就是一份可验证的来源，不能再像以前那样只回 detail.url
    source = result.detail["sources"][0]
    assert source["doc_id"] == "https://example.com/page"
    assert source["kb"] == "web"
    assert source["title"] == "示例页"
    assert source["snippet"].startswith("# 抓到的内容")


async def test_web_fetch_source_title_falls_back_to_host(tmp_home, monkeypatch):
    class FakeResult:
        text_content = "正文"  # 没有 title 属性：取不到就退回域名
        title = None

    class FakeMarkItDown:
        def __init__(self, *args, **kwargs):
            pass

        def convert(self, source):
            return FakeResult()

    monkeypatch.setattr("markitdown.MarkItDown", FakeMarkItDown)
    result = await WebFetchTool().run(_ctx(url="https://example.com/x", max_chars=5000))
    assert result.detail["sources"][0]["title"] == "example.com"


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
