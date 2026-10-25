"""子代理连接服务（§7.12）：增删查 + 探活。

连接配置是**磁盘为准**（`data/user/agents/connections.json`），库里只记会话选中的
是哪一个（`sessions.agent_connection`）。所有写路径走一个 asyncio.Lock + 原子写：
读-改-写撞上并发请求会丢条目，而这份文件是用户手点的配置，丢了没人能还原。

建连接、删连接、探测都落审计（§11.6）：跑的是本机上的别人家 CLI、以本用户权限
读写文件，出了事要能回答「什么时候建的、跑过没有」。
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

from nnnu.core.ids import new_id
from nnnu.runtime.home import get_data_root
from nnnu.services.agents.spec import (
    AGENT_KINDS,
    AgentConfig,
    AgentConnection,
    command_allowed,
    load_config,
    save_config,
)
from nnnu.services.audit import audit_log
from nnnu.subagents.process import probe_version


class AgentConnectionNotFound(ValueError):
    """连接不存在（配置里没有这个 id）。"""


class AgentConnectionInvalid(ValueError):
    """建/改连接时给的参数不成立（消息直接给用户看）。"""


class AgentService:
    """连接配置的单一入口（路由、挂载门、咨询工具都走它）。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    # ---- 读 ----

    def config(self) -> AgentConfig:
        return load_config()

    def list(self) -> list[AgentConnection]:
        """全部连接（按创建时间排，界面上顺序稳定）。"""
        items = list(self.config().connections.values())
        items.sort(key=lambda item: (item.created_at, item.id))
        return items

    def get(self, connection_id: str) -> AgentConnection | None:
        if not connection_id:
            return None
        return self.config().connections.get(connection_id)

    def count(self) -> int:
        """有几条连接（工具挂载门用：一条都没有就不挂 consult_subagent）。"""
        return len(self.config().connections)

    def resolve(
        self, connection_id: str | None, *, session_selected: str | None = None
    ) -> AgentConnection | None:
        """挑一条连接来用：显式指定 > 会话粘性选中 > 唯一那条。

        挑不出来（多半是"还没建连接"或"id 写错了"）返回 None，由调用方决定怎么报——
        工具要给出模型能读懂的话，路由要给出用户能读懂的话。
        """
        for candidate in (connection_id, session_selected):
            if candidate:
                found = self.get(candidate)
                if found is not None:
                    return found
        items = self.list()
        return items[0] if len(items) == 1 else None

    # ---- 写 ----

    async def create(
        self,
        *,
        kind: str,
        display_name: str = "",
        cwd: str = "",
        model_override: str = "",
        permission_mode: str = "bypassPermissions",
        command: list[str] | None = None,
        acknowledged: bool = False,
    ) -> AgentConnection:
        """建一条连接。

        `acknowledged` 是界面上那个「我了解子代理将以我的用户权限直接读写文件」的勾选：
        无头运行没有交互确认，这一步是唯一一次知情机会，必须显式给（同 cli_apps 安装的
        `confirm=true` 口径）。
        """
        if kind not in AGENT_KINDS:
            raise AgentConnectionInvalid(f"未知子代理类型 {kind!r}：取 {AGENT_KINDS} 之一")
        if not acknowledged:
            raise AgentConnectionInvalid(
                "建子代理连接需要先确认：它会以你的用户权限在本机直接读写文件（acknowledged=true）"
            )
        custom = [str(part) for part in (command or []) if str(part).strip()]
        if custom and not command_allowed():
            raise AgentConnectionInvalid(
                "本版本不接受自定义启动命令（只能用各类型自带的程序名）；"
                "确实需要时设 NNNU_AGENTS_ALLOW_CUSTOM_COMMAND=1 再启动服务"
            )
        resolved_cwd = self._check_cwd(cwd)
        connection = AgentConnection(
            id=new_id("ag"),
            kind=kind,
            display_name=display_name.strip(),
            cwd=resolved_cwd,
            model_override=model_override.strip(),
            permission_mode=permission_mode,
            command=custom,
            created_at=time.time(),
        )
        async with self._lock:
            config = load_config()
            config.connections[connection.id] = connection
            save_config(config)
        audit_log(
            "agent_connection_create",
            id=connection.id,
            kind=connection.kind,
            cwd=connection.cwd,
            permission_mode=connection.permission_mode,
            custom_command=bool(custom),
        )
        return connection

    async def delete(self, connection_id: str) -> AgentConnection:
        async with self._lock:
            config = load_config()
            connection = config.connections.pop(connection_id, None)
            if connection is None:
                raise AgentConnectionNotFound(f"没有这条子代理连接：{connection_id}")
            save_config(config)
        audit_log("agent_connection_delete", id=connection_id, kind=connection.kind)
        return connection

    # ---- 探活 ----

    async def probe(self, connection_id: str) -> dict:
        """真跑一次 `<程序> --version`，如实报结果（见 process.probe_version）。"""
        connection = self.get(connection_id)
        if connection is None:
            raise AgentConnectionNotFound(f"没有这条子代理连接：{connection_id}")
        argv = [*connection.argv_head(), "--version"]
        available, detail = await probe_version(argv)
        if available and not connection.supported:
            # 程序装了但本批还没接这条链路：别让用户以为"探到了就能用"
            detail = f"{detail}（本版本尚未接入该类型的咨询链路，先只记着）"
        audit_log(
            "agent_connection_probe",
            id=connection.id,
            kind=connection.kind,
            available=available,
            detail=detail[:200],
        )
        return {
            "connection_id": connection.id,
            "available": available,
            "supported": connection.supported,
            "argv": argv,
            "detail": detail,
        }

    # ---- 内部 ----

    @staticmethod
    def _check_cwd(cwd: str) -> str:
        """工作目录：留空给默认（workspace），给了就必须是存在目录的绝对路径。

        子代理会在这个目录里读写文件，路径写错一个字的后果是它在一个意外的目录里
        干活——这种错当场报出来，比让它慢慢跑完再发现好。
        """
        text = (cwd or "").strip()
        if not text:
            return str(get_data_root() / "user" / "workspace")
        path = Path(os.path.expanduser(text))
        if not path.is_absolute():
            raise AgentConnectionInvalid(f"工作目录要写绝对路径：{text}")
        if not path.is_dir():
            raise AgentConnectionInvalid(f"工作目录不存在或不是目录：{path}")
        return str(path)


# ---- 单例装配（挂载门/咨询工具用）——照 skills 的存取器模式 ----

_agent_service: AgentService | None = None


def get_agent_service() -> AgentService:
    if _agent_service is None:
        raise RuntimeError("子代理服务未装配（lifespan 未调用 set_agent_service）")
    return _agent_service


def set_agent_service(service: AgentService | None) -> None:
    global _agent_service
    _agent_service = service


__all__ = [
    "AgentConnectionInvalid",
    "AgentConnectionNotFound",
    "AgentService",
    "get_agent_service",
    "set_agent_service",
]
