"""命令行应用 REST（§7.18）：目录浏览、安装、卸载、详情。

三条贯穿全文件的规矩：

* **安装是用户在界面上点了确认的管理动作**，不是模型能触发的：`POST` 必须带
  `confirm=true`，装的动作在沙箱外执行（pip/npm 要联网、要写自己的目录），
  所以安装命令在 spec.plan_install 那一步就被解析成一包一包、argv 化执行。
* **每个响应都带上「现在装没装」**。状态的判据是程序此刻找不找得到（install.is_installed），
  不是记录：用户手删了 venv、PATH 换了版本，界面要如实反映。
* **失败要能读**：装失败回 400 + `install.log` 结尾那几百字符——「安装失败」四个字
  对用户毫无用处，pip 最后两行报错才有用。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from nnnu.services.cli_apps import install as installer
from nnnu.services.cli_apps.catalog import catalog_meta
from nnnu.services.cli_apps.service import (
    CliAppNotFoundError,
    CliAppUnsupportedError,
    get_cli_app_service,
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


class InstallRequestBody(BaseModel):
    """装一件应用：`id` 装目录里的，`entry` 是用户手写的一条（先校验再装）。

    `confirm` 必须显式给 true：pip/npm 安装会联网、会往自己的目录里写东西，
    用「默认就走」的接口干这件事，等于把一次管理动作降级成一次误点。
    """

    id: str | None = None
    entry: dict[str, Any] | None = None
    confirm: bool = False


# ---- 目录 ----


@router.get("/api/v1/cli-apps")
async def list_apps(q: str = "", installed: bool = False):
    """目录（可按名字/用途搜索）：每行带安装状态与程序路径。"""
    service = get_cli_app_service()
    rows = service.list(q, installed_only=installed)
    return {
        "apps": rows,
        "total": len(rows),
        "installed_count": sum(1 for row in rows if row["installed"]),
        "catalog": catalog_meta(),
    }


@router.get("/api/v1/cli-apps/{app_id}")
async def get_app(app_id: str):
    """一件应用的详情：参数模板、用法指南、安装日志结尾（排障就看它）。"""
    service = get_cli_app_service()
    entry = service.entry(app_id, require=False)
    if entry is None:
        return _error(404, "cli_app_not_found", f"目录里没有这件应用：{app_id}")
    data = service.describe(entry)
    data["install_log"] = installer.log_tail(entry)
    return {"app": data}


# ---- 安装与卸载 ----


@router.post("/api/v1/cli-apps")
async def install_app(body: InstallRequestBody):
    """装一件应用（或对自带的应用做一次「找得到吗」的检测）。"""
    if not body.id and not body.entry:
        return _error(
            422, "cli_app_invalid_request", "要么给 id（目录里的），要么给 entry（自加一条）"
        )
    if not body.confirm:
        return _error(
            400,
            "cli_app_confirmation_required",
            "安装会联网下载并写入应用目录，需要用户在界面上确认（confirm=true）",
        )
    service = get_cli_app_service()
    if body.entry:
        try:
            entry, result = await service.install_custom(body.entry)
        except CliAppUnsupportedError as exc:
            return _error(400, "cli_app_unsupported_install", str(exc))
        return _install_response(entry.id, result.as_dict())
    assert body.id is not None  # 上面已经拦过「两个都没给」
    try:
        service.entry(body.id)
    except CliAppNotFoundError as exc:
        return _error(404, "cli_app_not_found", str(exc))
    return _install_response(body.id, (await service.install(body.id)).as_dict())


@router.delete("/api/v1/cli-apps/{app_id}")
async def uninstall_app(app_id: str):
    """卸载：pip/npm 删掉应用目录，自带的应用只从记录里划掉。"""
    service = get_cli_app_service()
    entry = service.entry(app_id, require=False)
    if entry is None:
        return _error(404, "cli_app_not_found", f"目录里没有这件应用：{app_id}")
    result = await service.uninstall(app_id)
    if not result.ok:
        return _error(400, "cli_app_uninstall_failed", result.message, recoverable=True)
    # 审计不在这里补：install.uninstall 已经落过一条 cli_app_uninstall（§11.6）
    return {"app_id": app_id, "result": result.as_dict(), "app": service.describe(entry)}


def _install_response(app_id: str, result: dict[str, Any]):
    """安装结果 → 响应：失败回 400 并把 `install.log` 结尾一起给出去。"""
    service = get_cli_app_service()
    entry = service.entry(app_id, require=False)
    if not result.get("ok"):
        log_tail = installer.log_tail(entry)[-2000:] if entry is not None else ""
        return _error(
            400,
            "cli_app_install_failed",
            str(result.get("message") or "安装失败"),
            recoverable=True,
            details={"install_log": log_tail},
        )
    return {
        "app_id": app_id,
        "result": result,
        "app": service.describe(entry) if entry is not None else None,
    }
