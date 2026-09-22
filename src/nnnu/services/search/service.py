"""搜索服务（§7.2）：配置解析 + 提供商调度。

配置来源：settings models 区（search_provider/search_base_url/search_api_key）
+ user-secrets 域 "search"（按 provider 分槽）+ 环境变量兜底
（BOCHA_API_KEY / SERPAPI_API_KEY / SEARCH_API_KEY）。
未配置 provider 时用免密钥默认 DuckDuckGo。
"""

import os

from nnnu.services.search.providers import DEFAULT_PROVIDER, PROVIDERS
from nnnu.services.search.types import SearchResponse

PROVIDER_ENV_KEYS = {
    "bocha": ("BOCHA_API_KEY", "SEARCH_API_KEY"),
    "serpapi_compat": ("SERPAPI_API_KEY", "SEARCH_API_KEY"),
}


def _config() -> tuple[str, str | None, str | None]:
    from nnnu.services.settings.service import get_settings_service

    values = get_settings_service().load_area("models")
    raw_provider = str(values.get("search_provider") or "").strip()
    provider = raw_provider or DEFAULT_PROVIDER
    base_url = str(values.get("search_base_url") or "").strip() or None
    api_key: str | None = None
    from nnnu.services.secrets.store import get_secrets_store

    # 槽名与设置草稿流对齐（空 provider → "default" 槽，见 settings/service._secret_slot）
    api_key = get_secrets_store().get("search", raw_provider or "default")
    if api_key is None:
        for env_key in PROVIDER_ENV_KEYS.get(provider, ()):
            api_key = os.environ.get(env_key)
            if api_key:
                break
    return provider, base_url, api_key


async def web_search(
    query: str,
    max_results: int = 5,
    *,
    transport=None,
) -> SearchResponse:
    """按当前配置执行搜索；未知 provider 返回明确错误。"""
    provider_id, base_url, api_key = _config()
    provider = PROVIDERS.get(provider_id)
    if provider is None:
        return SearchResponse(
            query=query, provider=provider_id, error=f"未知搜索提供商 {provider_id}"
        )
    return await provider.search(
        query,
        max(1, min(int(max_results), 10)),
        api_key=api_key,
        base_url=base_url,
        transport=transport,
    )
