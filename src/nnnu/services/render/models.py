"""渲染产物域模型（§7.7 可视化 / §7.8 数学动画）。

产物走「markdown 正文 + HTTP 静态端点」两条通道，不新增事件类型、不落库：
- 可视化的 svg/echarts/mermaid/html 直接是正文里的渲染围栏（纯文本，前端自渲染）；
- 二进制产物（视频/图片）落 `<data>/user/renders/<render_id>/`，正文里放一段
  ```nnnu-artifact 围栏（JSON 载荷见 `artifact_payload`），前端按 URL 拉取。

正文围栏的前后端契约常量在 `validation.py` 顶部（前端对应 `web/lib/render.ts`），
改一边要同步另一边。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel

# §7.7 路由表：图表→echarts / 示意图→svg / 流程→mermaid / 交互→html / 数学动画→manim_video。
# manim_video 只能由分析段自动判出，不开放给用户直接 pin——想明确要动画就用
# math_animator 能力（P7 决策）。
RenderType = Literal["svg", "echarts", "mermaid", "html", "manim_video"]
VISUAL_RENDER_TYPES: tuple[str, ...] = ("svg", "echarts", "mermaid", "html")
MANIM_RENDER_TYPE = "manim_video"

# §7.8 质量档：对应 manim 的 -ql / -qm / -qh
QUALITIES: tuple[str, ...] = ("low", "medium", "high")
QUALITY_FLAGS: dict[str, str] = {"low": "l", "medium": "m", "high": "h"}

# 产物种类（artifact 围栏与 meta.json 的 kind 字段）
ArtifactKind = Literal["video", "image"]


class RenderMeta(BaseModel):
    """一份渲染产物的 meta.json（落盘，无 DB）；文件名/MIME 都是库自己写的。"""

    render_id: str
    filename: str
    mime: str
    kind: ArtifactKind
    attempts: int = 1  # 第几次渲染尝试产出的（code_retry 用）
    source: str = "manim"
    created_at: float = 0.0

    def url(self) -> str:
        """正文围栏与前端取产物用的 URL——**刻意不带扩展名**。

        `web/proxy.ts` 的 matcher 把 `.png/.svg` 这类结尾的路径排除在反代之外
        （那些本来就该由 Next 静态资源处理），带扩展名前端根本拿不到产物。
        """
        return f"/api/v1/renders/{self.render_id}"


def artifact_payload(meta: RenderMeta) -> dict[str, Any]:
    """```nnnu-artifact 围栏的 JSON 载荷（前后端契约，解析端 web/lib/render.ts）。"""
    return {
        "url": meta.url(),
        "filename": meta.filename,
        "mime": meta.mime,
        "kind": meta.kind,
        "attempts": meta.attempts,
    }


@dataclass(slots=True)
class RenderResult:
    """一次渲染尝试的结果：成功带 meta，失败带日志尾巴（喂回修复提示词）。"""

    ok: bool
    meta: RenderMeta | None = None
    log: str = ""
    error: str = ""
