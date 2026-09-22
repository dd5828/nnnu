"""本地嵌入模型下载（§7.9）：惰性、可续、双源兜底。

为什么不用 huggingface_hub / modelscope SDK：多一个重依赖只为下两个文件
不划算，而且这两个源、这两个文件的位置是固定的。源顺序：

1. ModelScope 的 Xenova/bge-small-zh-v1.5 镜像（国内可达性好）；
2. hf-mirror.com 同仓库（兜底）；
3. env NNNU_EMBEDDING_MODEL_URL 手工指定（内网源/离线包，带 {path} 模板
   或直接给目录前缀都行）。

注意：官方 BAAI/bge-small-zh-v1.5 仓库**没有** onnx 目录（实测 404），
所以用 Xenova 的转换版——权重数值同源，只是导出成了 onnx。

落盘协议：写 <name>.part → fsync → rename 覆盖；sha256 首次下载后记进
meta.json，之后每次校验（截断/损坏能发现并重下）。崩溃只会留下 .part，
不会留下半个正式文件。
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Callable

import httpx

from nnnu.services.embedding.base import BuildCancelled, ModelDownloadError

logger = logging.getLogger(__name__)

MODEL_REPO = "Xenova/bge-small-zh-v1.5"
MODEL_NAME = "bge-small-zh-v1.5"
ENV_MODEL_URL = "NNNU_EMBEDDING_MODEL_URL"

DOWNLOAD_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0)
CHUNK_BYTES = 1 << 16  # 64KB

# (远端路径, 本地文件名, 期望字节数或 None)
REMOTE_FILES: tuple[tuple[str, str, int | None], ...] = (
    ("onnx/model.onnx", "model.onnx", 94_851_877),
    ("vocab.txt", "vocab.txt", None),
)

DEFAULT_SOURCES: tuple[str, ...] = (
    "https://modelscope.cn/api/v1/models/{repo}/repo?FilePath={path}",
    "https://hf-mirror.com/{repo}/resolve/main/{path}",
)

ProgressFn = Callable[[int, int], None]  # (已下载字节, 预估总字节)


@dataclass(slots=True)
class _FileRecord:
    sha256: str
    size: int


def model_dir(home: str | Path | None = None) -> Path:
    """本地模型落在这里；与 KB 索引解耦，删了会自动重下。"""
    from nnnu.runtime import home as runtime_home

    return runtime_home.get_data_root(home) / "user" / "knowledge" / "models" / MODEL_NAME


def _sources() -> tuple[str, ...]:
    override = os.environ.get(ENV_MODEL_URL, "").strip()
    if not override:
        return DEFAULT_SOURCES
    if "{path}" in override:
        return (override, *DEFAULT_SOURCES)
    return (override.rstrip("/") + "/{path}", *DEFAULT_SOURCES)


def _url(source: str, remote_path: str) -> str:
    return source.format(repo=MODEL_REPO, path=remote_path)


def _read_meta(dest: Path) -> dict[str, object]:
    meta_path = dest / "meta.json"
    if not meta_path.exists():
        return {}
    import json

    try:
        raw = json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("模型 meta.json 读不出来，按未下载处理：%s", meta_path)
        return {}
    files = raw.get("files") if isinstance(raw, dict) else None
    return files if isinstance(files, dict) else {}


def _write_meta(dest: Path, records: dict[str, _FileRecord]) -> None:
    from nnnu.services.settings.atomic import atomic_write_json

    atomic_write_json(
        dest / "meta.json",
        {
            "repo": MODEL_REPO,
            "files": {
                name: {"sha256": record.sha256, "size": record.size}
                for name, record in sorted(records.items())
            },
        },
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _verify(path: Path, record: object) -> _FileRecord | None:
    """文件在、长度对得上、sha256 对得上才算完整；否则视为需要重下。

    record 为空时（meta.json 缺失但模型文件在，比如手工预置的离线包）
    按「文件存在即认」处理，然后把实际 sha256 补登记进 meta。
    """
    if not path.is_file():
        return None
    size = path.stat().st_size
    if size == 0:
        return None
    if not isinstance(record, dict):
        return _FileRecord(sha256=_sha256_file(path), size=size)
    expected_size = record.get("size")
    if isinstance(expected_size, int) and expected_size != size:
        logger.warning("模型文件 %s 长度不符（%s != %s），重下", path.name, size, expected_size)
        return None
    digest = _sha256_file(path)
    expected_sha = record.get("sha256")
    if isinstance(expected_sha, str) and expected_sha and digest != expected_sha:
        logger.warning("模型文件 %s 校验失败，重下", path.name)
        return None
    return _FileRecord(sha256=digest, size=size)


async def _download_one(
    client: httpx.AsyncClient,
    source: str,
    remote_path: str,
    target: Path,
    *,
    on_bytes: Callable[[int], None] | None,
    cancel: Event | None,
) -> _FileRecord:
    """流式下载单个文件到 target.part 再改名；返回它的记录。"""
    url = _url(source, remote_path)
    part = target.with_name(target.name + ".part")
    digest = hashlib.sha256()
    written = 0
    async with client.stream("GET", url) as response:
        if response.status_code != 200:
            raise ModelDownloadError(f"{url} 返回 {response.status_code}")
        with part.open("wb") as handle:
            async for block in response.aiter_bytes(CHUNK_BYTES):
                if cancel is not None and cancel.is_set():
                    handle.close()
                    part.unlink(missing_ok=True)
                    raise BuildCancelled("模型下载被取消")
                handle.write(block)
                digest.update(block)
                written += len(block)
                if on_bytes is not None:
                    on_bytes(len(block))
            handle.flush()
            os.fsync(handle.fileno())
    if written == 0:
        part.unlink(missing_ok=True)
        raise ModelDownloadError(f"{url} 返回空内容")
    part.replace(target)
    return _FileRecord(sha256=digest.hexdigest(), size=written)


async def ensure_model(
    dest: Path | None = None,
    *,
    on_progress: ProgressFn | None = None,
    cancel: Event | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Path:
    """确保模型文件齐备（缺什么下什么），返回模型目录。

    on_progress(已下载, 预估总字节)：总字节按已知文件大小预估，已完整的
    文件也计入分母，进度条不会从 0 开始跳。
    """
    dest = dest or model_dir()
    dest.mkdir(parents=True, exist_ok=True)
    records = _read_meta(dest)
    pending: list[tuple[str, str]] = []
    known: dict[str, _FileRecord] = {}
    for remote_path, name, _size in REMOTE_FILES:
        verified = _verify(dest / name, records.get(name))
        if verified is None:
            pending.append((remote_path, name))
        else:
            known[name] = verified
    if not pending:
        return dest

    pending_names = {name for _, name in pending}
    done_bytes = sum(record.size for record in known.values())
    total_bytes = done_bytes + sum(
        size or 0 for _, name, size in REMOTE_FILES if name in pending_names
    )
    errors: list[str] = []

    def on_bytes(count: int) -> None:
        nonlocal done_bytes
        done_bytes += count
        if on_progress is not None:
            on_progress(done_bytes, max(total_bytes, done_bytes))

    sources = _sources()
    async with httpx.AsyncClient(
        timeout=DOWNLOAD_TIMEOUT, transport=transport, follow_redirects=True
    ) as client:
        for remote_path, name in pending:
            target = dest / name
            for attempt, source in enumerate(sources):
                try:
                    record = await _download_one(
                        client, source, remote_path, target, on_bytes=on_bytes, cancel=cancel
                    )
                except BuildCancelled:
                    raise
                except (httpx.HTTPError, ModelDownloadError, OSError) as exc:
                    errors.append(f"{source}: {exc}")
                    logger.warning("模型源不可用（%s）：%s", source, exc)
                    if attempt == len(sources) - 1:
                        raise ModelDownloadError(
                            f"模型 {name} 下载失败，所有源都不可用："
                            + "；".join(errors[-len(sources) :])
                        ) from exc
                    continue
                known[name] = record
                break
    _write_meta(dest, known)
    return dest
