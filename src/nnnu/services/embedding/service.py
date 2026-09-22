"""嵌入服务（§7.9）：provider 解析 + 分批 + 进度 + 取消 + 限流重试。

三种 provider，按同一契约（base.EmbeddingProvider）互换：

- ``local``（默认）：本地 ONNX bge-small-zh-v1.5，首次用时惰性下载模型；
- ``remote``：任一套 OpenAI 兼容 /embeddings 端点，密钥走 user-secrets
  的 "embedding" 域（与 search/llm 同一套：密钥永不进设置 JSON）；
- ``scripted``：env NNNU_EMBEDDING_MOCK=scripted 时的离线替身（E2E/演示）。

批量切分、批间取消、429/5xx/连接错误退避重试都在这一层做，provider 只管
「一批文本进、一批向量出」。重试比 llm 的 with_retries 多覆盖两类错误：
嵌入往往是长任务（几百个 chunk），一次网络抖动不该让整个 KB 构建失败。
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from threading import Event
from typing import Callable

import httpx

from nnnu.services.embedding.base import (
    BuildCancelled,
    EmbeddingError,
    EmbeddingProvider,
)
from nnnu.services.embedding.downloader import MODEL_NAME, ensure_model, model_dir
from nnnu.services.embedding.local_onnx import DEFAULT_DIM, OnnxEmbeddingProvider
from nnnu.services.embedding.remote import RemoteEmbeddingProvider
from nnnu.services.embedding.scripted import ScriptedEmbedder
from nnnu.services.llm.errors import LLMError

logger = logging.getLogger(__name__)

ENV_MOCK = "NNNU_EMBEDDING_MOCK"
ENV_API_KEY = "EMBEDDING_API_KEY"
SECRET_DOMAIN = "embedding"

DEFAULT_BATCH_SIZE = 32
DEFAULT_REMOTE_MODEL = "bge-small-zh-v1.5"

RETRY_ATTEMPTS = 3
RETRY_BASE_DELAY_S = 0.5
RETRY_MAX_DELAY_S = 8.0

ProgressFn = Callable[[int, int], None]  # (已完成, 总数)：批数，或下载字节数
NoteFn = Callable[[str], None]  # 给人看的一句话（"正在下载嵌入模型 42/95 MB"）


@dataclass(frozen=True, slots=True)
class EmbeddingConfig:
    provider: str  # "local" | "remote" | "scripted"
    model: str
    base_url: str | None
    api_key: str | None
    batch_size: int


def resolve_config() -> EmbeddingConfig:
    """设置 models 区（embedding_*）> env > 默认本地。读不到就回退默认，不抛。"""
    batch_size = _configured_batch_size()
    if os.environ.get(ENV_MOCK) == "scripted":
        return EmbeddingConfig("scripted", ScriptedEmbedder.label, None, None, batch_size)

    values = _read_models_area()
    raw_provider = str(values.get("embedding_provider") or "").strip()
    base_url = str(values.get("embedding_base_url") or "").strip().rstrip("/") or None
    model = str(values.get("embedding_model") or "").strip() or MODEL_NAME
    if raw_provider != "remote":
        return EmbeddingConfig("local", MODEL_NAME, None, None, batch_size)

    api_key = _read_api_key(raw_provider)
    return EmbeddingConfig("remote", model, base_url, api_key, batch_size)


def _read_models_area() -> dict[str, object]:
    from nnnu.services.settings.service import get_settings_service

    try:
        return dict(get_settings_service().load_area("models"))
    except KeyError:  # pragma: no cover - models 区在 P2 就落地了
        return {}


def _configured_batch_size() -> int:
    """kb 区的 embedding_batch_size（设置卡在 P4 提交 6 落地，之前用默认）。"""
    from nnnu.services.settings.service import get_settings_service

    try:
        values = get_settings_service().load_area("kb")
    except KeyError:
        return DEFAULT_BATCH_SIZE
    try:
        return max(1, int(values.get("embedding_batch_size") or DEFAULT_BATCH_SIZE))
    except (TypeError, ValueError):
        return DEFAULT_BATCH_SIZE


def _read_api_key(raw_provider: str) -> str | None:
    """user-secrets 槽名与设置草稿流对齐（空 provider → "default" 槽）。"""
    from nnnu.services.secrets.store import get_secrets_store

    api_key = get_secrets_store().get(SECRET_DOMAIN, raw_provider or "default")
    if api_key is None:
        api_key = os.environ.get(ENV_API_KEY) or None
    return api_key


_stub_factory: Callable[[], EmbeddingProvider] | None = None
_embedding_service: EmbeddingService | None = None


def install_embedding_stub(factory_fn: Callable[[], EmbeddingProvider]) -> None:
    """测试注入：每次取 provider 用 factory_fn（§12.1，对齐 llm install_scripted）。"""
    global _stub_factory
    _stub_factory = factory_fn


def uninstall_embedding_stub() -> None:
    global _stub_factory
    _stub_factory = None


class EmbeddingService:
    """对外只有两件事：signature()（索引要记它）和 embed_batch()。"""

    def __init__(
        self,
        *,
        batch_size: int | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._batch_size_override = batch_size
        self._transport = transport
        self._provider: EmbeddingProvider | None = None
        self._provider_key: tuple[str, str, str, str | None] | None = None
        self._env_scripted: ScriptedEmbedder | None = None

    # ---------- 身份 ----------

    def signature(self) -> str:
        """索引 meta 里记的配置身份：换模型/换端点 = 换签名 = 得重建索引。

        维度不进签名：远程端点的维度要问一次才知道，而「配置没变」是常量判断。
        维度单独比对（见 VectorEngine），这样签名不会因为「还没调过」而抖动。
        """
        if _stub_factory is not None:
            return _stub_provider().label
        config = resolve_config()
        if config.provider == "scripted":
            return ScriptedEmbedder.label
        if config.provider == "remote":
            return f"remote:{config.base_url}:{config.model}"
        return f"local:{MODEL_NAME}:{DEFAULT_DIM}"

    def describe(self) -> str:
        """给日志/接口看的一句话（不含密钥）。"""
        config = resolve_config()
        if config.provider == "remote":
            return f"远程嵌入 {config.base_url}（{config.model}）"
        if config.provider == "scripted":
            return "离线替身嵌入（NNNU_EMBEDDING_MOCK）"
        return f"本地嵌入 {MODEL_NAME}（首次使用会下载模型）"

    # ---------- 嵌入 ----------

    async def embed_batch(
        self,
        texts: list[str],
        *,
        on_progress: ProgressFn | None = None,
        on_note: NoteFn | None = None,
        cancel: Event | None = None,
    ) -> list[list[float]]:
        """一批文本 → 一批向量；批间查取消，失败按可重试性退避重试。"""
        if not texts:
            return []
        provider = await self._resolve_provider(
            on_progress=on_progress, on_note=on_note, cancel=cancel
        )
        size = self._batch_size_override or resolve_config().batch_size
        size = max(1, size)
        vectors: list[list[float]] = []
        total = len(texts)
        for start in range(0, total, size):
            _check_cancel(cancel)
            batch = texts[start : start + size]
            vectors.extend(await self._embed_retrying(provider, batch, cancel=cancel))
            if on_progress is not None:
                on_progress(min(start + len(batch), total), total)
        if len(vectors) != total:
            raise EmbeddingError(f"嵌入返回 {len(vectors)} 条，请求了 {total} 条")
        return vectors

    async def provider(self) -> EmbeddingProvider:
        """给检索引擎用的 provider：查询是一次性的，不走分批/进度/取消。

        （索引构建走 embed_batch——批次与进度是构建侧才需要的语义。）
        """
        return await self._resolve_provider(on_progress=None, on_note=None, cancel=None)

    async def _embed_retrying(
        self, provider: EmbeddingProvider, batch: list[str], *, cancel: Event | None
    ) -> list[list[float]]:
        attempt = 0
        while True:
            try:
                return await provider.embed(batch)
            except BuildCancelled:
                raise
            except (httpx.HTTPError, LLMError, EmbeddingError) as exc:
                if attempt >= RETRY_ATTEMPTS - 1 or not _is_retryable(exc):
                    raise _as_embedding_error(exc) from exc
                delay = _retry_delay(exc, attempt)
                logger.warning(
                    "嵌入失败（%s），%.1fs 后第 %d/%d 次重试",
                    exc,
                    delay,
                    attempt + 1,
                    RETRY_ATTEMPTS,
                )
                attempt += 1
                await asyncio.sleep(delay)
                _check_cancel(cancel)

    # ---------- provider ----------

    async def _resolve_provider(
        self,
        *,
        on_progress: ProgressFn | None,
        on_note: NoteFn | None,
        cancel: Event | None,
    ) -> EmbeddingProvider:
        if _stub_factory is not None:
            return _stub_provider()
        config = resolve_config()
        key = (config.provider, config.model, config.base_url or "", config.api_key)
        if self._provider is not None and self._provider_key == key:
            return self._provider
        if self._provider is not None:
            await self._close_provider()
        self._provider = await self._build_provider(
            config, on_progress=on_progress, on_note=on_note, cancel=cancel
        )
        self._provider_key = key
        return self._provider

    async def _build_provider(
        self,
        config: EmbeddingConfig,
        *,
        on_progress: ProgressFn | None,
        on_note: NoteFn | None,
        cancel: Event | None,
    ) -> EmbeddingProvider:
        if config.provider == "scripted":
            if self._env_scripted is None:
                self._env_scripted = ScriptedEmbedder()
            return self._env_scripted
        if config.provider == "remote":
            if not config.base_url:
                raise EmbeddingError("远程嵌入需要在设置页填 Base URL（要带 /v1）")
            if not config.api_key:
                raise EmbeddingError("远程嵌入缺少 API Key，请在设置页填写")
            logger.info("嵌入远端就绪：%s（%s）", config.base_url, config.model)
            return RemoteEmbeddingProvider(
                base_url=config.base_url,
                model=config.model,
                api_key=config.api_key,
                transport=self._transport,
            )
        provider = OnnxEmbeddingProvider(model_dir())
        if not provider.model_path.is_file():
            await self._download_model(on_progress=on_progress, on_note=on_note, cancel=cancel)
        await asyncio.to_thread(provider.load)
        return provider

    async def _download_model(
        self,
        *,
        on_progress: ProgressFn | None,
        on_note: NoteFn | None,
        cancel: Event | None,
    ) -> None:
        """首次使用本地嵌入：下模型（约 95MB），进度按字节报。"""

        def on_bytes(done: int, total: int) -> None:
            if on_note is not None:
                on_note(f"正在下载嵌入模型 {done // (1 << 20)}/{max(total, done) // (1 << 20)} MB")
            if on_progress is not None:
                on_progress(done, max(total, done))

        if on_note is not None:
            on_note(f"正在下载嵌入模型 {MODEL_NAME}（约 95MB，只需一次）")
        logger.info("首次使用本地嵌入，开始下载模型 %s", MODEL_NAME)
        await ensure_model(on_progress=on_bytes, cancel=cancel)

    async def _close_provider(self) -> None:
        provider = self._provider
        self._provider = None
        self._provider_key = None
        close = getattr(provider, "aclose", None)
        if callable(close):
            try:
                await close()
            except Exception:  # pragma: no cover - 关闭失败不该影响主流程
                logger.warning("嵌入 provider 关闭失败", exc_info=True)

    async def aclose(self) -> None:
        await self._close_provider()


def _stub_provider() -> EmbeddingProvider:
    if _stub_factory is None:  # pragma: no cover - 调用点都先判过
        raise EmbeddingError("未注入嵌入替身")
    return _stub_factory()


def _check_cancel(cancel: Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise BuildCancelled("索引构建被取消")


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, LLMError):
        return bool(exc.retryable)
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, EmbeddingError):
        return True  # 本地推理/端点返回异常，多半是暂时性的
    return False


def _retry_delay(exc: Exception, attempt: int) -> float:
    retry_after = getattr(exc, "retry_after", None)
    if isinstance(retry_after, (int, float)) and retry_after > 0:
        return min(float(retry_after), RETRY_MAX_DELAY_S)
    return min(RETRY_BASE_DELAY_S * (2**attempt), RETRY_MAX_DELAY_S)


def _as_embedding_error(exc: Exception) -> EmbeddingError:
    if isinstance(exc, EmbeddingError):
        return exc
    return EmbeddingError(f"嵌入失败：{exc}")


def get_embedding_service() -> EmbeddingService:
    """进程级单例；lifespan 未装配时按需自建（测试/脚本友好）。"""
    global _embedding_service
    if _embedding_service is None:
        _embedding_service = EmbeddingService()
    return _embedding_service


def set_embedding_service(service: EmbeddingService | None) -> None:
    global _embedding_service
    _embedding_service = service


def reset_embedding_service() -> None:
    """测试用：解除装配。"""
    global _embedding_service
    _embedding_service = None
