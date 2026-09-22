"""图像生成服务（§7.2 imagegen）：OpenAI 兼容 images/generations 端点。

- 端点/密钥复用当前 LLM 配置（resolve_model_config），模型名取设置 image_model；
- 优先 b64_json 落盘 workspace/generated/（返回 data URI 供前端直显），
  否则用返回的 URL；
- videogen：各提供商差异大，P3 仅保留统一接口与友好报错（偏离记录）。
"""

import base64
import time
from dataclasses import dataclass

import httpx

from nnnu.runtime import home
from nnnu.services.settings.service import get_settings_service

TIMEOUT_S = 120.0


@dataclass(slots=True)
class ImageResult:
    ok: bool
    data_uri: str | None = None  # 前端 <img> 直显
    url: str | None = None
    path: str | None = None  # workspace 内落盘路径（模型可经文件工具引用）
    error: str | None = None


def _configured_image_model() -> str:
    return str(get_settings_service().load_area("models").get("image_model") or "").strip()


def image_generation_available() -> bool:
    """是否配置了图像生成模型（挂载/提示判断用）。"""
    return bool(_configured_image_model())


async def generate_image(
    prompt: str,
    *,
    size: str = "1024x1024",
    transport: httpx.AsyncBaseTransport | None = None,
) -> ImageResult:
    """OpenAI 兼容 images/generations；未配置模型时返回明确错误。"""
    from nnnu.services.llm.factory import resolve_model_config

    model = _configured_image_model()
    if not model:
        return ImageResult(ok=False, error="未配置图像生成模型（设置页模型卡片 image_model）")
    try:
        mc = resolve_model_config()
    except Exception as exc:
        return ImageResult(ok=False, error=f"模型配置无效：{exc}")
    endpoint = f"{mc.base_url or 'https://api.openai.com/v1'}/images/generations"
    headers = {"Content-Type": "application/json"}
    if mc.api_key:
        headers["Authorization"] = f"Bearer {mc.api_key}"
    body = {"model": model, "prompt": prompt, "n": 1, "size": size}
    async with httpx.AsyncClient(timeout=TIMEOUT_S, transport=transport) as client:
        try:
            response = await client.post(endpoint, headers=headers, json=body)
        except httpx.HTTPError as exc:
            return ImageResult(ok=False, error=f"图像生成请求失败：{exc.__class__.__name__}")
    if response.status_code != 200:
        return ImageResult(ok=False, error=f"图像生成端点返回 HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError:
        return ImageResult(ok=False, error="图像生成端点响应不是 JSON")
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        return ImageResult(ok=False, error="图像生成响应没有图片数据")
    item = data[0]
    b64 = item.get("b64_json")
    if b64:
        try:
            raw = base64.b64decode(b64)
        except ValueError:
            return ImageResult(ok=False, error="图片数据解码失败")
        generated = home.get_data_root() / "user" / "workspace" / "generated"
        generated.mkdir(parents=True, exist_ok=True)
        path = generated / f"image_{int(time.time() * 1000)}.png"
        path.write_bytes(raw)
        return ImageResult(
            ok=True,
            data_uri=f"data:image/png;base64,{b64}",
            path=f"generated/{path.name}",
        )
    url = item.get("url")
    if url:
        return ImageResult(ok=True, url=str(url))
    return ImageResult(ok=False, error="图像生成响应既无图片数据也无 URL")


async def generate_video(prompt: str) -> ImageResult:
    """videogen 统一接口：P3 未实现（各提供商协议差异大，随后续阶段补齐）。"""
    return ImageResult(ok=False, error="视频生成尚未支持（当前仅图像生成，见设置页模型卡片）")
