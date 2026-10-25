"""子代理 REST（§7.12）：连接配置的增删查与探测。

两条贯穿全文件的规矩：

* **探测就是真跑一次**。`/probe` 起一个真进程跑 `<程序> --version` 看它退不退 0，
  不看 PATH 里有没有这个文件——本机就躺着一个反例：npm 全局的 claude 三件套都在，
  原生二进制却没装上，任何参数只打一行报错。以文件存在为准的话，界面会显示
  「可用」，然后每次咨询都失败（见 subagents/process.py 的注释）。
* **建连接是一次知情确认**。无头运行没有交互确认，子代理以本用户的权限直接读写
  文件，所以 `POST` 必须带 `acknowledged=true`，与 cli_apps 安装的 `confirm=true`
  同一个口径：把一次「放着以后再说」的误点，变回一次明确的选择。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from nnnu.services.agents.imports import (
    SUPPORTED_SOURCES,
    AgentImportInvalid,
    AgentSessionNotFound,
    get_agent_import_service,
)
from nnnu.services.agents.service import (
    AgentConnectionInvalid,
    AgentConnectionNotFound,
    get_agent_service,
)
from nnnu.services.agents.spec import (
    AGENT_KINDS,
    DEFAULT_COMMANDS,
    PERMISSION_MODES,
    SUPPORTED_KINDS,
    command_allowed,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _error(
    status: int, code: str, message: str, *, recoverable: bool = False, details: dict | None = None
) -> JSONResponse:
    """§9.1 统一错误信封 {error:{code,message,recoverable}}。"""
    error: dict[str, Any] = {"code": code, "message": message, "recoverable": recoverable}
    if details is not None:
        error["details"] = details
    return JSONResponse(status_code=status, content={"error": error})


class ConnectionRequestBody(BaseModel):
    """建连接：kind 必填，其余可省（cwd 省了用默认 workspace）。

    `command` 是给非标准安装路径留的口子，默认只在设了
    `NNNU_AGENTS_ALLOW_CUSTOM_COMMAND=1` 时才被接受——不设这道门，这个字段就是
    给 HTTP 开的一个任意代码执行面（见 spec.py 模块头）。
    """

    kind: str
    display_name: str = ""
    cwd: str = ""
    model_override: str = ""
    permission_mode: str = "bypassPermissions"
    command: list[str] = Field(default_factory=list)
    acknowledged: bool = False


# ---- 连接 ----


@router.get("/api/v1/agents")
async def list_connections():
    """连接清单 + 这份清单可选项的元信息（kind 有哪些、哪个能真跑）。"""
    service = get_agent_service()
    return {
        "connections": [_payload(item) for item in service.list()],
        "kinds": _kinds_payload(),
        "permission_modes": list(PERMISSION_MODES),
        "custom_command_allowed": command_allowed(),
    }


@router.post("/api/v1/agents")
async def create_connection(body: ConnectionRequestBody):
    """建一条连接（需要 `acknowledged=true`，理由见模块头）。"""
    service = get_agent_service()
    try:
        connection = await service.create(
            kind=body.kind,
            display_name=body.display_name,
            cwd=body.cwd,
            model_override=body.model_override,
            permission_mode=body.permission_mode,
            command=body.command,
            acknowledged=body.acknowledged,
        )
    except AgentConnectionInvalid as exc:
        # 校验消息是写给用户看的（目录不存在 / 不认识 kind / 没确认），原样透出
        return _error(400, "agent_connection_invalid", str(exc))
    return {"connection": _payload(connection)}


@router.delete("/api/v1/agents/{connection_id}")
async def delete_connection(connection_id: str):
    """删一条连接：只动配置，已导入的历史会话与主干会话都不受影响。

    会话里粘性选中的那个 id 会变成悬空引用——咨询时按「选中项找不到就回退自动选」
    处理（service.resolve），所以不在这里回头清扫 sessions 表。
    """
    service = get_agent_service()
    try:
        connection = await service.delete(connection_id)
    except AgentConnectionNotFound as exc:
        return _error(404, "agent_connection_not_found", str(exc))
    return {"deleted": connection.id}


@router.post("/api/v1/agents/{connection_id}/probe")
async def probe_connection(connection_id: str):
    """真跑一次 `<程序> --version`；探测本身落审计（跑的是本机上别人家的程序）。"""
    service = get_agent_service()
    try:
        result = await service.probe(connection_id)
    except AgentConnectionNotFound as exc:
        return _error(404, "agent_connection_not_found", str(exc))
    return {"probe": result}


# ---- 历史导入（§7.12 第三刀） ----


class ImportRequestBody(BaseModel):
    """导入：`root` 与 `days` 是可选的筛选条件，`ids` 是用户勾中的那几条。

    `ids` 为空 = 把扫到的都导一遍（幂等，没变的不重写）；给了 id = 只导这几条。
    """

    root: str = ""
    days: float = 0.0
    ids: list[str] = Field(default_factory=list)


@router.get("/api/v1/agents/imports")
async def list_imports(q: str = "", limit: int = 50, offset: int = 0):
    """已导入的会话（标题与前段正文可搜）。"""
    service = get_agent_import_service()
    return await service.list_sessions(q=q, limit=limit, offset=offset)


@router.get("/api/v1/agents/imports/candidates")
async def scan_imports(root: str = "", days: float = 0.0):
    """扫候选：把根下扫到的历史列出来，并标明哪些已经导过、哪些来源又变了。

    只扫不写——导入向导先让用户看清楚有什么，再决定导哪些（`POST` 那一步才动库）。
    """
    service = get_agent_import_service()
    sources = list(SUPPORTED_SOURCES)
    try:
        result = await service.scan(root=root, days=days)
    except AgentImportInvalid as exc:
        return _error(400, "agent_import_invalid", str(exc))
    result["sources"] = sources
    return result


@router.post("/api/v1/agents/imports")
async def import_sessions(body: ImportRequestBody):
    """扫 + 导（幂等：同一份历史再扫一次不会多出条目，见 imports.py 模块头）。"""
    service = get_agent_import_service()
    try:
        return await service.import_sessions(root=body.root, days=body.days, ids=body.ids)
    except AgentImportInvalid as exc:
        return _error(400, "agent_import_invalid", str(exc))


@router.get("/api/v1/agents/imports/{session_id}")
async def read_import(session_id: str, limit: int = 50, offset: int = 0):
    """读一条导入会话的正文（分页）。id 收口在服务层：形状不对 → 404。"""
    service = get_agent_import_service()
    try:
        return await service.read_transcript(session_id, limit=limit, offset=offset)
    except AgentSessionNotFound as exc:
        return _error(404, "agent_session_not_found", str(exc))


@router.delete("/api/v1/agents/imports/{session_id}")
async def delete_import(session_id: str):
    """删一条导入会话（库里那行 + 正文文件），落审计。"""
    service = get_agent_import_service()
    try:
        return await service.delete(session_id)
    except AgentSessionNotFound as exc:
        return _error(404, "agent_session_not_found", str(exc))


# ---- 组装 ----


def _payload(connection) -> dict[str, Any]:
    """一条连接的对外视图：给界面用的 label 与启动命令，加「本批支持吗」。"""
    data = connection.model_dump(mode="json")
    data["label"] = connection.label()
    data["argv"] = connection.argv_head()
    data["supported"] = connection.supported
    return data


def _kinds_payload() -> list[dict[str, Any]]:
    """可选 kind 一行一条：默认程序名 + 本批是否已接上咨询链路。

    把 `supported` 送到界面上，是为了让它能把「能用」和「先记着」分开显示——
    用户建了一条 codex 连接、点探测显示可用，然后每次咨询都失败，是最坏的一种体验。
    """
    return [
        {
            "kind": kind,
            "default_command": list(DEFAULT_COMMANDS[kind]),
            "supported": kind in SUPPORTED_KINDS,
        }
        for kind in AGENT_KINDS
    ]
