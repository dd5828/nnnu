"""vector 引擎单测（§7.9）：写读往返、删除过滤、维度校验、混合检索差异。

「混合 ≠ 纯向量」这条用 TopicEmbedder 构造：纯向量把「同主题但没问到词面」的
文档排前，混合把「词面+语义都命中」的文档提到第一——两边都讲得通（验收 B）。
"""

import numpy as np
import pytest
from conftest import HashEmbedder, TopicEmbedder

from nnnu.services.rag.base import Chunk
from nnnu.services.rag.pipelines.vector import IndexDimMismatchError, VectorEngine


def _chunk(doc_id: str, seq: int, text: str, page: int | None) -> Chunk:
    return Chunk(chunk_id=f"{doc_id}#{seq}", doc_id=doc_id, seq=seq, text=text, page=page)


async def _build(tmp_path, chunks: list[Chunk], embedder) -> tuple[VectorEngine, object]:
    engine = VectorEngine()
    vectors = np.asarray(await embedder.embed([c.text for c in chunks]), dtype=np.float32)
    index_dir = tmp_path / "index" / "version-1"
    await engine.write_index(index_dir, chunks=chunks, vectors=vectors, dim=embedder.dim)
    return engine, index_dir


async def test_round_trip_finds_exact_text(tmp_path):
    chunks = [
        _chunk("kbdoc-a", 0, "傅里叶变换把时域信号分解为频域分量。", 1),
        _chunk("kbdoc-a", 1, "滤波器设计关注通带与阻带。", 2),
        _chunk("kbdoc-b", 0, "植物学讲义：光合作用。", 1),
    ]
    embedder = HashEmbedder()
    engine, index_dir = await _build(tmp_path, chunks, embedder)

    hits = await engine.query(
        index_dir, chunks[0].text, kb_id="kb-1", top_k=3, mode="vector", embedder=embedder
    )
    assert hits
    assert hits[0].doc_id == "kbdoc-a"
    assert hits[0].page == 1
    assert hits[0].kb_id == "kb-1"


async def test_hybrid_differs_from_pure_vector(tmp_path):
    """验收 B：混合检索与纯向量结果不同且都合理。"""
    chunks = [
        _chunk("kbdoc-a", 0, "傅里叶变换。", 1),
        _chunk("kbdoc-b", 0, "时域、频域、频谱、滤波、信号都在讲同一件事。", 2),
        _chunk("kbdoc-c", 0, "植物学：光合作用转化光能。", 3),
    ]
    embedder = TopicEmbedder()
    engine, index_dir = await _build(tmp_path, chunks, embedder)
    # 词面上只有 b 沾到「信号」，但语义上 a/b 同主题——两条通路各有所好
    query = "怎么把信号拆开看"

    vector_hits = await engine.query(
        index_dir, query, kb_id="kb-1", top_k=2, mode="vector", embedder=embedder
    )
    hybrid_hits = await engine.query(
        index_dir, query, kb_id="kb-1", top_k=2, mode="hybrid", embedder=embedder
    )

    assert [hit.doc_id for hit in vector_hits] == ["kbdoc-a", "kbdoc-b"]
    assert [hit.doc_id for hit in hybrid_hits] == ["kbdoc-b", "kbdoc-a"]
    assert [hit.doc_id for hit in vector_hits] != [hit.doc_id for hit in hybrid_hits]
    # 无关文档两边都不该出现
    assert "kbdoc-c" not in {hit.doc_id for hit in vector_hits + hybrid_hits}


async def test_excluded_docs_filtered_in_both_modes(tmp_path):
    chunks = [
        _chunk("kbdoc-a", 0, "傅里叶变换。", 1),
        _chunk("kbdoc-b", 0, "傅里叶变换的应用。", 2),
    ]
    embedder = HashEmbedder()
    engine, index_dir = await _build(tmp_path, chunks, embedder)
    excluded = frozenset({"kbdoc-a"})

    vector_hits = await engine.query(
        index_dir,
        "傅里叶变换",
        kb_id="kb-1",
        top_k=5,
        mode="vector",
        excluded_docs=excluded,
        embedder=embedder,
    )
    hybrid_hits = await engine.query(
        index_dir,
        "傅里叶变换",
        kb_id="kb-1",
        top_k=5,
        mode="hybrid",
        excluded_docs=excluded,
        embedder=embedder,
    )
    assert {hit.doc_id for hit in vector_hits} == {"kbdoc-b"}
    assert {hit.doc_id for hit in hybrid_hits} == {"kbdoc-b"}


async def test_dim_mismatch_raises(tmp_path):
    chunks = [_chunk("kbdoc-a", 0, "傅里叶变换。", 1)]
    engine, index_dir = await _build(tmp_path, chunks, HashEmbedder(dim=64))
    with pytest.raises(IndexDimMismatchError):
        await engine.query(
            index_dir,
            "傅里叶变换",
            kb_id="kb-1",
            top_k=3,
            mode="vector",
            embedder=HashEmbedder(dim=32),
        )


async def test_empty_index_returns_no_hits(tmp_path):
    engine = VectorEngine()
    index_dir = tmp_path / "version-1"
    await engine.write_index(
        index_dir, chunks=[], vectors=np.zeros((0, 8), dtype=np.float32), dim=8
    )
    hits = await engine.query(
        index_dir, "任意", kb_id="kb-1", top_k=3, mode="hybrid", embedder=HashEmbedder(dim=8)
    )
    assert hits == []


async def test_index_is_cached_until_rewritten(tmp_path):
    """进程内缓存：同一目录重复查询不重读；重写后自动换新。"""
    chunks = [_chunk("kbdoc-a", 0, "第一版内容。", 1)]
    embedder = HashEmbedder()
    engine, index_dir = await _build(tmp_path, chunks, embedder)
    first = engine.load(index_dir)
    assert engine.load(index_dir) is first  # 命中缓存

    chunks2 = [*chunks, _chunk("kbdoc-b", 0, "第二版新增内容。", 1)]
    vectors = np.asarray(await embedder.embed([c.text for c in chunks2]), dtype=np.float32)
    await engine.write_index(index_dir, chunks=chunks2, vectors=vectors, dim=embedder.dim)
    assert engine.load(index_dir) is not first  # 重写后换新
    assert len(engine.load(index_dir).chunks) == 2
