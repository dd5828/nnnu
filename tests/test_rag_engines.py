"""P14 检索引擎单测：pageindex 页级召回、引擎注册表、图谱引擎的公共路径。

图谱依赖（lightrag/graphrag）在 CI 里不装，所以这里只测「依赖可用之后」的
代码路径（用假 runner 代替子进程），依赖探测本身用 find_spec 打桩。
"""

from __future__ import annotations

import importlib.util
import json

import numpy as np
import pytest

from nnnu.services.rag.base import Chunk, EngineUnavailable, Hit
from nnnu.services.rag.pipelines.graph_common import (
    RESULT_MARKER,
    attach_graph_context,
    load_chunk_index,
    parse_worker_result,
)
from nnnu.services.rag.pipelines.lightrag import WORKER_SUBDIR, LightRagEngine, normalize_mode
from nnnu.services.rag.pipelines.pageindex import PageIndexEngine
from nnnu.services.rag.pipelines.vector import VectorEngine
from nnnu.services.rag.registry import (
    DEFAULT_ENGINE,
    ENGINE_IDS,
    build_engine,
    engine_info,
    engine_infos,
)


class DictEmbedder:
    label = "test:dict"

    def __init__(self, mapping: dict[str, list[float]]) -> None:
        self.mapping = mapping

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self.mapping[text] for text in texts]


def _unit(*values: float) -> list[float]:
    array = np.asarray(values, dtype=np.float32)
    return (array / np.linalg.norm(array)).tolist()


def _sample_chunks() -> tuple[list[Chunk], np.ndarray]:
    chunks = [
        Chunk(chunk_id="c1", doc_id="doc-1", seq=0, text="傅里叶变换 频谱分析", page=1),
        Chunk(chunk_id="c2", doc_id="doc-1", seq=1, text="时域信号与频域", page=1),
        Chunk(chunk_id="c3", doc_id="doc-1", seq=2, text="滤波器设计 通带阻带", page=2),
        Chunk(chunk_id="c4", doc_id="doc-2", seq=0, text="光合作用 叶绿体", page=1),
    ]
    vectors = np.asarray(
        [_unit(1, 0), _unit(0.9, 0.1), _unit(0, 1), _unit(0.7, 0.7)], dtype=np.float32
    )
    return chunks, vectors


# ================= pageindex =================


async def test_pageindex_recalls_pages_and_carries_page_id(tmp_path):
    chunks, vectors = _sample_chunks()
    engine = PageIndexEngine()
    index_dir = tmp_path / "index"
    await engine.write_index(index_dir, chunks=chunks, vectors=vectors, dim=2)

    loaded = engine.load(index_dir)  # 增量复用走底层 vector 三件套
    assert [chunk.chunk_id for chunk in loaded.chunks] == [c.chunk_id for c in chunks]

    embedder = DictEmbedder({"傅里叶 频谱": _unit(1, 0), "叶绿体": _unit(1, 0)})
    hits = await engine.query(
        index_dir, "傅里叶 频谱", kb_id="kb-1", top_k=3, mode="hybrid", embedder=embedder
    )
    assert hits
    top = hits[0]
    assert top.doc_id == "doc-1"
    assert top.page == 1
    assert top.metadata["page_id"] == "doc-1:p1"
    assert top.metadata["chunk_id"] in {"c1", "c2"}
    assert top.metadata["page_rank"] == 1
    assert top.metadata["page_cosine"] > 0.9
    assert 0.0 <= top.metadata["cosine"] <= 1.0

    # 查询叶绿体、向量故意给成 doc-1 方向：字面命中仍要进融合结果
    lexical = await engine.query(
        index_dir, "叶绿体", kb_id="kb-1", top_k=6, mode="hybrid", embedder=embedder
    )
    by_doc = {hit.doc_id: hit for hit in lexical}
    assert "doc-2" in by_doc and by_doc["doc-2"].page == 1
    assert "lexical_rank" in by_doc["doc-2"].metadata

    # vector 模式不走向量以外的召回
    vector_only = await engine.query(
        index_dir, "傅里叶 频谱", kb_id="kb-1", top_k=2, mode="vector", embedder=embedder
    )
    assert vector_only and vector_only[0].page == 1


async def test_pageindex_without_embedder_falls_back_to_lexical(tmp_path):
    """嵌入端点临时不可用时（embedder=None）：不该崩，能靠 BM25 给结果。"""
    chunks, vectors = _sample_chunks()
    engine = PageIndexEngine()
    index_dir = tmp_path / "index"
    await engine.write_index(index_dir, chunks=chunks, vectors=vectors, dim=2)
    hits = await engine.query(
        index_dir, "滤波器 通带", kb_id="kb-1", top_k=2, mode="hybrid", embedder=None
    )
    assert hits and hits[0].doc_id == "doc-1"


async def test_pageindex_empty_index(tmp_path):
    engine = PageIndexEngine()
    index_dir = tmp_path / "index"
    await engine.write_index(
        index_dir, chunks=[], vectors=np.zeros((0, 0), dtype=np.float32), dim=0
    )
    assert await engine.query(index_dir, "x", kb_id="kb", top_k=3, mode="hybrid") == []


# ================= 注册表 =================


def test_registry_lists_all_engines_and_respects_optional_deps(monkeypatch):
    real = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name, *args, **kwargs: (
            None if name in {"lightrag", "graphrag"} else real(name, *args, **kwargs)
        ),
    )
    infos = {info.id: info for info in engine_infos()}
    assert tuple(infos) == ENGINE_IDS
    assert infos[DEFAULT_ENGINE].available is True
    assert infos["pageindex"].available is True
    assert infos["pageindex"].needs_embeddings is True
    for engine_id in ("lightrag", "graphrag"):
        assert infos[engine_id].available is False
        assert "pip install" in infos[engine_id].reason
    assert infos["lightrag"].needs_embeddings is False
    assert "global" in infos["graphrag"].modes

    assert isinstance(build_engine("vector"), VectorEngine)
    assert isinstance(build_engine("pageindex"), PageIndexEngine)
    with pytest.raises(EngineUnavailable):
        build_engine("lightrag")
    with pytest.raises(EngineUnavailable):
        engine_info("nope")


# ================= 图谱引擎公共路径 =================


class FakeRunner:
    def __init__(self, result: str = "") -> None:
        self.result = result
        self.calls: list[tuple[str, list[str]]] = []

    async def __call__(self, module, args, env, timeout):
        self.calls.append((module, list(args)))
        return self.result


def _allow_lightrag(monkeypatch) -> None:
    monkeypatch.setattr(LightRagEngine, "available", classmethod(lambda cls: (True, "")))


async def test_lightrag_build_and_query_use_subprocess_runner(tmp_path, monkeypatch):
    _allow_lightrag(monkeypatch)
    runner = FakeRunner()
    engine = LightRagEngine(runner=runner)
    chunks, _ = _sample_chunks()
    index_dir = tmp_path / "index"
    await engine.write_index(
        index_dir, chunks=chunks, vectors=np.zeros((0, 0), dtype=np.float32), dim=0
    )
    assert (index_dir / "corpus.jsonl").is_file()
    assert runner.calls and runner.calls[0][0] == engine.worker_module
    assert runner.calls[0][1][0] == "build"

    loaded_chunks, _ = load_chunk_index(index_dir)  # 引用索引仍可读
    assert [chunk.chunk_id for chunk in loaded_chunks] == [c.chunk_id for c in chunks]

    (index_dir / WORKER_SUBDIR).mkdir()  # 真实 worker 建图后才会出现
    runner.result = RESULT_MARKER + json.dumps({"context": "傅里叶把时域变成频域。"})
    hits = await engine.query(
        index_dir, "傅里叶", kb_id="kb-1", top_k=3, mode="hybrid", embedder=None
    )
    assert hits
    assert "【lightrag 图谱上下文】" in hits[0].text
    assert "傅里叶把时域变成频域" in hits[0].text
    assert hits[0].metadata["graph_context"] is True
    assert hits[0].kb_id == "kb-1"
    assert hits[0].metadata["chunk_id"] == "c1"


async def test_lightrag_query_without_lexical_hit_returns_synthetic(tmp_path, monkeypatch):
    _allow_lightrag(monkeypatch)
    runner = FakeRunner(RESULT_MARKER + json.dumps({"context": "只有图谱里有这句话。"}))
    engine = LightRagEngine(runner=runner)
    chunks, _ = _sample_chunks()
    index_dir = tmp_path / "index"
    await engine.write_index(
        index_dir, chunks=chunks, vectors=np.zeros((0, 0), dtype=np.float32), dim=0
    )
    (index_dir / WORKER_SUBDIR).mkdir()
    hits = await engine.query(
        index_dir, "完全不相关的词", kb_id="kb-9", top_k=3, mode="hybrid", embedder=None
    )
    assert len(hits) == 1
    assert hits[0].metadata["synthetic"] is True
    assert hits[0].kb_id == "kb-9"
    assert hits[0].doc_id == ""


def test_graph_attachments_and_worker_result_parsing():
    hits = [Hit(doc_id="doc-1", kb_id="kb-2", score=1.0, text="原文", metadata={"chunk_id": "c1"})]
    out = attach_graph_context(hits, "上下文", engine_id="graphrag", kb_id="kb-2", query="q")
    assert out[0].text.startswith("【graphrag 图谱上下文】")
    assert out[0].metadata["graph_engine"] == "graphrag"

    synthetic = attach_graph_context([], "上下文", engine_id="graphrag", kb_id="kb-2", query="q")
    assert synthetic[0].kb_id == "kb-2" and synthetic[0].metadata["synthetic"] is True

    assert parse_worker_result("log line\n" + RESULT_MARKER + '{"context": "x"}') == {
        "context": "x"
    }
    with pytest.raises(Exception):
        parse_worker_result("no marker here")


def test_lightrag_mode_mapping():
    assert normalize_mode(None) == "hybrid"
    assert normalize_mode("auto") == "hybrid"
    assert normalize_mode("vector") == "naive"
    assert normalize_mode("local") == "local"
    assert normalize_mode("mix") == "mix"
    assert normalize_mode("unknown") == "hybrid"
