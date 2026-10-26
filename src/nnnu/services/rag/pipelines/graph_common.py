"""图谱引擎的公共底座（§7.9，P14）。

graphrag / lightrag 都是「重依赖 + 自己调模型」的引擎，和 vector / pageindex
的「外层算好 chunks+向量、引擎只写文件」不是同一种东西。这里收敛三件共用的事：

1. **子进程隔离**：构建/查询放到独立进程里跑。理由不是洁癖——LightRAG 的本地
   存储会在 async 方法里做同步图合并，跑在主事件循环上会把整个后端卡住
   （§7.9 验收明确要求不阻塞）；子进程还顺带解决了「取消时杀得掉」和
   「库版本升级不会把主进程 import 崩」。
2. **仍保留块级引用索引**：图谱查询返回的是上下文/答案，没有稳定页码。我们
   同时用 chunks.jsonl + BM25 留一份块索引，把图谱上下文挂在最相关的那条块
   命中上，引用至少能落回原文；没有块命中时退化成一条 `synthetic` 命中。
3. **错误可读**：依赖没装、模型没配、子进程失败都转成 `EngineUnavailable`
   或带 stderr 尾部的错误，界面直接显示，不让用户面对一个 ImportError。

whitelist 说明（§16.5）：这个文件与两个适配器都是自研实现，只参考了参考仓库
的接口划分，没有复制它的 pipeline 代码。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from nnnu.services.rag.base import BaseEngine, Chunk, EngineUnavailable, Hit
from nnnu.services.rag.bm25 import Bm25Index

if TYPE_CHECKING:
    import numpy as np

    from nnnu.services.embedding.base import EmbeddingProvider

CHUNKS_FILE = "chunks.jsonl"
BM25_FILE = "bm25.json"
RESULT_MARKER = "__NNNU_RAG_RESULT__"
WORKER_TIMEOUT_S = 30 * 60  # 图谱构建是分钟级；查询复用同一个上限，超时给明确错误
MAX_CONTEXT_CHARS = 12_000

Runner = Callable[[str, list[str], dict[str, str] | None, float], Awaitable[str]]


class EngineProcessError(RuntimeError):
    """图谱子进程非零退出或超时（消息含 stderr 尾部，便于定位）。"""


def write_citation_index(index_dir: Path, chunks: list[Chunk]) -> None:
    """写块索引（chunks.jsonl + bm25.json）。各引擎的图谱产物另写自己的目录。"""
    index_dir.mkdir(parents=True, exist_ok=True)
    tmp = index_dir / (CHUNKS_FILE + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        for chunk in chunks:
            handle.write(json.dumps(chunk.model_dump(), ensure_ascii=False) + "\n")
    tmp.replace(index_dir / CHUNKS_FILE)
    tmp = index_dir / (BM25_FILE + ".tmp")
    tmp.write_text(
        json.dumps(Bm25Index.from_chunks(chunks).to_json(), ensure_ascii=False), encoding="utf-8"
    )
    tmp.replace(index_dir / BM25_FILE)


def read_chunks(index_dir: Path) -> list[Chunk]:
    path = index_dir / CHUNKS_FILE
    if not path.is_file():
        return []
    chunks: list[Chunk] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                chunks.append(Chunk.model_validate(json.loads(line)))
    return chunks


def load_chunk_index(index_dir: Path) -> tuple[list[Chunk], Bm25Index]:
    chunks = read_chunks(index_dir)
    bm25_path = index_dir / BM25_FILE
    if bm25_path.is_file():
        bm25 = Bm25Index.from_json(json.loads(bm25_path.read_text(encoding="utf-8")))
    else:
        bm25 = Bm25Index.from_chunks(chunks)
    return chunks, bm25


async def run_module(
    module: str,
    args: list[str],
    env: dict[str, str] | None = None,
    timeout: float = WORKER_TIMEOUT_S,
) -> str:
    """在当前解释器里跑 `python -m module ...`，返回 stdout。

    stderr 只用来拼错误信息；stdout 里可能混着库的日志，调用方按 RESULT_MARKER
    取最后一段 JSON（见 `parse_worker_result`）。
    """
    merged = dict(os.environ)
    if env:
        merged.update(env)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        module,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=merged,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise EngineProcessError(f"图谱子进程超时（>{timeout:.0f}s），已终止") from exc
    except asyncio.CancelledError:
        # 用户取消构建/查询：先杀子进程再退出，别留一个孤儿在后台跑几十分钟
        process.kill()
        await process.wait()
        raise
    text = stdout.decode("utf-8", errors="replace")
    if process.returncode != 0:
        tail = stderr.decode("utf-8", errors="replace").strip().splitlines()[-8:]
        raise EngineProcessError(
            f"图谱子进程退出码 {process.returncode}：{' | '.join(tail) or '没有 stderr'}"
        )
    return text


def parse_worker_result(stdout: str) -> dict[str, Any]:
    """从 stdout 里取最后一个 RESULT_MARKER 之后的 JSON 对象。"""
    index = stdout.rfind(RESULT_MARKER)
    if index < 0:
        raise EngineProcessError("图谱子进程没有返回结果标记（stdout 可能被库日志污染）")
    payload = stdout[index + len(RESULT_MARKER) :].strip()
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise EngineProcessError(f"图谱子进程结果不是合法 JSON：{payload[:200]!r}") from exc
    return data if isinstance(data, dict) else {"value": data}


def worker_env() -> dict[str, str]:
    """子进程继承当前 NNNU_HOME（设置/密钥/数据目录都在里面）。"""
    env: dict[str, str] = {}
    home = os.environ.get("NNNU_HOME")
    if home:
        env["NNNU_HOME"] = home
    return env


def attach_graph_context(
    hits: list[Hit],
    context: str,
    *,
    engine_id: str,
    kb_id: str,
    query: str,
) -> list[Hit]:
    """把图谱上下文挂到最相关的块上；没有块命中时给一条 synthetic 命中。"""
    text = (context or "").strip()
    if not text:
        return hits
    text = text[:MAX_CONTEXT_CHARS]
    if hits:
        first = hits[0]
        merged = f"【{engine_id} 图谱上下文】\n{text}\n\n【原文片段】\n{first.text}"
        hits[0] = first.model_copy(
            update={
                "text": merged,
                "metadata": {
                    **first.metadata,
                    "graph_context": True,
                    "graph_engine": engine_id,
                },
            }
        )
        return hits
    return [
        Hit(
            doc_id="",
            kb_id=kb_id,
            score=1.0,
            text=f"【{engine_id} 图谱上下文】\n{text}",
            metadata={
                "synthetic": True,
                "graph_context": True,
                "graph_engine": engine_id,
                "filename": f"{engine_id} 图谱",
                "query": query,
            },
        )
    ]


class GraphEngineBase(BaseEngine):
    """子类实现 `_build_index` 与 `_query_context`；其余共用。"""

    needs_embeddings = False
    worker_module = ""
    engine_id = ""

    def __init__(self, runner: Runner | None = None) -> None:
        self._runner = runner or run_module

    @classmethod
    def available(cls) -> tuple[bool, str]:
        raise NotImplementedError

    async def write_index(
        self,
        index_dir: Path,
        *,
        chunks: list[Chunk],
        vectors: "np.ndarray",
        dim: int,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.require_available()
        write_citation_index(index_dir, chunks)
        await self._build_index(index_dir, chunks, extra or {})

    async def query(
        self,
        index_dir: Path,
        query: str,
        *,
        kb_id: str,
        top_k: int,
        mode: str,
        excluded_docs: frozenset[str] = frozenset(),
        embedder: "EmbeddingProvider | None" = None,
    ) -> list[Hit]:
        self.require_available()
        context = await self._query_context(index_dir, query, mode)
        chunks, bm25 = load_chunk_index(index_dir)
        by_id = {chunk.chunk_id: chunk for chunk in chunks}
        lexical = bm25.search(query, max(1, top_k), excluded_docs=excluded_docs)
        hits: list[Hit] = []
        for rank, (chunk_id, score) in enumerate(lexical, start=1):
            chunk = by_id.get(chunk_id)
            if chunk is None:
                continue
            hits.append(
                Hit(
                    doc_id=chunk.doc_id,
                    kb_id=kb_id,
                    score=score,
                    text=chunk.text,
                    page=chunk.page,
                    metadata={"chunk_id": chunk.chunk_id, "lexical_rank": rank},
                )
            )
        return attach_graph_context(
            hits, context, engine_id=self.engine_id, kb_id=kb_id, query=query
        )

    @classmethod
    def require_available(cls) -> None:
        ok, reason = cls.available()
        if not ok:
            raise EngineUnavailable(reason)

    async def _build_index(
        self, index_dir: Path, chunks: list[Chunk], extra: dict[str, Any]
    ) -> None:
        raise NotImplementedError

    async def _query_context(self, index_dir: Path, query: str, mode: str) -> str:
        raise NotImplementedError
