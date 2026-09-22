"""设置 spec：字段级声明式定义。

P0 最小集三区（appearance/network/system）；P2 加 models（§7.19 模型卡片）。
每字段带类型、默认值、生效方式（instant|restart）与 i18n 键——P2 表单自动
渲染直接消费本表。secret 类型不进设置 JSON（§5 配置铁律：密钥只进
user-secrets 目录），明文只在草稿提交时经服务层转运一次。
"""

from dataclasses import dataclass
from typing import Any, Literal

from nnnu.services.llm.provider_registry import build_registry

Effect = Literal["instant", "restart"]


@dataclass(frozen=True, slots=True)
class SettingField:
    key: str
    type: str  # "string" | "int" | "float" | "bool" | "choice" | "string_list" | "secret"
    default: Any
    label_key: str  # 前端 locales 键
    effect: Effect = "instant"
    choices: tuple[str, ...] | None = None
    description_key: str = ""
    # secret 字段专属：user-secrets 域与槽位（槽名取同区某个字段的值）
    secret_domain: str = "llm"
    secret_slot_of: str = "provider"


@dataclass(frozen=True, slots=True)
class AreaSpec:
    name: str
    fields: tuple[SettingField, ...]


# 模型卡片 provider 选项："" = 未显式配置（跟随环境变量 NNNU_MODEL 或内置默认）、
# 注册表全部内置 provider + 自定义端点（§7.19）
PROVIDER_CHOICES: tuple[str, ...] = ("", *[spec.id for spec in build_registry()], "custom")


SPECS: dict[str, AreaSpec] = {
    "appearance": AreaSpec(
        "appearance",
        (
            SettingField(
                "theme",
                "choice",
                "light",
                "settings.appearance.theme",
                choices=("light", "beige", "dark", "glass"),
            ),
            SettingField(
                "ui_language",
                "choice",
                "zh",
                "settings.appearance.uiLanguage",
                choices=("zh", "en"),
            ),
            SettingField(
                "output_language",
                "choice",
                "zh",
                "settings.appearance.outputLanguage",
                choices=("zh", "en"),
            ),
        ),
    ),
    "network": AreaSpec(
        "network",
        (
            SettingField(
                "backend_port", "int", 8001, "settings.network.backendPort", effect="restart"
            ),
            SettingField(
                "frontend_port", "int", 3782, "settings.network.frontendPort", effect="restart"
            ),
            SettingField(
                "cors_origins",
                "string_list",
                ["http://localhost:3782", "http://127.0.0.1:3782"],
                "settings.network.corsOrigins",
                effect="restart",
            ),
        ),
    ),
    "system": AreaSpec(
        "system",
        (
            SettingField(
                "log_level",
                "choice",
                "info",
                "settings.system.logLevel",
                effect="restart",
                choices=("debug", "info", "warning", "error"),
            ),
        ),
    ),
    # §7.19 模型卡片：LLM 接入（embedding/search/TTS 等随 P3/P4 追加同区字段）
    "models": AreaSpec(
        "models",
        (
            SettingField(
                "provider",
                "choice",
                "",
                "settings.models.provider",
                choices=PROVIDER_CHOICES,
                description_key="settings.models.providerDesc",
            ),
            SettingField(
                "base_url",
                "string",
                "",
                "settings.models.baseUrl",
                description_key="settings.models.baseUrlDesc",
            ),
            SettingField(
                "model",
                "string",
                "",
                "settings.models.model",
                description_key="settings.models.modelDesc",
            ),
            SettingField(
                "api_key",
                "secret",
                None,
                "settings.models.apiKey",
                description_key="settings.models.apiKeyDesc",
            ),
            SettingField(
                "temperature",
                "float",
                1.0,
                "settings.models.temperature",
            ),
            SettingField(
                "reasoning_effort",
                "choice",
                "",
                "settings.models.reasoningEffort",
                choices=("", "low", "medium", "high"),
            ),
            SettingField(
                "search_provider",
                "choice",
                "",
                "settings.models.searchProvider",
                choices=("", "duckduckgo", "searxng", "bocha", "serpapi_compat"),
                description_key="settings.models.searchProviderDesc",
            ),
            SettingField(
                "search_base_url",
                "string",
                "",
                "settings.models.searchBaseUrl",
                description_key="settings.models.searchBaseUrlDesc",
            ),
            SettingField(
                "search_api_key",
                "secret",
                None,
                "settings.models.searchApiKey",
                secret_domain="search",
                secret_slot_of="search_provider",
            ),
            SettingField(
                "image_model",
                "string",
                "",
                "settings.models.imageModel",
                description_key="settings.models.imageModelDesc",
            ),
        ),
    ),
}
