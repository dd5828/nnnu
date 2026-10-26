"""lightrag 引擎（§7.9，P14）：可选重依赖，构建与查询都在子进程里跑。

为什么是子进程而不是 `asyncio.to_thread`：LightRAG 的本地存储后端会在 async
方法内部做同步图合并与 JSON 序列化；线程方案挡不住 GIL 下的长 CPU 段，而且
取消时线程杀不掉，会继续往已经作废的 version 目录里写。子进程既满足「索引不
阻塞事件循环」，又能在用户点取消时直接杀掉（§7.9 验收）。

依赖：`pip install 'nnnu[rag-graph]'`（lightrag-hku）。没装时引擎仍登记在
列表里，但 `available()` 返回 false，界面显示原因、创建按钮禁用。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

from nnnu.services.rag.base import Chunk
from nnnu.services.rag.pipelines.graph_common import (
    GraphEngineBase,
    Runner,
    parse_worker_result,
    worker_env,
)

WORKER_MODULE = "nnnu.services.rag.pipelines.lightrag_worker"
CORPUS_FILE = "corpus.jsonl"
WORKER_SUBDIR = "lightrag"
SUPPORTED_MODES = ("auto", "vector", "hybrid", "local", "global", "naive", "mix")
DEFAULT_MODE = "hybrid"


class LightRagEngine(GraphEngineBase):
    engine_id = "lightrag"
    worker_module = WORKER_MODULE
    supported_modes = SUPPORTED_MODES

    def __init__(self, runner: Runner | None = None) -> None:
        super().__init__(runner=runner)

    @classmethod
    def available(cls) -> tuple[bool, str]:
        if importlib.util.find_spec("lightrag") is None:
            return False, "未安装 lightrag-hku；执行 pip install 'nnnu[rag-graph]' 后可用"
        return True, ""

    async def _build_index(
        self, index_dir: Path, chunks: list[Chunk], extra: dict[str, Any]
    ) -> None:
        corpus = self._group_by_doc(chunks)
        if not corpus:
            raise ValueError("没有可送进 LightRAG 的文本块")
        corpus_path = index_dir / CORPUS_FILE
        with corpus_path.open("w", encoding="utf-8") as handle:
            for item in corpus:
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")
        await self._runner(
            WORKER_MODULE,
            [
                "build",
                "--index-dir",
                str(index_dir),
                "--corpus",
                str(corpus_path),
            ],
            worker_env(),
            float(extra.get("timeout") or 30 * 60),
        )

    async def _query_context(self, index_dir: Path, query: str, mode: str) -> str:
        if not (index_dir / WORKER_SUBDIR).is_dir():
            return ""
        output = await self._runner(
            WORKER_MODULE,
            [
                "query",
                "--index-dir",
                str(index_dir),
                "--query",
                query,
                "--mode",
                normalize_mode(mode),
            ],
            worker_env(),
            10 * 60,
        )
        payload = parse_worker_result(output)
        return str(payload.get("context") or "")

    @staticmethod
    def _group_by_doc(chunks: list[Chunk]) -> list[dict[str, str]]:
        """按文档聚合（LightRAG 以文档为单位抽实体，按块送会把上下文切碎）。"""
        grouped: dict[str, list[Chunk]] = {}
        for chunk in chunks:
            grouped.setdefault(chunk.doc_id, []).append(chunk)
        items: list[dict[str, str]] = []
        for doc_id, items_chunks in grouped.items():
            ordered = sorted(items_chunks, key=lambda item: item.seq)
            text = "\n".join(chunk.text for chunk in ordered).strip()
            if text:
                items.append({"doc_id": doc_id, "text": text})
        return items


def normalize_mode(mode: str | None) -> str:
    """把界面/`rag` 工具的三档模式映射到 LightRAG 自己的检索模式。"""
    value = (mode or "").strip().lower()
    mapping = {
        "auto": "hybrid",
        "vector": "naive",
        "hybrid": "hybrid",
        "local": "local",
        "global": "global",
        "naive": "naive",
        "mix": "mix",
    }
    return mapping.get(value, DEFAULT_MODE)
