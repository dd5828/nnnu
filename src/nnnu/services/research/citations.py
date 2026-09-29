"""引用记号重排（§7.6）：模型只写 `[CIT-<k>-<nn>]`，编号由代码说了算。

调研分成 N 个子问题各跑各的检索，每个子问题攒自己的证据。模型在子问题摘要里写的
`[CIT-k-nn]`（k = 子问题序号，nn = 该来源在本子问题里第几个出现）在这里被统一重排成
`[1] [2] [3]`——**号在正文里第一次引用时才发**（按引用顺序连排），**同一个来源
（doc_id + page）全篇复用同一个号**，对不上任何真实来源的记号直接剥掉并计一次 warning。

为什么编号权不给模型：报告的说服力全压在「每条引用都能点开验证」上，而模型很乐意
给没检索到的来源也标个号。编号握在代码手里，它能引的就只有工具真给过的来源。
上游有同类分工（deeptutor/agents/research/utils/citation_manager.py），但那是拿全局
自增 id 在工具层注册，本仓的模型是子问题命名空间，只作对照、未搬实现（§16.5）。
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# 模型写的记号；k/nn 都只认十进制数字，别的形态一律当普通文本
MARKER_RE = re.compile(r"\[CIT-(\d+)-(\d+)\]")
SNIPPET_MAX_CHARS = 140


class CitationBook:
    """按子问题收集来源 → 正文首次引用时发号 → 重排正文 → 生成参考资料清单。

    为什么号要等到被引用才发：登记的来源里混着搜索通道的噪音（必应中文版把英文长查询
    拆成词，回一屏「HYBRID 剑桥词典」），全量列进参考资料就成了「这篇报告引用了它」的
    假声明。清单要的是「正文引用了什么」，不是「这次一共搜到了什么」。
    """

    def __init__(self) -> None:
        self._by_subtopic: dict[int, list[dict]] = {}
        # (doc_id, page) → 全局编号：跨子问题去重，同一份来源全篇一个号
        self._numbers: dict[tuple[str, int | None], int] = {}
        # 被正文引用过的来源，按首次引用顺序——参考资料清单照这个列
        self.used: list[dict] = []

    def add_subtopic(self, index: int, sources: list[dict]) -> None:
        """登记某个子问题检索到的来源（按工具返回顺序）。

        存归一化后的副本（只留 `CitationSource` 认识的五个键、缺的补默认值）：登记进来的
        不是原始 dict 而是统一形状，后面的编号键 `(doc_id, page)` 与清单渲染才不会各说各话。
        """
        self._by_subtopic[index] = [_normalize(source) for source in sources]

    def source_at(self, index: int, nn: int) -> dict | None:
        """子问题 index 里的第 nn 个来源（对不上返回 None）。"""
        sources = self._by_subtopic.get(index)
        if not sources or nn < 1 or nn > len(sources):
            return None
        return sources[nn - 1]

    def renumber(self, text: str) -> tuple[str, int]:
        """正文里的 `[CIT-k-nn]` → `[n]`；对不上号的记号剥掉，返回 (新正文, 剥掉的个数)。

        号在扫描过程中按首次出现顺序发（`MARKER_RE.sub` 从左到右走，正好就是引用顺序）。
        """
        dropped = 0

        def _replace(match: re.Match[str]) -> str:
            nonlocal dropped
            source = self.source_at(int(match.group(1)), int(match.group(2)))
            if source is None:
                dropped += 1
                return ""
            return f"[{self._number_of(source)}]"

        return MARKER_RE.sub(_replace, text), dropped

    def _number_of(self, source: dict) -> int:
        """取号，没有就发一个新号（首次引用才进 `used`）。"""
        key = _key(source)
        number = self._numbers.get(key)
        if number is None:
            number = len(self.used) + 1
            self._numbers[key] = number
            self.used.append(source)
        return number

    def references_lines(self, render_item, *, heading: str) -> list[str]:
        """参考资料清单（markdown 有序列表）：`1. [标题](url) — 摘要`，只列正文引用过的。

        清单由代码生成、不由模型生成：这样「每条都能点开验证」是构造保证的，
        也省得模型去抄一串它记不准的 URL。`render_item(index=, title=, url=, snippet=)`
        由调用方传进来（要走提示词 i18n）。
        """
        if not self.used:
            return []
        lines = [heading.strip(), ""]
        for number, source in enumerate(self.used, 1):
            url = _clean_url(str(source.get("doc_id") or ""))
            if not url:
                continue
            lines.append(
                render_item(
                    index=number,
                    title=_clean_text(str(source.get("title") or ""), url),
                    url=url,
                    snippet=_clean_text(str(source.get("snippet") or ""), ""),
                )
            )
        # 一条能点的都没有时连标题也别留：挂个空的「参考资料」比不挂更像出了故障
        return lines if len(lines) > 2 else []


def _key(source: dict) -> tuple[str, int | None]:
    page = source.get("page")
    return (str(source.get("doc_id") or ""), int(page) if isinstance(page, int) else None)


def _normalize(source: dict) -> dict:
    """只留 `CitationSource` 认识的五个键，缺的补默认值（多出来的字段不进事件）。"""
    page = source.get("page")
    return {
        "doc_id": str(source.get("doc_id") or ""),
        "kb": str(source.get("kb") or ""),
        "page": int(page) if isinstance(page, int) else None,
        "snippet": str(source.get("snippet") or ""),
        "title": str(source.get("title") or ""),
    }


def _clean_text(raw: str, fallback: str) -> str:
    """标题/摘要：压平换行、去掉会破坏 markdown 链接语法的方括号。"""
    text = " ".join(raw.split())
    text = text.replace("[", "（").replace("]", "）")
    if len(text) > SNIPPET_MAX_CHARS:
        text = text[:SNIPPET_MAX_CHARS].rstrip() + "…"
    return text or fallback


def _clean_url(raw: str) -> str:
    """只放行 http(s)；圆括号会截断 markdown 链接目标，转义成 %28/%29。"""
    url = raw.strip()
    if not url.lower().startswith(("http://", "https://")):
        return ""
    return url.replace("(", "%28").replace(")", "%29")
