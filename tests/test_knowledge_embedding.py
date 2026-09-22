"""嵌入服务单测（§7.9）：分词器、替身、远程端点、重试、模型下载。

不打真实端点、不加载真模型：远程用 httpx MockTransport，本地用假 provider
验证「先下模型再加载」的接线，真模型的语义质量由手工冒烟承担。
"""

import hashlib
import json
import math
from pathlib import Path
from threading import Event

import httpx
import pytest

from nnnu.services.embedding import service as embedding_service
from nnnu.services.embedding.base import BuildCancelled, EmbeddingError
from nnnu.services.embedding.downloader import ensure_model
from nnnu.services.embedding.local_onnx import WordPieceTokenizer
from nnnu.services.embedding.remote import RemoteEmbeddingProvider
from nnnu.services.embedding.scripted import ScriptedEmbedder, embed_text
from nnnu.services.embedding.service import EmbeddingService, resolve_config
from nnnu.services.llm.errors import LLMRateLimitError

VOCAB = [
    "[PAD]",
    "[UNK]",
    "[CLS]",
    "[SEP]",
    "的",
    "傅",
    "里",
    "叶",
    "变",
    "换",
    "##里",
    "fourier",
    "##s",
    "cafe",
]


@pytest.fixture(autouse=True)
def _clean_extras():
    """每个用例后清注入的替身与单例，别串味到下一个用例。"""
    yield
    embedding_service.uninstall_embedding_stub()
    embedding_service.reset_embedding_service()


def _vocab_file(tmp_path: Path) -> Path:
    path = tmp_path / "vocab.txt"
    path.write_text("\n".join(VOCAB) + "\n", encoding="utf-8")
    return path


# ---------- WordPiece ----------


def test_tokenizer_cjk_per_char_and_ids(tmp_path):
    tokenizer = WordPieceTokenizer.from_file(_vocab_file(tmp_path))
    ids, mask = tokenizer.encode("傅里叶变换")
    assert ids == [2, 5, 6, 7, 8, 9, 3]  # [CLS] 傅 里 叶 变 换 [SEP]
    assert mask == [1] * 7


def test_tokenizer_wordpiece_continuation_and_unknown(tmp_path):
    tokenizer = WordPieceTokenizer.from_file(_vocab_file(tmp_path))
    # fourier 整体在词表 → 直接命中；fouriers 走 fourier + ##s
    assert tokenizer.encode("fourier")[0] == [2, 11, 3]
    assert tokenizer.encode("fouriers")[0] == [2, 11, 12, 3]
    assert tokenizer.encode("龘")[0] == [2, 1, 3]  # 词表里没有的字 → [UNK]


def test_tokenizer_lowercases_and_strips_accents(tmp_path):
    tokenizer = WordPieceTokenizer.from_file(_vocab_file(tmp_path))
    assert tokenizer.encode("FOURIER")[0] == [2, 11, 3]
    assert tokenizer.encode("café")[0] == [2, 13, 3]  # 重音剥掉后命中 cafe


def test_tokenizer_truncates_to_max_length(tmp_path):
    tokenizer = WordPieceTokenizer.from_file(_vocab_file(tmp_path), max_length=5)
    ids, _ = tokenizer.encode("傅里叶变换")
    assert len(ids) == 5
    assert ids[0] == 2 and ids[-1] == 3


def test_tokenizer_empty_vocab_raises(tmp_path):
    empty = tmp_path / "empty.txt"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(EmbeddingError):
        WordPieceTokenizer.from_file(empty)


def test_encode_batch_pads_to_longest(tmp_path):
    tokenizer = WordPieceTokenizer.from_file(_vocab_file(tmp_path))
    ids_matrix, mask_matrix = tokenizer.encode_batch(["傅", "傅里叶变换"])
    assert len(ids_matrix) == 2
    assert {len(row) for row in ids_matrix} == {7}
    assert mask_matrix[0] == [1, 1, 1, 0, 0, 0, 0]  # 短的那条右补 0


# ---------- 离线替身 ----------


def test_scripted_embedder_is_deterministic_and_normalized():
    first = embed_text("傅里叶变换把时域信号分解为频域分量")
    again = embed_text("傅里叶变换把时域信号分解为频域分量")
    assert first == again
    assert abs(math.sqrt(sum(value * value for value in first)) - 1.0) < 1e-9
    other = embed_text("植物学：光合作用")
    assert first != other


def test_scripted_overlap_scores_higher_than_unrelated():
    """替身也得有「词面重叠 → 余弦更高」的性质，否则 E2E 断言没意义。"""

    def cosine(left: list[float], right: list[float]) -> float:
        return sum(a * b for a, b in zip(left, right))

    query = embed_text("傅里叶变换")
    near = cosine(query, embed_text("傅里叶变换把信号分解为频率分量"))
    far = cosine(query, embed_text("今天中午吃什么好呢"))
    assert near > far


def test_scripted_empty_text_is_not_zero_vector():
    vector = embed_text("")
    assert abs(math.sqrt(sum(value * value for value in vector)) - 1.0) < 1e-9


async def test_scripted_embedder_contract():
    embedder = ScriptedEmbedder()
    vectors = await embedder.embed(["甲", "乙"])
    assert embedder.dim == 512
    assert len(vectors) == 2 and all(len(vector) == 512 for vector in vectors)


# ---------- 远程端点 ----------


def _vector_for(index: int, dim: int) -> list[float]:
    """每条向量方向不同（同一方向归一化后分不出来，乱序测试就没意义了）。"""
    return [float(index + 1), float(dim - index)] + [0.0] * (dim - 2)


def _remote_transport(*, dim: int = 4, reverse: bool = False, calls: list[dict] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if calls is not None:
            calls.append(
                {
                    "url": str(request.url),
                    "payload": payload,
                    "auth": request.headers.get("authorization"),
                }
            )
        items = [
            {"index": index, "embedding": _vector_for(index, dim)}
            for index in range(len(payload["input"]))
        ]
        if reverse:
            items.reverse()
        return httpx.Response(200, json={"data": items})

    return httpx.MockTransport(handler)


async def test_remote_provider_calls_embeddings_and_normalizes():
    calls: list[dict] = []
    provider = RemoteEmbeddingProvider(
        base_url="https://api.example.com/v1",
        model="bge-m3",
        api_key="sk-test",
        transport=_remote_transport(calls=calls),
    )
    vectors = await provider.embed(["甲", "乙"])
    await provider.aclose()

    assert calls[0]["url"] == "https://api.example.com/v1/embeddings"
    assert calls[0]["payload"]["model"] == "bge-m3"
    assert calls[0]["payload"]["input"] == ["甲", "乙"]
    assert calls[0]["auth"] == "Bearer sk-test"
    assert provider.dim == 4
    assert all(abs(math.sqrt(sum(v * v for v in vec)) - 1.0) < 1e-9 for vec in vectors)


async def test_remote_provider_restores_order_by_index():
    provider = RemoteEmbeddingProvider(
        base_url="https://api.example.com/v1",
        model="bge-m3",
        api_key="sk-test",
        transport=_remote_transport(reverse=True),
    )
    vectors = await provider.embed(["甲", "乙"])
    await provider.aclose()
    # 服务端倒序返回：还原后第 0 条还是第 0 条（[1,4] 归一化首元素 1/√17）
    assert vectors[0][0] == pytest.approx(1 / math.sqrt(17))
    assert vectors[1][0] == pytest.approx(2 / math.sqrt(13))


async def test_remote_provider_rejects_ragged_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [{"index": 0, "embedding": [1.0, 0.0]}, {"index": 1, "embedding": [1.0]}]
            },
        )

    provider = RemoteEmbeddingProvider(
        base_url="https://api.example.com/v1",
        model="m",
        api_key="k",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(EmbeddingError):
        await provider.embed(["甲", "乙"])
    await provider.aclose()


# ---------- 服务层：分批 / 进度 / 取消 / 重试 ----------


class _CountingProvider:
    label = "stub:counting"
    dim = 3

    def __init__(self, *, failures: int = 0, error: Exception | None = None) -> None:
        self.calls = 0
        self.failures = failures
        self.error = error or EmbeddingError("端点抖动")

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error
        return [[1.0, 0.0, 0.0] for _ in texts]


async def test_service_batches_and_reports_progress(monkeypatch):
    provider = _CountingProvider()
    embedding_service.install_embedding_stub(lambda: provider)
    service = EmbeddingService(batch_size=2)
    seen: list[tuple[int, int]] = []

    vectors = await service.embed_batch(
        [f"第{i}段" for i in range(5)], on_progress=lambda d, t: seen.append((d, t))
    )

    assert len(vectors) == 5
    assert provider.calls == 3  # 2 + 2 + 1
    assert seen == [(2, 5), (4, 5), (5, 5)]


async def test_service_cancel_before_first_batch():
    provider = _CountingProvider()
    embedding_service.install_embedding_stub(lambda: provider)
    cancel = Event()
    cancel.set()
    with pytest.raises(BuildCancelled):
        await EmbeddingService().embed_batch(["甲"], cancel=cancel)
    assert provider.calls == 0


async def test_service_retries_transient_failure(monkeypatch):
    monkeypatch.setattr(embedding_service, "RETRY_BASE_DELAY_S", 0.0)
    provider = _CountingProvider(failures=2)
    embedding_service.install_embedding_stub(lambda: provider)

    vectors = await EmbeddingService().embed_batch(["甲"])
    assert provider.calls == 3
    assert vectors == [[1.0, 0.0, 0.0]]


async def test_service_gives_up_after_attempts(monkeypatch):
    monkeypatch.setattr(embedding_service, "RETRY_BASE_DELAY_S", 0.0)
    provider = _CountingProvider(failures=99)
    embedding_service.install_embedding_stub(lambda: provider)

    with pytest.raises(EmbeddingError):
        await EmbeddingService().embed_batch(["甲"])
    assert provider.calls == embedding_service.RETRY_ATTEMPTS


async def test_service_does_not_retry_auth_error(monkeypatch):
    """密钥错重试三次只是白等——不可重试的错误立刻抛。"""
    from nnnu.services.llm.errors import LLMAuthenticationError

    monkeypatch.setattr(embedding_service, "RETRY_BASE_DELAY_S", 0.0)
    provider = _CountingProvider(failures=99, error=LLMAuthenticationError("401 密钥无效"))
    embedding_service.install_embedding_stub(lambda: provider)

    with pytest.raises(EmbeddingError):
        await EmbeddingService().embed_batch(["甲"])
    assert provider.calls == 1


def test_retry_delay_honors_retry_after_with_cap():
    assert embedding_service._retry_delay(LLMRateLimitError("429", retry_after=3.0), 0) == 3.0
    assert (
        embedding_service._retry_delay(LLMRateLimitError("429", retry_after=999.0), 0)
        == embedding_service.RETRY_MAX_DELAY_S
    )
    # 没有 Retry-After 就指数退避
    assert (
        embedding_service._retry_delay(EmbeddingError("x"), 0)
        == embedding_service.RETRY_BASE_DELAY_S
    )
    assert embedding_service._retry_delay(EmbeddingError("x"), 3) == min(
        embedding_service.RETRY_BASE_DELAY_S * 8, embedding_service.RETRY_MAX_DELAY_S
    )


async def test_service_returns_empty_for_empty_input():
    provider = _CountingProvider()
    embedding_service.install_embedding_stub(lambda: provider)
    assert await EmbeddingService().embed_batch([]) == []
    assert provider.calls == 0


# ---------- 配置解析 ----------


def _write_models_settings(tmp_home, values: dict) -> None:
    path = tmp_home / "data" / "user" / "settings" / "models.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")


def test_config_defaults_to_local(tmp_home):
    config = resolve_config()
    assert config.provider == "local"
    assert config.batch_size == embedding_service.DEFAULT_BATCH_SIZE


def test_config_remote_reads_settings(tmp_home):
    _write_models_settings(
        tmp_home,
        {
            "embedding_provider": "remote",
            "embedding_base_url": "https://api.example.com/v1/",
            "embedding_model": "bge-m3",
        },
    )
    config = resolve_config()
    assert config.provider == "remote"
    assert config.base_url == "https://api.example.com/v1"  # 尾斜杠归一
    assert config.model == "bge-m3"


def test_config_mock_env_wins(tmp_home, monkeypatch):
    monkeypatch.setenv("NNNU_EMBEDDING_MOCK", "scripted")
    assert resolve_config().provider == "scripted"
    assert EmbeddingService().signature() == "stub:scripted"


def test_signature_distinguishes_local_and_remote(tmp_home):
    assert EmbeddingService().signature().startswith("local:bge-small-zh-v1.5")
    _write_models_settings(
        tmp_home,
        {
            "embedding_provider": "remote",
            "embedding_base_url": "https://x/v1",
            "embedding_model": "m",
        },
    )
    assert EmbeddingService().signature() == "remote:https://x/v1:m"


async def test_remote_without_key_raises_actionable_error(tmp_home, monkeypatch):
    monkeypatch.delenv(embedding_service.ENV_API_KEY, raising=False)
    _write_models_settings(
        tmp_home,
        {
            "embedding_provider": "remote",
            "embedding_base_url": "https://x/v1",
            "embedding_model": "m",
        },
    )
    with pytest.raises(EmbeddingError, match="API Key"):
        await EmbeddingService().embed_batch(["甲"])


# ---------- 本地 provider 接线（不加载真模型） ----------


class _FakeOnnx:
    def __init__(self, model_dir: Path) -> None:
        self.model_dir = Path(model_dir)
        self.dim = 4
        self.loaded = False

    @property
    def model_path(self) -> Path:
        return self.model_dir / "model.onnx"

    def load(self) -> None:
        self.loaded = True

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


async def test_local_provider_downloads_once_then_reuses(tmp_home, monkeypatch):
    downloads: list[str] = []
    created: list[_FakeOnnx] = []

    async def fake_ensure_model(*, on_progress=None, cancel=None, **kwargs):
        downloads.append("download")
        target = tmp_home / "data" / "user" / "knowledge" / "models" / "bge-small-zh-v1.5"
        target.mkdir(parents=True, exist_ok=True)
        (target / "model.onnx").write_bytes(b"fake")
        (target / "vocab.txt").write_text("x\n", encoding="utf-8")
        if on_progress is not None:
            on_progress(10, 10)
        return target

    def fake_provider(model_dir):
        instance = _FakeOnnx(model_dir)
        created.append(instance)
        return instance

    monkeypatch.setattr(embedding_service, "ensure_model", fake_ensure_model)
    monkeypatch.setattr(embedding_service, "OnnxEmbeddingProvider", fake_provider)

    notes: list[str] = []
    service = EmbeddingService()
    await service.embed_batch(["甲", "乙"], on_note=notes.append)
    await service.embed_batch(["丙"])

    assert downloads == ["download"]  # 第二次复用已建好的 provider
    assert len(created) == 1 and created[0].loaded
    assert any("下载" in note for note in notes)


# ---------- 模型下载器 ----------


def _payload(file_bytes: dict[str, bytes], calls: list[str], *, fail_first: bool = False):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        if fail_first and "modelscope" in url:
            return httpx.Response(500, content=b"boom")
        for name, content in file_bytes.items():
            if name in url:
                return httpx.Response(
                    200, content=content, headers={"content-length": str(len(content))}
                )
        return httpx.Response(404, content=b"no")

    return httpx.MockTransport(handler)


async def test_downloader_fetches_files_and_writes_meta(tmp_path):
    files = {"model.onnx": b"ONNX" * 1000, "vocab.txt": b"[PAD]\n[CLS]\n"}
    calls: list[str] = []
    dest = tmp_path / "model"
    await ensure_model(dest, transport=_payload(files, calls))

    assert (dest / "model.onnx").read_bytes() == files["model.onnx"]
    meta = json.loads((dest / "meta.json").read_text(encoding="utf-8"))
    recorded = meta["files"]["model.onnx"]
    assert recorded["size"] == len(files["model.onnx"])
    assert recorded["sha256"] == hashlib.sha256(files["model.onnx"]).hexdigest()
    assert not list(dest.glob("*.part"))

    before = len(calls)
    await ensure_model(dest, transport=_payload(files, calls))
    assert len(calls) == before  # 校验通过就不重下


async def test_downloader_redownloads_corrupted_file(tmp_path):
    files = {"model.onnx": b"ONNX" * 1000, "vocab.txt": b"[PAD]\n"}
    calls: list[str] = []
    dest = tmp_path / "model"
    await ensure_model(dest, transport=_payload(files, calls))

    (dest / "model.onnx").write_bytes(b"truncated")
    await ensure_model(dest, transport=_payload(files, calls))
    assert (dest / "model.onnx").read_bytes() == files["model.onnx"]


async def test_downloader_falls_back_to_second_source(tmp_path):
    files = {"model.onnx": b"ONNX" * 10, "vocab.txt": b"v"}
    calls: list[str] = []
    dest = tmp_path / "model"
    await ensure_model(dest, transport=_payload(files, calls, fail_first=True))

    assert (dest / "model.onnx").read_bytes() == files["model.onnx"]
    assert any("modelscope" in url for url in calls)
    assert any("hf-mirror" in url for url in calls)


async def test_downloader_cancel_leaves_no_partial_file(tmp_path):
    files = {"model.onnx": b"ONNX" * 1000, "vocab.txt": b"v"}
    cancel = Event()
    cancel.set()
    dest = tmp_path / "model"
    with pytest.raises(BuildCancelled):
        await ensure_model(dest, transport=_payload(files, []), cancel=cancel)
    assert not (dest / "model.onnx").exists()
    assert not list(dest.glob(".part")) and not list(dest.glob("*.part"))


async def test_downloader_reports_progress(tmp_path):
    files = {"model.onnx": b"ONNX" * 1000, "vocab.txt": b"v"}
    seen: list[tuple[int, int]] = []
    dest = tmp_path / "model"
    await ensure_model(
        dest, transport=_payload(files, []), on_progress=lambda d, t: seen.append((d, t))
    )

    assert seen
    assert seen[-1][0] == len(files["model.onnx"]) + len(files["vocab.txt"])
    assert all(done <= total for done, total in seen)


async def test_downloader_all_sources_fail_raises(tmp_path):
    from nnnu.services.embedding.base import ModelDownloadError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"down")

    dest = tmp_path / "model"
    with pytest.raises(ModelDownloadError):
        await ensure_model(dest, transport=httpx.MockTransport(handler))
    assert not (dest / "model.onnx").exists()
