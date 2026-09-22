"""web_search 提供商（§7.2）：≥3 可配置 + 免密钥默认。

adapted from DeepTutor (Apache-2.0) deeptutor/services/search/providers/：
- DuckDuckGo 免密钥默认（lite 端点 HTML 解析，零依赖）；
- SearXNG（自建实例，format=json）；
- Bocha（博查，POST /v1/web-search）；
- serpapi_compat（任意 SerpAPI 兼容端点 GET search.json）。

全部 httpx 异步；transport 参数供测试注入（§12.1 同法）。
"""

import html
import re
from typing import Protocol

import httpx

from nnnu.services.search.types import SearchHit, SearchResponse

SEARCH_TIMEOUT_S = 15.0

# DDG lite 页面结构：<a rel="nofollow" class="result-link">标题</a> + <td class="result-snippet">摘要</td>
_DDG_LINK_RE = re.compile(r'<a[^>]*class="result-link"[^>]*>(.*?)</a>', re.S)
_DDG_SNIPPET_RE = re.compile(r'<td[^>]*class="result-snippet"[^>]*>(.*?)</td>', re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_tags(text: str) -> str:
    return html.unescape(_TAG_RE.sub("", text)).strip()


class SearchProvider(Protocol):
    """提供商统一接口：search(query, max_results, transport) → SearchResponse。"""

    async def search(
        self,
        query: str,
        max_results: int,
        *,
        api_key: str | None,
        base_url: str | None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> SearchResponse: ...


class DuckDuckGoProvider:
    """免密钥默认：lite 端点 HTML 解析（无第三方依赖，测试注入 MockTransport）。"""

    BASE_URL = "https://lite.duckduckgo.com/lite/"

    async def search(
        self,
        query: str,
        max_results: int,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> SearchResponse:
        url = base_url or self.BASE_URL
        headers = {"User-Agent": "Mozilla/5.0 (nnnu search tool)"}
        async with httpx.AsyncClient(timeout=SEARCH_TIMEOUT_S, transport=transport) as client:
            try:
                response = await client.get(url, params={"q": query}, headers=headers)
            except httpx.HTTPError as exc:
                return SearchResponse(
                    query=query,
                    provider="duckduckgo",
                    error=f"搜索请求失败：{exc.__class__.__name__}",
                )
            if response.status_code != 200:
                return SearchResponse(
                    query=query,
                    provider="duckduckgo",
                    error=f"搜索引擎返回 HTTP {response.status_code}",
                )
        titles = [_strip_tags(m) for m in _DDG_LINK_RE.findall(response.text)]
        snippets = [_strip_tags(m) for m in _DDG_SNIPPET_RE.findall(response.text)]
        hits: list[SearchHit] = []
        for index, title in enumerate(titles[:max_results]):
            hits.append(
                SearchHit(
                    title=title,
                    url=f"https://duckduckgo.com/lite/?q={query}",  # lite 页无直接链接，命中以摘要为准
                    snippet=snippets[index] if index < len(snippets) else "",
                    source="DuckDuckGo",
                )
            )
        return SearchResponse(query=query, provider="duckduckgo", hits=hits)


class SearxNGProvider:
    """自建 SearXNG 实例：GET /search?q=&format=json，免密钥。"""

    async def search(
        self,
        query: str,
        max_results: int,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> SearchResponse:
        if not base_url:
            return SearchResponse(
                query=query,
                provider="searxng",
                error="SearXNG 需要设置实例地址（设置页搜索 base_url）",
            )
        url = base_url.rstrip("/") + "/search"
        async with httpx.AsyncClient(timeout=SEARCH_TIMEOUT_S, transport=transport) as client:
            try:
                response = await client.get(url, params={"q": query, "format": "json"})
            except httpx.HTTPError as exc:
                return SearchResponse(
                    query=query, provider="searxng", error=f"搜索请求失败：{exc.__class__.__name__}"
                )
            if response.status_code != 200:
                return SearchResponse(
                    query=query,
                    provider="searxng",
                    error=f"SearXNG 返回 HTTP {response.status_code}",
                )
            try:
                payload = response.json()
            except ValueError:
                return SearchResponse(
                    query=query, provider="searxng", error="SearXNG 响应不是 JSON"
                )
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            return SearchResponse(query=query, provider="searxng", hits=[])
        hits = [
            SearchHit(
                title=str(row.get("title", "")),
                url=str(row.get("url", "")),
                snippet=str(row.get("content", ""))[:500],
                content=str(row.get("content", "")),
                source="SearXNG",
            )
            for row in results[:max_results]
            if isinstance(row, dict)
        ]
        return SearchResponse(query=query, provider="searxng", hits=hits)


class BochaProvider:
    """博查：POST /v1/web-search（Bearer 密钥）。"""

    BASE_URL = "https://api.bochaai.com/v1/web-search"

    async def search(
        self,
        query: str,
        max_results: int,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> SearchResponse:
        if not api_key:
            return SearchResponse(
                query=query, provider="bocha", error="博查需要 API 密钥（设置页填写）"
            )
        url = base_url or self.BASE_URL
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        body = {
            "query": query,
            "summary": True,
            "freshness": "noLimit",
            "count": min(max(1, max_results), 10),
        }
        async with httpx.AsyncClient(timeout=SEARCH_TIMEOUT_S, transport=transport) as client:
            try:
                response = await client.post(url, headers=headers, json=body)
            except httpx.HTTPError as exc:
                return SearchResponse(
                    query=query, provider="bocha", error=f"搜索请求失败：{exc.__class__.__name__}"
                )
            if response.status_code != 200:
                return SearchResponse(
                    query=query, provider="bocha", error=f"博查返回 HTTP {response.status_code}"
                )
            try:
                payload = response.json()
            except ValueError:
                return SearchResponse(query=query, provider="bocha", error="博查响应不是 JSON")
        if payload.get("code") not in (200, 0, None):
            return SearchResponse(
                query=query, provider="bocha", error=f"博查错误：{payload.get('msg') or payload}"
            )
        rows = ((payload.get("data") or {}).get("webPages") or {}).get("value") or []
        hits = [
            SearchHit(
                title=str(row.get("name", "")),
                url=str(row.get("url", "")),
                snippet=str(row.get("snippet", "")),
                content=str(row.get("summary", "") or ""),
                date=str(row.get("datePublished", "") or ""),
                source=str(row.get("siteName", "") or "博查"),
            )
            for row in rows[:max_results]
            if isinstance(row, dict)
        ]
        return SearchResponse(query=query, provider="bocha", hits=hits)


class SerpApiCompatProvider:
    """任意 SerpAPI 兼容端点：GET search.json?q=&api_key=。"""

    BASE_URL = "https://serpapi.com/search.json"

    async def search(
        self,
        query: str,
        max_results: int,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> SearchResponse:
        if not api_key:
            return SearchResponse(
                query=query, provider="serpapi_compat", error="该提供商需要 API 密钥（设置页填写）"
            )
        url = base_url or self.BASE_URL
        async with httpx.AsyncClient(timeout=SEARCH_TIMEOUT_S, transport=transport) as client:
            try:
                response = await client.get(
                    url, params={"q": query, "api_key": api_key, "num": max_results}
                )
            except httpx.HTTPError as exc:
                return SearchResponse(
                    query=query,
                    provider="serpapi_compat",
                    error=f"搜索请求失败：{exc.__class__.__name__}",
                )
            if response.status_code != 200:
                return SearchResponse(
                    query=query,
                    provider="serpapi_compat",
                    error=f"搜索端点返回 HTTP {response.status_code}",
                )
            try:
                payload = response.json()
            except ValueError:
                return SearchResponse(
                    query=query, provider="serpapi_compat", error="搜索端点响应不是 JSON"
                )
        rows = payload.get("organic_results") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return SearchResponse(query=query, provider="serpapi_compat", hits=[])
        hits = [
            SearchHit(
                title=str(row.get("title", "")),
                url=str(row.get("link", "")),
                snippet=str(row.get("snippet", ""))[:500],
                source=str(row.get("source", "") or "SerpAPI"),
            )
            for row in rows[:max_results]
            if isinstance(row, dict)
        ]
        return SearchResponse(query=query, provider="serpapi_compat", hits=hits)


PROVIDERS: dict[str, SearchProvider] = {
    "duckduckgo": DuckDuckGoProvider(),
    "searxng": SearxNGProvider(),
    "bocha": BochaProvider(),
    "serpapi_compat": SerpApiCompatProvider(),
}

DEFAULT_PROVIDER = "duckduckgo"
