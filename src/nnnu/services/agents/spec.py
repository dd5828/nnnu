"""子代理连接配置（§7.12）：形态与落盘。

一份 JSON（`data/user/agents/connections.json`）装下所有连接：`{connections: {id: 配置}}`。
形态照 MCP 的 `servers.json`（单用户场景没有建表，原子写、坏文件当空告警）。

**与方案原文的两处偏离（都是补出来的）**：

- `permission_mode`：无头跑没有交互确认，默认 `bypassPermissions`——不给它就会被
  卡在"等用户批准某个工具"上直到超时。代价是子代理以本用户的权限直接读写文件，
  所以界面上建连接必须显式勾选知情，每次咨询另落一条审计（§11.6）。
- `command`：启动命令覆盖。默认空 = 用 kind 的默认程序名（`claude`）。它是给
  非标准安装路径与 E2E 假 CLI 用的，**同时是 HTTP 层的一个任意代码执行面**，
  所以只在 `NNNU_AGENTS_ALLOW_CUSTOM_COMMAND=1` 时才接受（见 `command_allowed`）。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

from nnnu.runtime import home
from nnnu.services.settings.atomic import atomic_write_json

CONFIG_FILENAME = "connections.json"

#: 方案 §7.12 的六种 kind。第一批只有 claude_code 有适配器（其余能建连接、能探测，
#: 咨询时明确报「这个 kind 还没接」）——先打通一条链路，别一次铺六条半成品。
AGENT_KINDS: tuple[str, ...] = ("claude_code", "codex", "gemini", "kimi", "opencode", "mimo")

#: 每种 kind 的默认启动命令（`command` 为空时用）。只有 claude_code 是本批实现的，
#: 其余先登记着：探测能告诉用户「装了没」，咨询会如实拒绝。
DEFAULT_COMMANDS: dict[str, tuple[str, ...]] = {
    "claude_code": ("claude",),
    "codex": ("codex",),
    "gemini": ("gemini",),
    "kimi": ("kimi",),
    "opencode": ("opencode",),
    "mimo": ("mimo",),
}

#: 本批真能跑咨询的 kind
SUPPORTED_KINDS: tuple[str, ...] = ("claude_code",)

#: `--permission-mode` 的取值（Claude Code 的四个档位；我们默认第一个）
PERMISSION_MODES: tuple[str, ...] = (
    "bypassPermissions",
    "acceptEdits",
    "plan",
    "default",
)


def command_allowed() -> bool:
    """允许连接里自定义启动命令吗（默认不允许）。

    不设这道门，`POST /api/v1/agents {command:["cmd.exe","/c",...]}` 就是给 HTTP 开
    了一个任意代码执行口子——建完连接再让它"咨询"，跑的就是任意命令，而审计只能在
    事后记录。E2E 用假 CLI 时在 `start-backend.mjs` 里显式开这个开关。
    """
    return os.environ.get("NNNU_AGENTS_ALLOW_CUSTOM_COMMAND", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


class AgentConnection(BaseModel):
    id: str
    kind: str
    display_name: str = ""
    #: 子代理的工作目录（绝对路径；服务层校验存在且是目录）
    cwd: str = ""
    #: 可选模型覆盖（透传给 CLI 的 --model；空 = 用 CLI 自己的默认）
    model_override: str = ""
    #: 无头运行的权限档位（见模块头）
    permission_mode: str = "bypassPermissions"
    #: 启动命令覆盖（空 = DEFAULT_COMMANDS[kind]）；仅在 command_allowed() 时被接受
    command: list[str] = Field(default_factory=list)
    created_at: float = 0.0

    @field_validator("kind")
    @classmethod
    def _check_kind(cls, value: str) -> str:
        if value not in AGENT_KINDS:
            raise ValueError(f"未知子代理类型 {value!r}：取 {AGENT_KINDS} 之一")
        return value

    @field_validator("permission_mode")
    @classmethod
    def _check_permission(cls, value: str) -> str:
        if value not in PERMISSION_MODES:
            raise ValueError(f"未知权限档位 {value!r}：取 {PERMISSION_MODES} 之一")
        return value

    def argv_head(self) -> list[str]:
        """启动命令（自定义优先，否则 kind 默认）。"""
        return list(self.command) if self.command else list(DEFAULT_COMMANDS[self.kind])

    def label(self) -> str:
        """界面上显示什么：用户起的名字，没有就用 kind。"""
        return self.display_name or self.kind

    @property
    def supported(self) -> bool:
        return self.kind in SUPPORTED_KINDS


class AgentConfig(BaseModel):
    connections: dict[str, AgentConnection] = Field(default_factory=dict)


def config_path() -> Path:
    return home.get_data_root() / "user" / "agents" / CONFIG_FILENAME


def imports_dir() -> Path:
    """导入的历史会话正文目录（每会话一个 `<id>.jsonl`，见 imports.py）。"""
    return home.get_data_root() / "user" / "agents" / "imports"


logger = logging.getLogger(__name__)


def load_config() -> AgentConfig:
    """读配置；文件坏了/缺了按空处理（子代理是可选扩展，读不出来不该拦启动）。"""
    path = config_path()
    if not path.is_file():
        return AgentConfig()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("子代理连接配置损坏，按空处理：%s", path)
        return AgentConfig()
    try:
        return AgentConfig.model_validate(raw)
    except Exception:
        logger.warning("子代理连接配置校验失败，按空处理：%s", path)
        return AgentConfig()


def save_config(config: AgentConfig) -> None:
    """原子写；一次写坏不能让整份配置被 load 的空兜底吞掉。"""
    atomic_write_json(config_path(), config.model_dump(mode="json"))
