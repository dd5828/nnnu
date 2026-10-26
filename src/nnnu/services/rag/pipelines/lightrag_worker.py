"""LightRAG 子进程 worker（§7.9，P14）。

只在子进程里被 `python -m nnnu.services.rag.pipelines.lightrag_worker` 调用；
主进程不 import lightrag。构建与查询都走同一个 worker，输出用固定标记包一段
JSON，避免库自己的 stdout 日志混进结果。

模型与嵌入配置从当前进程的 NNNU_HOME 读取（设置/密钥都在那儿），所以子进程
必须是主进程的同一个 Python 环境、同一个 home——由 runner 负责继承环境变量。
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np

from nnnu.services.llm.factory import create_client, resolve_model_config
from nnnu.services.llm.protocol import LLMRequest
from nnnu.services.rag.pipelines.graph_common import RESULT_MARKER

WORKER_SUBDIR = "lightrag"
TOP_K = 12


def _read_corpus(path: Path) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            raw = json.loads(line)
            text = str(raw.get("text") or "").strip()
            doc_id = str(raw.get("doc_id") or "").strip()
            if text and doc_id:
                items.append({"doc_id": doc_id, "text": text})
    return items


def _embedding_func_type() -> type:
    try:
        from lightrag.utils import EmbeddingFunc
    except ImportError:  # 不同版本可能把它放顶层
        from lightrag import EmbeddingFunc  # type: ignore[attr-defined,no-redef]

    return EmbeddingFunc


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _make_funcs() -> tuple[Any, Any, Any]:
    """→ (llm_model_func, embedding_func 实例, 清理回调)。"""
    from nnnu.services.embedding.service import EmbeddingService

    embedding = EmbeddingService()
    probe = await embedding.embed_batch(["dimension probe"])
    if not probe or not probe[0]:
        raise RuntimeError("嵌入端点没有返回向量，无法初始化 LightRAG")
    dim = len(probe[0])

    model_config = resolve_model_config()
    client = create_client(
        model_config.model,
        provider_id=model_config.provider_id,
        base_url=model_config.base_url,
        api_key=model_config.api_key,
    )

    async def embed(texts: list[str]) -> np.ndarray:
        vectors = await embedding.embed_batch(list(texts))
        return np.asarray(vectors, dtype=np.float32)

    async def llm(
        prompt: str,
        system_prompt: str | None = None,
        history_messages: list[dict[str, str]] | None = None,
        **kwargs: Any,
    ) -> str:
        messages: list[dict[str, Any]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        for message in history_messages or []:
            role = str(message.get("role") or "user")
            messages.append({"role": role, "content": str(message.get("content") or "")})
        messages.append({"role": "user", "content": prompt})
        response = await client.complete(
            LLMRequest(
                messages=messages,
                model=model_config.model,
                temperature=0.0,
                max_tokens=kwargs.get("max_tokens"),
            )
        )
        return response.text

    embedding_type = _embedding_func_type()

    async def cleanup() -> None:
        await embedding.aclose()
        close = getattr(client, "aclose", None)
        if close is not None:
            await _maybe_await(close())

    return llm, embedding_type(embedding_dim=dim, max_token_size=8192, func=embed), cleanup


async def _init_rag(rag: Any) -> None:
    await _maybe_await(getattr(rag, "initialize_storages", lambda: None)())
    try:
        from lightrag.kg.shared_storage import initialize_pipeline_status
    except ImportError:
        return
    await _maybe_await(initialize_pipeline_status())


async def _finalize_rag(rag: Any) -> None:
    finalize = getattr(rag, "finalize_storages", None)
    if finalize is not None:
        await _maybe_await(finalize())


def _make_rag(working_dir: Path, llm: Any, embedding_func: Any) -> Any:
    from lightrag import LightRAG

    return LightRAG(
        working_dir=str(working_dir),
        llm_model_func=llm,
        embedding_func=embedding_func,
    )


async def _build(index_dir: Path, corpus: list[dict[str, str]]) -> None:
    from lightrag import LightRAG

    llm, embedding_func, cleanup = await _make_funcs()
    working_dir = index_dir / WORKER_SUBDIR
    working_dir.mkdir(parents=True, exist_ok=True)
    rag = _make_rag(working_dir, llm, embedding_func)
    try:
        await _init_rag(rag)
        texts = [item["text"] for item in corpus]
        ids = [item["doc_id"] for item in corpus]
        kwargs: dict[str, Any] = {}
        parameters = inspect.signature(rag.ainsert).parameters
        if "ids" in parameters:
            kwargs["ids"] = ids
        if "file_paths" in parameters:
            kwargs["file_paths"] = [f"{doc_id}.txt" for doc_id in ids]
        await rag.ainsert(texts, **kwargs)
        del LightRAG
    finally:
        await _finalize_rag(rag)
        await cleanup()


async def _query(index_dir: Path, query: str, mode: str) -> str:
    llm, embedding_func, cleanup = await _make_funcs()
    working_dir = index_dir / WORKER_SUBDIR
    if not working_dir.is_dir():
        await cleanup()
        return ""
    rag = _make_rag(working_dir, llm, embedding_func)
    try:
        await _init_rag(rag)
        from lightrag import QueryParam

        options: dict[str, Any] = {
            "mode": mode,
            "top_k": TOP_K,
            "only_need_context": True,
        }
        parameters = inspect.signature(QueryParam).parameters
        options = {key: value for key, value in options.items() if key in parameters}
        result = await rag.aquery(query, param=QueryParam(**options))
        if isinstance(result, str):
            return result
        if isinstance(result, dict):
            for key in ("context", "response", "answer", "result"):
                value = result.get(key)
                if isinstance(value, str) and value.strip():
                    return value
        return str(result or "")
    finally:
        await _finalize_rag(rag)
        await cleanup()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="nnnu LightRAG worker")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("--index-dir", required=True)
    build.add_argument("--corpus", required=True)
    query = sub.add_parser("query")
    query.add_argument("--index-dir", required=True)
    query.add_argument("--query", required=True)
    query.add_argument("--mode", default="hybrid")
    args = parser.parse_args(argv)

    index_dir = Path(args.index_dir)
    if args.command == "build":
        corpus = _read_corpus(Path(args.corpus))
        if not corpus:
            print("没有可索引的文档", file=sys.stderr)
            return 2
        asyncio.run(_build(index_dir, corpus))
        payload: dict[str, Any] = {"ok": True, "docs": len(corpus)}
    else:
        context = asyncio.run(_query(index_dir, args.query, args.mode))
        payload = {"ok": True, "context": context}
    print(RESULT_MARKER + json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover - 子进程入口
    raise SystemExit(main())
