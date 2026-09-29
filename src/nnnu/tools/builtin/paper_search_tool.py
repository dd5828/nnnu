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


def _phrase_query(query: str) -> str:
    """整句当一个词组匹配（arXiv 语法：带引号的 `all:"…"`）。"""
    return f'all:"{query}"'


def _query_ladder(query: str) -> list[str]:
    """检索式阶梯：先按词组精确匹配，一个结果都没有再退回按词裸写。

    裸写的 `all:一串关键词` 会被 arXiv 解析器拆散成 OR 语义，长查询直接变成噪音
    （实测拿一篇综述的完整标题去搜，第一条回来的是毫不相干的引力波论文）；
    加引号能锚住，但词组太长的查询会 0 命中。所以两个都留着：精确优先，落空退宽松。
    单个词的查询没有「词组」可言，直接用裸写，省一次往返。
    """
    cleaned = " ".join(query.replace('"', " ").split())
    if len(cleaned.split()) < 2:
        return [_build_query(cleaned)]
    return [_phrase_query(cleaned), _build_query(cleaned)]


def _parse_atom(text: str, max_results: int, years_limit: int | None) -> list[dict]:
    """Atom 响应 → 论文列表（先按年份窗口过滤，再按 max_results 截断）。"""
    try:
        root = ET.fromstring(text)
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


async def _search_arxiv(
    query: str,
    max_results: int,
    *,
    sort_by: str = "relevance",
    years_limit: int | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[dict]:
    """arXiv Atom API 检索；返回 [{title, authors, year, abstract, url, arxiv_id, published}]。"""
    limit = max(1, min(int(max_results), 20))
    params_base = {
        "max_results": str(limit),
        "sortBy": "submittedDate" if sort_by == "date" else "relevance",
    }
    async with httpx.AsyncClient(timeout=TIMEOUT_S, transport=transport) as client:
        for expression in _query_ladder(query):
            response = await client.get(
                ARXIV_API_URL, params={"search_query": expression, **params_base}
            )
            if response.status_code != 200:
                raise RuntimeError(f"arXiv API 返回 HTTP {response.status_code}")
            papers = _parse_atom(response.text, limit, years_limit)
            if papers:
                return papers
    return []


class PaperSearchTool(BaseTool):
    definition = ToolDefinition(
        name="paper_search",
        description="tools.paper_search",
        parameters={
            "type": "object",
            "properties": {
                # 查询写短是检索质量的一半：长串关键词会被 arXiv 解析器拆散成 OR
                "query": {"type": "string", "description": "检索词（2~4 个词，或论文标题）"},
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
                    "title": paper["title"],
                }
            )
        return ToolResult(ok=True, output="\n".join(lines), detail={"sources": sources})
