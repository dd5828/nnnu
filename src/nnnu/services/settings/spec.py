"""设置 spec：字段级声明式定义。

P0 最小集三区（appearance/network/system）；每字段带类型、默认值、
生效方式（instant|restart）与 i18n 键——P2 表单自动渲染直接消费本表。
"""

from dataclasses import dataclass
from typing import Any, Literal

Effect = Literal["instant", "restart"]


@dataclass(frozen=True, slots=True)
class SettingField:
    key: str
    type: str  # "string" | "int" | "bool" | "choice" | "string_list"
    default: Any
    label_key: str  # 前端 locales 键
    effect: Effect = "instant"
    choices: tuple[str, ...] | None = None
    description_key: str = ""


@dataclass(frozen=True, slots=True)
class AreaSpec:
    name: str
    fields: tuple[SettingField, ...]


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
}
