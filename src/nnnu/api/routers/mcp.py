"""MCP REST（§7.17）：服务器配置、连接状态、测试连接、OAuth 授权。

两条贯穿全文件的规矩：

* **配置只进不出明文**。写进来的密钥进 SecretsStore（域 `mcp/<id>`），配置里只留
  `${secret:name}` 引用；读回去的永远只有字段名与掩码（`GET` 不该是密钥的出口）。
* **换密钥先探后存**。带 `secrets` 的保存先拿候选值去连一次，连不上就整笔回滚并把
  原因报出来——「保存成功但连不上」是最让人白忙的一种成功。
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from nnnu.services.audit import audit_log
from nnnu.services.mcp import oauth as mcp_oauth
from nnnu.services.mcp.manager import PROBE_DEFAULT_TIMEOUT_S, get_mcp_manager
from nnnu.services.mcp.secrets import (
    configured_fields,
    delete_secrets,
    domain_for,
    store_secrets,
)
from nnnu.services.mcp.spec import (
    CredentialSpec,
    MCPServerConfig,
    load_catalog,
    load_config,
    save_config,
)
from nnnu.services.secrets.store import get_secrets_store

logger = logging.getLogger(__name__)

router = APIRouter()

#: 同一时刻只允许一次探测：连不上的服务器（比如那个没人听的端口）会一直挂着，
#: 用户会连点按钮，几十个并发连接排着队等超时对谁都没好处。
_probe_lock = asyncio.Lock()


def _error(
    status: int, code: str, message: str, *, recoverable: bool = False, details: dict | None = None
) -> JSONResponse:
    """§9.1 统一错误信封 {error:{code,message,recoverable}}。"""
    error: dict[str, Any] = {"code": code, "message": message, "recoverable": recoverable}
    if details is not None:
        error["details"] = details
    return JSONResponse(status_code=status, content={"error": error})


class ServerRequestBody(BaseModel):
    """新建/修改服务器的请求体（PATCH 只认传了的字段）。"""

    id: str | None = None
    name: str | None = None
    transport: str | None = None
    command: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None
    url: str | None = None
    headers: dict[str, str] | None = None
    credential: CredentialSpec | None = None
    enabled: bool | None = None
    disabled_tools: list[str] | None = None
    connect_timeout_s: int | None = None
    tool_timeout_s: int | None = None
    catalog_entry: str | None = None
    #: 仅入参：字段名 → 明文。空串 = 删除该字段。永不回显、永不进配置文件。
    secrets: dict[str, str] = Field(default_factory=dict)


class ProbeRequestBody(BaseModel):
    """测试连接的请求体：可不带（用已存的凭据），也可带未落盘的候选值。"""

    secrets: dict[str, str] = Field(default_factory=dict)


# ---- 列表 ----


@router.get("/api/v1/mcp")
async def list_servers():
    """服务器清单：配置（脱敏）+ 连接状态 + 工具逐个的开关态。"""
    config = load_config()
    state = get_mcp_manager().state()
    return {"servers": [_payload(cfg, state.get(cfg.id)) for cfg in config.servers.values()]}


@router.get("/api/v1/mcp/catalog")
async def list_catalog():
    """内置服务器目录（一键预填用）：只给地址与安装方式，绝不含凭据。"""
    return {"entries": load_catalog()}


# ---- 增删改 ----


@router.post("/api/v1/mcp")
async def create_server(body: ServerRequestBody):
    """新建服务器；带 secrets 时先探通再落盘（探不通整笔回滚）。"""
    if not body.id:
        return _error(422, "mcp_invalid_config", "新建 MCP 服务器必须给 id")
    config = load_config()
    if body.id in config.servers:
        return _error(409, "mcp_duplicate_id", f"MCP 服务器 id 已存在：{body.id}")
    try:
        cfg = _merge(MCPServerConfig(id=body.id), body, creating=True)
    except (ValidationError, ValueError) as exc:
        return _error(422, "mcp_invalid_config", _validation_message(exc))

    probe = None
    if body.secrets:
        overrides = _clean_secrets(body.secrets)
        if overrides is None:
            return _error(422, "mcp_invalid_secrets", "密钥字段名只许字母/数字/下划线/连字符")
        probe = await _probe(cfg, overrides)
        failure = _probe_failure(probe)
        if failure:
            return _error(400, "mcp_connect_failed", failure, details={"probe": probe})

    config.servers[cfg.id] = cfg
    save_config(config)
    if body.secrets:
        store_secrets(cfg.id, _clean_secrets(body.secrets) or {})
        audit_log("mcp_credential_change", server=cfg.id, fields=sorted(body.secrets))
    audit_log("mcp_config_save", server=cfg.id, created=True, transport=cfg.resolved_transport())
    await get_mcp_manager().reload()
    return {"server": _payload(cfg, get_mcp_manager().state().get(cfg.id)), "probe": probe}


@router.patch("/api/v1/mcp/{server_id}")
async def update_server(server_id: str, body: ServerRequestBody):
    """改配置/启停/逐个停用工具/换密钥；换密钥先探后存，探不通不动任何东西。"""
    config = load_config()
    cfg = config.servers.get(server_id)
    if cfg is None:
        return _error(404, "mcp_not_found", f"没有这台 MCP 服务器：{server_id}")
    try:
        merged = _merge(cfg, body, creating=False)
    except (ValidationError, ValueError) as exc:
        return _error(422, "mcp_invalid_config", _validation_message(exc))

    probe = None
    if body.secrets:
        overrides = _clean_secrets(body.secrets)
        if overrides is None:
            return _error(422, "mcp_invalid_secrets", "密钥字段名只许字母/数字/下划线/连字符")
        probe = await _probe(merged, overrides)
        failure = _probe_failure(probe)
        if failure:
            # 回滚是「什么都不做」：配置没保存、密钥也没落盘，服务器继续用旧凭据。
            return _error(400, "mcp_connect_failed", failure, details={"probe": probe})

    config.servers[server_id] = merged
    save_config(config)
    if body.secrets:
        store_secrets(server_id, _clean_secrets(body.secrets) or {})
        audit_log("mcp_credential_change", server=server_id, fields=sorted(body.secrets))
    changed = sorted(
        key for key in body.model_dump(exclude_none=True, exclude={"secrets"}) if key != "id"
    )
    audit_log("mcp_config_save", server=server_id, fields=changed)
    await get_mcp_manager().reload()
    return {"server": _payload(merged, get_mcp_manager().state().get(server_id)), "probe": probe}


@router.delete("/api/v1/mcp/{server_id}")
async def delete_server(server_id: str):
    """删除服务器：配置、凭据（含 OAuth 令牌）一起清，再让管理器断开重连。"""
    config = load_config()
    if server_id not in config.servers:
        return _error(404, "mcp_not_found", f"没有这台 MCP 服务器：{server_id}")
    config.servers.pop(server_id)
    save_config(config)
    delete_secrets(server_id)  # OAuth 令牌也在这个域里，一并清掉
    audit_log("mcp_config_delete", server=server_id)
    await get_mcp_manager().reload()
    return {"deleted": server_id}


# ---- 探测与授权 ----


@router.post("/api/v1/mcp/{server_id}/test")
async def test_server(server_id: str, body: ProbeRequestBody | None = None):
    """连接探测：无论如何 ≤10 秒必返回（失败也是一条能读的原因）。"""
    config = load_config()
    cfg = config.servers.get(server_id)
    if cfg is None:
        return _error(404, "mcp_not_found", f"没有这台 MCP 服务器：{server_id}")
    overrides = _clean_secrets((body.secrets if body else {}))
    if overrides is None:
        return _error(422, "mcp_invalid_secrets", "密钥字段名只许字母/数字/下划线/连字符")
    return await _probe(cfg, overrides or None)


@router.post("/api/v1/mcp/{server_id}/auth")
async def authorize(server_id: str):
    """开一次 OAuth 同意流程，回授权 URL（浏览器能弹就顺带弹出来）。"""
    config = load_config()
    cfg = config.servers.get(server_id)
    if cfg is None:
        return _error(404, "mcp_not_found", f"没有这台 MCP 服务器：{server_id}")
    if cfg.credential is None or cfg.credential.kind != "oauth":
        return _error(400, "mcp_not_oauth", "这台服务器没有配置 OAuth，不需要授权")
    if not cfg.url:
        return _error(422, "mcp_invalid_config", "OAuth 服务器必须先填地址")
    try:
        auth_url = await mcp_oauth.begin_authorization(server_url=cfg.url, server_id=server_id)
    except Exception as exc:
        logger.warning("MCP OAuth 流程启动失败：%s", server_id, exc_info=True)
        return _error(502, "mcp_auth_failed", f"授权流程启动失败：{exc}")
    audit_log("mcp_config_save", server=server_id, kind="oauth_start")
    return {"auth_url": auth_url}


# ---- 组装 ----


def _payload(cfg: MCPServerConfig, state: dict[str, Any] | None) -> dict[str, Any]:
    """一台服务器的对外视图：配置（脱敏）+ 状态 + 工具清单。"""
    data = cfg.model_dump(mode="json")
    data.pop("secrets", None)
    data["secret_fields"] = _secret_fields(cfg)
    data["oauth"] = _oauth_payload(cfg)
    data["transport"] = cfg.resolved_transport()
    tools = _tool_rows(cfg, state)
    data["tools"] = tools
    data["status"] = "disabled" if not cfg.enabled else (state or {}).get("status", "connecting")
    data["error"] = (state or {}).get("error", "") if cfg.enabled else ""
    return data


def _tool_rows(cfg: MCPServerConfig, state: dict[str, Any] | None) -> list[dict[str, Any]]:
    """工具清单：连上过就有真名，没连上过就只剩配置里停用的那几名。

    停用名要与已知清单**取并集**：只列活工具的话，一个当前连不上的服务器里被停用的
    工具就消失了，用户再也没法把它打开。

    连上过、但现在没连接的服务器（停用的、刚断的）走 `last_tools`：界面要能照旧
    列出这台服务器有什么工具，开关也才有东西可点。
    """
    known: dict[str, dict[str, str]] = {}
    tools = (state or {}).get("tools") or get_mcp_manager().last_tools(cfg.id)
    for tool in tools:
        known[str(tool.get("name"))] = tool
    disabled = set(cfg.disabled_tools)
    rows = [
        {
            "name": name,
            "wrapped": tool.get("wrapped") or "",
            "description": tool.get("description") or "",
            "enabled": name not in disabled,
        }
        for name, tool in sorted(known.items())
    ]
    for name in sorted(disabled - set(known)):
        rows.append({"name": name, "wrapped": "", "description": "", "enabled": False})
    return rows


def _secret_fields(cfg: MCPServerConfig) -> dict[str, dict[str, Any]]:
    """配置里声明过的字段 + 已存的字段，掩码回显（明文没有出口）。"""
    store = get_secrets_store()
    domain = domain_for(cfg.id)
    names = set(configured_fields(cfg.id))
    if cfg.credential is not None:
        names.update(cfg.credential.secret_names)
    names -= set(mcp_oauth.OAUTH_FIELDS)  # 令牌不是「用户填的密钥」，不进这个清单
    return {name: store.summary(domain, name) for name in sorted(names)}


def _oauth_payload(cfg: MCPServerConfig) -> dict[str, Any] | None:
    if cfg.credential is None or cfg.credential.kind != "oauth":
        return None
    state = mcp_oauth.oauth_state(cfg.id)
    return {"authorized": state.authorized, "scope": state.scope}


def _merge(cfg: MCPServerConfig, body: ServerRequestBody, *, creating: bool) -> MCPServerConfig:
    """把请求体并进配置（PATCH 的语义：没传的字段保持原样）。"""
    data = cfg.model_dump(mode="json")
    incoming = body.model_dump(exclude_none=True, exclude={"secrets", "id"})
    data.update(incoming)
    if creating and body.id:
        data["id"] = body.id
    return MCPServerConfig.model_validate(data)


def _clean_secrets(values: dict[str, str]) -> dict[str, str] | None:
    """校验密钥字段名；非法返回 None（转成 422）。

    字段名会进 SecretsStore 的键和 `${secret:name}` 引用语法，宽松的字符集会在
    引用解析那一层变成难查的错，所以在这里就卡住。
    """
    out: dict[str, str] = {}
    for name, value in values.items():
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name):
            return None
        out[name] = str(value)
    return out


def _probe_failure(probe: dict[str, Any]) -> str:
    """探测结果里哪些算「必须拦住保存」的失败。

    `needs_auth` 不算：那是「还差一步人工授权」，用户要的正是把服务器存下来再去授权。
    把它当失败拦掉，OAuth 服务器就永远存不进来了。
    """
    if probe.get("ok"):
        return ""
    if probe.get("status") == "needs_auth":
        return ""
    return str(probe.get("error") or "连接失败")


async def _probe(cfg: MCPServerConfig, overrides: dict[str, str] | None) -> dict[str, Any]:
    """带互斥的探测：同一时刻只跑一次，超时上限探针自己管。"""
    async with _probe_lock:
        return await get_mcp_manager().probe(
            cfg, timeout_s=PROBE_DEFAULT_TIMEOUT_S, override=overrides
        )


def _validation_message(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        errors = exc.errors()
        if errors:
            first = errors[0]
            location = ".".join(str(part) for part in first.get("loc", ()))
            return f"{location}: {first.get('msg', '配置不合法')}".strip(": ")
    return str(exc)
