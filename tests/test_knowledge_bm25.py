"""BM25 单测（§7.9）：分词、持久化往返、检索正确性、删除过滤。"""

from nnnu.services.rag.base import Chunk
from nnnu.services.rag.bm25 import Bm25Index, tokenize


def test_tokenize_cjk_unigrams_and_bigrams():
    tokens = tokenize("傅里叶变换")
    assert tokens[:5] == ["傅", "里", "叶", "变", "换"]  # 单字保召回
    assert "傅里" in tokens and "变换" in tokens  # 二元组保精度


def test_tokenize_ascii_words_and_mixed():
    tokens = tokenize("BM25 与 bge-small 都要小写")
    assert "bm25" in tokens and "bge" in tokens and "small" in tokens
    assert "要" in tokens and "都要" in tokens  # 汉字仍然展开


def _chunk(doc_id: str, seq: int, text: str) -> Chunk:
    return Chunk(chunk_id=f"{doc_id}#{seq}", doc_id=doc_id, seq=seq, text=text)


def _corpus() -> list[Chunk]:
    return [
        _chunk("kbdoc-a", 0, "傅里叶变换把时域信号分解为不同频率的正弦分量之和。"),
        _chunk("kbdoc-a", 1, "滤波器设计需要关注通带与阻带的边界频率。"),
        _chunk("kbdoc-b", 0, "植物学讲义：光合作用把光能转化为化学能。"),
    ]


def test_search_hits_expected_chunk():
    index = Bm25Index.from_chunks(_corpus())
    hits = index.search("傅里叶变换", top_k=3)
    assert hits, "应命中"
    assert hits[0][0] == "kbdoc-a#0"
    assert all(chunk_id != "kbdoc-b#0" for chunk_id, _ in hits)


def test_search_returns_empty_when_no_term_in_index():
    index = Bm25Index.from_chunks(_corpus())
    assert index.search("qqzzxx", top_k=3) == []  # 词表里没有的 ASCII 词
    assert index.search("", top_k=3) == []


def test_unigram_char_coincidence_scores_weakly():
    """单字召回必然有字符噪声：真正切题的查询要显著压过"碰巧撞字"的查询。

    中文按单字建索引，任何查询都会蹭到几个常见字——这是召回换来的代价；
    靠 IDF 把这些噪声的分数压低（这里用相对分数断言）。
    """
    index = Bm25Index.from_chunks(_corpus())
    real = index.search("傅里叶变换", top_k=1)[0][1]
    noisy = index.search("量子色动力学", top_k=1)
    assert not noisy or noisy[0][1] < real / 3


def test_excluded_docs_are_filtered():
    index = Bm25Index.from_chunks(_corpus())
    hits = index.search("傅里叶变换", top_k=3, excluded_docs=frozenset({"kbdoc-a"}))
    assert all(chunk_id.startswith("kbdoc-b") for chunk_id, _ in hits)


def test_index_json_round_trip():
    index = Bm25Index.from_chunks(_corpus())
    restored = Bm25Index.from_json(index.to_json())
    assert restored.n == index.n
    assert restored.df == index.df
    assert [hit[0] for hit in restored.search("滤波", top_k=2)] == [
        hit[0] for hit in index.search("滤波", top_k=2)
    ]


def test_empty_corpus_is_safe():
    index = Bm25Index.from_chunks([])
    assert index.search("任意查询", top_k=3) == []
