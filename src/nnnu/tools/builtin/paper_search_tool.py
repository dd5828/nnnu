"""paper_search 工具（§7.2）：arXiv 检索。

adapted from DeepTutor (Apache-2.0) deeptutor/tools/paper_search_tool.py ——
保留其检索语义（关键词/标题/作者 + 年份窗口 + 排序），实现改为 httpx 直连
arXiv Atom API + stdlib XML 解析（原版依赖 arxiv 第三方包，本机磁盘限制不新增依赖）。
"""

import re
import xml.etree.ElementTree as ET

import httpx

from nnnu.core.tool_protocol import BaseTool, ToolContext, ToolDefinition, ToolMount, ToolResult

ARXIV_API_URL = "https://export.arxiv.org/api/query"
TIMEOUT_S = 20.0
ATOM = "{http://www.w3.org/2005/Atom}"

ABSTRACT_MAX_CHARS = 600


def _build_query(query: str) -> str:
    """按关键词搜索（标题/作者/摘要均命中），原版 arxiv.Search 默认语义。"""
    return f"all:{query}"


async def _search_arxiv(
    query: str,
    max_results: int,
    *,
    sort_by: str = "relevance",
    years_limit: int | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[dict]:
    """arXiv Atom API 检索；返回 [{title, authors, year, abstract, url, arxiv_id, published}]。"""
    params: dict = {
        "search_query": _build_query(query),
        "max_results": str(max(1, min(int(max_results), 20))),
        "sortBy": "submittedDate" if sort_by == "date" else "relevance",
    }
    async with httpx.AsyncClient(timeout=TIMEOUT_S, transport=transport) as client:
        response = await client.get(ARXIV_API_URL, params=params)
    if response.status_code != 200:
        raise RuntimeError(f"arXiv API 返回 HTTP {response.status_code}")
    try:
        root = ET.fromstring(response.text)
    except ET.ParseError:
        raise RuntimeError("arXiv 响应解析失败") from None

    papers: list[dict] = []
    for entry in root.findall(f"{ATOM}entry"):
        arxiv_id = entry.findtext(f"{ATOM}id", "").rsplit("/abs/", 1)[-1].strip()
        arxiv_id = re.sub(r"v\d+$", "", arxiv_id)  # 去掉版本号（abs 页不带版本）
        published = entry.findtext(f"{ATOM}published", "")
        year = published[:4]
        if years_limit is not None and year and year.isdigit():
            import datetime as _dt

            if int(year) < _dt.datetime.now().year - years_limit:
                continue
        title = " ".join(entry.findtext(f"{ATOM}title", "").split())
        abstract = " ".join(entry.findtext(f"{ATOM}summary", "").split())
        authors = [a.findtext(f"{ATOM}name", "") for a in entry.findall(f"{ATOM}author")]
        papers.append(
            {
                "title": title,
                "authors": authors[:8],
                "year": year,
                "abstract": abstract,
                "url": f"https://arxiv.org/abs/{arxiv_id}",
                "arxiv_id": arxiv_id,
                "published": published,
            }
        )
        if len(papers) >= max_results:
            break
    return papers


class PaperSearchTool(BaseTool):
    definition = ToolDefinition(
        name="paper_search",
        description="tools.paper_search",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "关键词（可含标题/作者）"},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 10, "default": 3},
                "sort_by": {
                    "type": "string",
                    "enum": ["relevance", "date"],
                    "default": "relevance",
                },
                "years_limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 30,
                    "description": "只看最近 N 年（可选）",
                },
            },
            "required": ["query"],
        },
        mount=ToolMount.USER_TOGGLEABLE,
        cost_hint="tools.cost.paper_search",  # 双语键：chat.yaml tool_cost_hints
    )

    async def run(self, ctx: ToolContext) -> ToolResult:
        query = str(ctx.args.get("query", "")).strip()
        if not query:
            return ToolResult(ok=False, output="检索词不能为空。")
        try:
            papers = await _search_arxiv(
                query,
                int(ctx.args.get("max_results", 3)),
                sort_by=str(ctx.args.get("sort_by", "relevance")),
                years_limit=ctx.args.get("years_limit"),
            )
        except RuntimeError as exc:
            return ToolResult(ok=False, output=f"arXiv 检索失败：{exc}")

        if not papers:
            return ToolResult(ok=True, output=f"arXiv 上没有搜到与「{query}」相关的论文。")

        lines = [f"arXiv 检索到 {len(papers)} 篇论文："]
        sources: list[dict] = []
        for index, paper in enumerate(papers, 1):
            authors = ", ".join(paper["authors"]) or "未知作者"
            lines.append(
                f"[{index}] {paper['title']}（{paper['year']}）\n"
                f"    作者：{authors}\n"
                f"    {paper['url']}\n"
                f"    摘要：{paper['abstract'][:ABSTRACT_MAX_CHARS]}"
            )
            sources.append(
                {
                    "doc_id": paper["url"],
                    "kb": "arxiv",
                    "page": None,
                    "snippet": paper["abstract"][:ABSTRACT_MAX_CHARS],
                }
            )
        return ToolResult(ok=True, output="\n".join(lines), detail={"sources": sources})
