"""graphrag 引擎（§7.9，P14）：可选重依赖，走 GraphRAG 3.x 的 CLI。

和 lightrag 的取舍一样：主进程不 import graphrag，构建/查询都交给子进程。
区别是 GraphRAG 是**配置驱动**的——它要一个 `settings.yaml` 和 `input/*.txt`
目录，所以这里负责把 KB 的块按文档写成纯文本、把当前的模型/嵌入配置翻译成
GraphRAG 的 settings，再调用 `python -m graphrag index|query`。

依赖：`pip install 'nnnu[rag-graph]'`（graphrag>=3,<4）。没装时引擎仍登记，
`available()` 返回 false 并给出安装命令。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import yaml

from nnnu.services.rag.base import Chunk, EngineUnavailable
from nnnu.services.rag.pipelines.graph_common import (
    MAX_CONTEXT_CHARS,
    GraphEngineBase,
    Runner,
    worker_env,
)

WORKER_MODULE = "graphrag"
INPUT_DIR = "input"
SETTINGS_FILE = "settings.yaml"
SUPPORTED_MODES = ("auto", "vector", "hybrid", "local", "global", "drift", "basic")
DEFAULT_MODE = "local"
EMBEDDING_PROBE = "dimension probe"


class GraphRagEngine(GraphEngineBase):
    engine_id = "graphrag"
    worker_module = WORKER_MODULE
    supported_modes = SUPPORTED_MODES

    def __init__(self, runner: Runner | None = None) -> None:
        super().__init__(runner=runner)

    @classmethod
    def available(cls) -> tuple[bool, str]:
        if importlib.util.find_spec("graphrag") is None:
            return False, "未安装 graphrag；执行 pip install 'nnnu[rag-graph]' 后可用"
        return True, ""

    async def _build_index(
        self, index_dir: Path, chunks: list[Chunk], extra: dict[str, Any]
    ) -> None:
        input_dir = index_dir / INPUT_DIR
        input_dir.mkdir(parents=True, exist_ok=True)
        for old in input_dir.glob("*.txt"):
            old.unlink(missing_ok=True)
        documents = self._group_by_doc(chunks)
        if not documents:
            raise ValueError("没有可送进 GraphRAG 的文本块")
        for doc_id, text in documents.items():
            (input_dir / f"{doc_id}.txt").write_text(text, encoding="utf-8")

        settings, api_key = await self._settings()
        settings_path = index_dir / SETTINGS_FILE
        settings_path.write_text(
            yaml.safe_dump(settings, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )
        env = worker_env()
        if api_key:
            env["GRAPHRAG_API_KEY"] = api_key
        await self._runner(
            WORKER_MODULE,
            ["index", "--root", str(index_dir)],
            env,
            float(extra.get("timeout") or 60 * 60),
        )

    async def _query_context(self, index_dir: Path, query: str, mode: str) -> str:
        if not (index_dir / SETTINGS_FILE).is_file():
            return ""
        output = await self._runner(
            WORKER_MODULE,
            [
                "query",
                "--root",
                str(index_dir),
                "--method",
                normalize_mode(mode),
                "--query",
                query,
            ],
            worker_env(),
            20 * 60,
        )
        return _clean_answer(output)[:MAX_CONTEXT_CHARS]

    async def _settings(self) -> tuple[dict[str, Any], str | None]:
        """把当前 LLM/嵌入配置翻译成 GraphRAG 3.x 的 settings 字典。

        GraphRAG 只认 OpenAI 兼容的嵌入端点；本地 ONNX 嵌入没有 HTTP 端点，
        与其在子进程里报一个费解的连接错误，不如在这里明确拒绝。
        """
        from nnnu.services.embedding.service import EmbeddingService, resolve_config
        from nnnu.services.llm.errors import LLMConfigError
        from nnnu.services.llm.factory import resolve_model_config

        try:
            model = resolve_model_config()
        except LLMConfigError as exc:
            raise EngineUnavailable(f"GraphRAG 需要先配置聊天模型：{exc}") from exc
        embedding = resolve_config()
        if embedding.provider != "remote" or not embedding.base_url:
            raise EngineUnavailable(
                "GraphRAG 需要远程 OpenAI 兼容嵌入端点；请在设置里把嵌入切成远程端点"
            )
        # 维度只有真调一次才知道（远程端点），这里做一次轻量探针
        service = EmbeddingService()
        try:
            vectors = await service.embed_batch([EMBEDDING_PROBE])
            dim = len(vectors[0]) if vectors and vectors[0] else 0
        finally:
            await service.aclose()
        if dim <= 0:
            raise EngineUnavailable("GraphRAG 的嵌入端点没有返回向量，无法建索引")

        api_key = model.api_key or "sk-no-key-required"
        settings: dict[str, Any] = {
            "completion_models": {
                "default_completion_model": {
                    "type": "openai_chat",
                    "model_provider": "openai",
                    "model": model.model,
                    "auth_method": "api_key",
                    "api_key": api_key,
                    **({"api_base": model.base_url} if model.base_url else {}),
                }
            },
            "embedding_models": {
                "default_embedding_model": {
                    "type": "openai_embedding",
                    "model_provider": "openai",
                    "model": embedding.model,
                    "auth_method": "api_key",
                    "api_key": embedding.api_key or "sk-no-key-required",
                    "api_base": embedding.base_url,
                }
            },
            # 文本输入：KBService 已经把文档解析好了，GraphRAG 不再自己解析
            "input": {"type": "text", "file_pattern": r".*\.txt$$"},
            "input_storage": {"type": "file", "base_dir": INPUT_DIR},
            "output_storage": {"type": "file", "base_dir": "output"},
            "cache": {"type": "json", "storage": {"type": "file", "base_dir": "cache"}},
            "reporting": {"type": "file", "base_dir": "logs"},
            "vector_store": {
                "type": "lancedb",
                "db_uri": "output/lancedb",
                "vector_size": dim,
            },
        }
        return settings, (embedding.api_key or None)

    @staticmethod
    def _group_by_doc(chunks: list[Chunk]) -> dict[str, str]:
        grouped: dict[str, list[Chunk]] = {}
        for chunk in chunks:
            grouped.setdefault(chunk.doc_id, []).append(chunk)
        documents: dict[str, str] = {}
        for doc_id, items in grouped.items():
            text = "\n".join(chunk.text for chunk in sorted(items, key=lambda item: item.seq))
            if text.strip():
                documents[doc_id] = text
        return documents


def normalize_mode(mode: str | None) -> str:
    value = (mode or "").strip().lower()
    return {
        "auto": "local",
        "hybrid": "local",
        "vector": "basic",
        "local": "local",
        "global": "global",
        "drift": "drift",
        "basic": "basic",
    }.get(value, DEFAULT_MODE)


def _clean_answer(stdout: str) -> str:
    """GraphRAG 把日志写 stderr，答案写 stdout；这里只清掉偶发的日志行。"""
    lines = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("INFO:", "WARNING:", "DEBUG:", "ERROR:")):
            continue
        lines.append(line)
    return "\n".join(lines).strip()
