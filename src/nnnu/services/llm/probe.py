"""模型端点连通性探测（§7.19 草稿-应用：探测成功才生效）。

GET {base_url}/models（OpenAI 兼容标准端点）：200 → 提取模型 id 列表；
401 → 密钥无效；连接失败/超时 → 明确文案。base_url 未带 /v1 时自动
追加 /v1 再试一次（兼容用户填根域名）。transport 参数供测试注入
（与 openai_compat.py 同法，§12.1）。
"""

import logging
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)

PROBE_TIMEOUT_S = 8.0


@dataclass(frozen=True, slots=True)
class ProbeResult:
    ok: bool
    models: list[str] = field(default_factory=list)
    error: str | None = None


def _candidate_urls(base_url: str) -> list[str]:
    """候选 models 端点：原样一个；未含 /v1 时追加 /v1 版。"""
    base = base_url.strip().rstrip("/")
    urls = [f"{base}/models"]
    if "/v1" not in base and not base.endswith("/v1"):
        urls.append(f"{base}/v1/models")
    return urls


def _extract_models(payload: dict) -> list[str]:
    """OpenAI 兼容 /models 响应：data[].id。"""
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    return [item["id"] for item in data if isinstance(item, dict) and "id" in item]


async def probe_models(
    base_url: str,
    api_key: str | None = None,
    *,
    timeout: float = PROBE_TIMEOUT_S,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ProbeResult:
    """探测端点可用性；失败时 error 为可直接展示的文案（§7.19 探测失败提示）。"""
    if not base_url.strip():
        return ProbeResult(ok=False, error="base_url 为空")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    urls = _candidate_urls(base_url)
    async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
        for index, url in enumerate(urls):
            try:
                response = await client.get(url, headers=headers)
            except httpx.TimeoutException:
                return ProbeResult(ok=False, error=f"连接超时（{timeout:g} 秒），请检查地址与网络")
            except httpx.ConnectError:
                return ProbeResult(ok=False, error=f"无法连接 {url}，请检查 base_url 是否正确")
            if response.status_code == 401:
                return ProbeResult(ok=False, error="API 密钥无效（端点返回 401）")
            if response.status_code != 200:
                # 非 200 且还有候选（比如 /v1 版本）就继续试；否则报错
                if index < len(urls) - 1 and response.status_code in (404, 403):
                    continue
                return ProbeResult(
                    ok=False, error=f"端点返回 HTTP {response.status_code}，不是可用的 /models 服务"
                )
            try:
                payload = response.json()
            except ValueError:
                return ProbeResult(ok=False, error="端点响应不是 JSON，可能不是 OpenAI 兼容服务")
            if not isinstance(payload, dict):
                return ProbeResult(ok=False, error="端点响应格式不对，可能不是 OpenAI 兼容服务")
            models = _extract_models(payload)
            return ProbeResult(ok=True, models=models)
    return ProbeResult(ok=False, error="未找到可用的 /models 端点")
