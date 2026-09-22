"""搜索域共享类型（§7.2）：各提供商统一输出 SearchHit 列表。

adapted from DeepTutor (Apache-2.0) deeptutor/services/search/types.py ——
保留 Citation/SearchResult 的字段语义，合并为单类型（本项目无 LLM 合成答案层）。
"""

from dataclasses import dataclass, field


@dataclass(slots=True)
class SearchHit:
    title: str
    url: str
    snippet: str
    content: str = ""  # 更长摘要（Bocha summary / SearXNG content，可为空）
    date: str = ""
    source: str = ""


@dataclass(slots=True)
class SearchResponse:
    query: str
    provider: str
    hits: list[SearchHit] = field(default_factory=list)
    error: str | None = None
