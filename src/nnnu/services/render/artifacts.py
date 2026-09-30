"""渲染产物落盘与读取（§7.7/§7.8）：`<data>/user/renders/<render_id>/` 一份目录。

- 目录名即 render_id（`rnd-` + 8 位 hex，`core/ids.py` 统一发号）；读取先过正则、
  再 resolve 确认没跑出根目录——URL 里的 id 是外部输入，防目录穿越是硬要求；
- 后缀白名单**刻意排除 .svg/.html**：`GET /api/v1/renders/{id}` 是随服务裸奔的
  本机端点，能把任意 HTML/SVG 当同源页面回出去就等于开后门（存储型 XSS）。
  可视化的 svg/html 走正文围栏由前端自渲染（且过 sanitize），不走这里；
- meta.json 记录原始文件名/MIME/种类/尝试次数，无 DB 迁移（P7 决策）。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from nnnu.core.ids import new_id
from nnnu.services.render.models import ArtifactKind, RenderMeta

# 渲染 ID：core/ids.py 的 new_id("rnd") 产物
RENDER_ID_RE = re.compile(r"^rnd-[0-9a-f]{8}$")

# 产物后缀白名单 → MIME（排除 .svg/.html，见模块 docstring）
ARTIFACT_MIME_BY_SUFFIX: dict[str, str] = {
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
}

META_FILENAME = "meta.json"


class RenderStoreError(ValueError):
    """产物落盘被拒（后缀不在白名单/ID 非法等）。"""


class RenderStore:
    """渲染产物的唯一读写口；真渲染器与假渲染器共用。"""

    def __init__(self, data_root: Path) -> None:
        self.root = Path(data_root) / "user" / "renders"

    def new_render_id(self) -> str:
        return new_id("rnd")

    def ensure_dir(self, render_id: str) -> Path:
        """取（并创建）某次渲染的工作目录；非法 ID 直接拒绝。"""
        return self._render_dir(render_id)

    def save(
        self,
        render_id: str,
        source: Path | bytes,
        *,
        filename: str,
        kind: ArtifactKind,
        attempts: int = 1,
    ) -> RenderMeta:
        """把产物写进渲染目录并落 meta.json；后缀不在白名单一律拒绝。"""
        safe_name = Path(filename).name  # 再兜一层：调用方传 ../ 也只会留下文件名
        suffix = Path(safe_name).suffix.lower()
        mime = ARTIFACT_MIME_BY_SUFFIX.get(suffix)
        if mime is None:
            raise RenderStoreError(
                f"不支持的产物后缀 {suffix!r}（白名单：{sorted(ARTIFACT_MIME_BY_SUFFIX)}）"
            )
        directory = self._render_dir(render_id)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / safe_name
        if isinstance(source, bytes):
            target.write_bytes(source)
        else:
            target.write_bytes(Path(source).read_bytes())
        meta = RenderMeta(
            render_id=render_id,
            filename=safe_name,
            mime=mime,
            kind=kind,
            attempts=attempts,
            created_at=time.time(),
        )
        (directory / META_FILENAME).write_text(meta.model_dump_json(indent=2), encoding="utf-8")
        return meta

    def get(self, render_id: str) -> tuple[Path, RenderMeta] | None:
        """读回（产物路径, meta）；ID 非法、目录/meta/产物缺失都返回 None。"""
        if not RENDER_ID_RE.match(render_id):
            return None
        directory = self.root / render_id
        meta_path = directory / META_FILENAME
        if not meta_path.is_file():
            return None
        try:
            meta = RenderMeta.model_validate(json.loads(meta_path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return None
        # 产物文件名也过一遍白名单（meta 虽是自己写的，防御性检查不嫌多）
        if Path(meta.filename).suffix.lower() not in ARTIFACT_MIME_BY_SUFFIX:
            return None
        artifact = self._confined(directory / meta.filename)
        if artifact is None or not artifact.is_file():
            return None
        return artifact, meta

    def _render_dir(self, render_id: str) -> Path:
        if not RENDER_ID_RE.match(render_id):
            raise RenderStoreError(f"非法渲染 ID {render_id!r}")
        path = self._confined(self.root / render_id)
        if path is None:
            raise RenderStoreError(f"渲染 ID {render_id!r} 越出产物根目录")
        return path

    def _confined(self, path: Path) -> Path | None:
        """resolve 后必须仍在根目录内（正则之外的保险，符号链接也拦得住）。"""
        resolved = path.resolve()
        root = self.root.resolve()
        if resolved == root or not resolved.is_relative_to(root):
            return None
        return resolved
