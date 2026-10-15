"""MCP 服务器配置（§7.17）：形态与落盘。

一份 JSON（`data/user/mcp/servers.json`）装下所有服务器：`{servers: {id: 配置}}`。
形态照 §8.2 之外的单用户先例（没有 MCP 表），与 settings/secrets 一样原子写。

**凭据只存引用**（§11.1）：`headers`/`env`/`args` 里的密钥一律写 `"${secret:name}"`，
明文只落 `data/system/user-secrets/mcp/<id>.json`（SecretsStore 的域，见 secrets.py）。
配置文件会被回显、复制、进日志，明文放这儿等于到处撒。

transport 三选一，`transport` 留空按字段自动判：有 `command` → stdio；`url` 以
`/sse` 结尾 → sse；其余 `url` → streamable_http。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from nnnu.runtime import home
from nnnu.services.settings.atomic import atomic_write_json

#: 内置服务器目录的文件名（仓库根 `catalogs/`，随 prompts/ 一起装）。
CATALOG_FILENAME = "mcp_servers.json"

#: 服务器 id：小写字母/数字/连字符，最长 48；它同时是 secrets 目录名与工具名前缀，
#: 所以卡得比显示名严（大小写混写会造出两个只差大小写的域）。
SERVER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")

TRANSPORTS = ("stdio", "sse", "streamable_http")

CONFIG_FILENAME = "servers.json"


class CredentialSpec(BaseModel):
    """一个服务器的凭据说明（不是凭据本身）。

    - `kind="apikey"`：`value_template` 渲染进 `header` 指的请求头（如
      `Authorization: Bearer ${secret:token}`）；header 留空表示引用直接写在
      `headers` 里，这里只做「配了哪些密钥」的账。
    - `kind="oauth"`：走 oauth.py 的授权流，`secret_names` 为空。
    """

    kind: Literal["apikey", "oauth"] = "apikey"
    header: str = ""
    value_template: str = ""
    secret_names: list[str] = Field(default_factory=list)


class MCPServerConfig(BaseModel):
    id: str
    name: str = ""
    transport: str = ""  # 空 = 按字段自动判（见模块头）
    # stdio
    command: str = ""  # 空 = 用当前解释器（sys.executable，见 manager）
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    # http
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    # 行为
    credential: CredentialSpec | None = None
    enabled: bool = True
    disabled_tools: list[str] = Field(default_factory=list)  # 逐个停用的工具（原始名或包装名）
    connect_timeout_s: int = Field(default=15, ge=1, le=120)
    tool_timeout_s: int = Field(default=60, ge=1, le=600)
    # 出处：从内置目录哪一条装来的（自建为空）
    catalog_entry: str = ""

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        if not SERVER_ID_RE.match(value):
            raise ValueError(f"非法 MCP 服务器 id {value!r}：只许小写字母/数字/连字符")
        return value

    @field_validator("command", "url", "transport")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip() if isinstance(value, str) else value

    def resolved_transport(self) -> str:
        """实际走哪种传输；判不出来返回空串（配置不完整）。"""
        if self.transport:
            return self.transport
        if self.command:
            return "stdio"
        if self.url:
            return "sse" if self.url.rstrip("/").endswith("/sse") else "streamable_http"
        return ""

    def connection_signature(self) -> str:
        """连接指纹：reload 靠它判断「这条配置变了没有」（变了才重连）。

        `catalog_entry` 不算：它记的是出处，不是怎么连，「补个出处字段就把活着的
        会话踢掉」是没道理的。凭据引用也只算引用本身——**轮换密钥后的指纹变化**
        由 manager 把解析后的摘要拌进来（见 manager._signature）。
        """
        data = self.model_dump(mode="json")
        data.pop("catalog_entry", None)
        return json.dumps(data, sort_keys=True, ensure_ascii=False)

    def tool_allowed(self, raw_name: str, wrapped_name: str) -> bool:
        """这个工具要不要注册：停用名单里的（原始名或包装名都认）一律不注册。"""
        blocked = set(self.disabled_tools or [])
        return raw_name not in blocked and wrapped_name not in blocked


class MCPConfig(BaseModel):
    servers: dict[str, MCPServerConfig] = Field(default_factory=dict)


def config_path() -> Path:
    return home.get_data_root() / "user" / "mcp" / CONFIG_FILENAME


logger = logging.getLogger(__name__)


def load_config() -> MCPConfig:
    """读配置；文件坏了/缺了按空处理（MCP 是可选扩展，读不出来不该拦启动）。"""
    path = config_path()
    if not path.is_file():
        return MCPConfig()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("MCP 配置损坏，按空处理：%s", path)
        return MCPConfig()
    try:
        return MCPConfig.model_validate(raw)
    except Exception:
        logger.warning("MCP 配置校验失败，按空处理：%s", path)
        return MCPConfig()


def save_config(config: MCPConfig) -> None:
    """原子写；一次写坏不能让整个配置被 load 的空兜底吞掉。"""
    atomic_write_json(config_path(), config.model_dump(mode="json"))


def load_catalog() -> list[dict[str, Any]]:
    """内置服务器目录（`catalogs/mcp_servers.json`）的条目列表。

    读不出来（文件缺失、坏 JSON、非可编辑安装里目录根没探到）一律返回空列表：
    目录只提供「一键预填」，坏了该少几条推荐，而不是让 MCP 页整页报错。
    """
    path = home.get_catalogs_root() / CATALOG_FILENAME
    if not path.is_file():
        logger.warning("内置 MCP 目录不存在：%s", path)
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("内置 MCP 目录损坏：%s", path)
        return []
    entries = raw.get("entries") if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict) and entry.get("id")]


__all__ = [
    "CATALOG_FILENAME",
    "CONFIG_FILENAME",
    "SERVER_ID_RE",
    "TRANSPORTS",
    "CredentialSpec",
    "MCPConfig",
    "MCPServerConfig",
    "config_path",
    "load_catalog",
    "load_config",
    "save_config",
]
