"""技能 REST（§7.16）：技能增删改查 + 市场目录与安装。

路由顺序是硬性的：`/skills/hub` 与 `/skills/hub/{slug}` 必须先于 `/skills/{name}`
声明，否则 "hub" 会被当成一个技能名（照 settings.py:124 的先例——静态路径在
动态段之前）。
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from nnnu.services.settings.service import get_settings_service
from nnnu.services.skills import hub as skill_hub
from nnnu.services.skills.service import SkillExistsError, SkillNotFoundError, SkillService
from nnnu.services.skills.spec import InvalidSkillNameError, SkillFormatError

router = APIRouter()


def _error(
    status: int, code: str, message: str, *, recoverable: bool = False, details: dict | None = None
) -> JSONResponse:
    """§9.1 统一错误信封 {error:{code,message,recoverable}}；details 供安全门报告等附加数据。"""
    error: dict = {"code": code, "message": message, "recoverable": recoverable}
    if details is not None:
        error["details"] = details
    return JSONResponse(status_code=status, content={"error": error})


def _service(http_request: Request) -> SkillService:
    return http_request.app.state.skills


def _hub_url() -> str:
    """远程技能目录 URL（设置区 skills；空 = 只用内置目录）。"""
    return str(get_settings_service().load_area("skills").get("hub_url") or "").strip()


class CreateSkillRequest(BaseModel):
    name: str
    content: str
    files: dict[str, str] | None = None


class UpdateSkillRequest(BaseModel):
    enabled: bool | None = None
    content: str | None = None
    files: dict[str, str] | None = None


class InstallSkillRequest(BaseModel):
    confirm: bool = False


@router.get("/api/v1/skills")
async def list_skills(http_request: Request):
    """技能列表（磁盘 ∪ skills_meta；带启用态、来源与文件数）。"""
    return {"skills": await _service(http_request).list_all()}


# ---- 市场（静态路径，必须先于 /skills/{name} 声明）----


@router.get("/api/v1/skills/hub")
async def list_hub(http_request: Request, refresh: int = 0):
    """市场目录（内置 + 可配置远程；远程故障降级并带 remote_error）。"""
    service = _service(http_request)
    data = await skill_hub.market(_hub_url(), refresh=bool(refresh))
    skills = await service.list_all()
    installed_hubs = {str(item["hub_ref"]) for item in skills if item.get("hub_ref")}
    installed_names = {str(item["name"]) for item in skills}
    for entry in data["entries"]:
        entry["installed"] = entry["slug"] in installed_hubs or entry["slug"] in installed_names
    return data


@router.get("/api/v1/skills/hub/{slug}")
async def hub_detail(slug: str, http_request: Request, refresh: int = 0):
    """市场条目详情：含安装前安全门预扫描报告与来源信息。"""
    entry = await skill_hub.find_entry(_hub_url(), slug, refresh=bool(refresh))
    if entry is None:
        return _error(404, "hub_not_found", f"市场里没有技能：{slug}")
    detail = skill_hub.preview(entry)
    detail["installed"] = await _service(http_request).exists(str(detail.get("skill_name") or slug))
    return detail


@router.post("/api/v1/skills/hub/{slug}/install")
async def install_from_hub(slug: str, body: InstallSkillRequest, http_request: Request):
    """安装市场技能（快照式）；安全门 block 级发现直接拒绝并附全量 findings。"""
    if not body.confirm:
        return _error(400, "confirmation_required", "安装技能前需要用户确认（confirm=true）")
    entry = await skill_hub.find_entry(_hub_url(), slug)
    if entry is None:
        return _error(404, "hub_not_found", f"市场里没有技能：{slug}")
    try:
        detail = await skill_hub.install(entry, _service(http_request))
    except skill_hub.SkillGateError as exc:
        return _error(
            400, "skill_gate_blocked", str(exc), details={"slug": exc.slug, **exc.report.to_dict()}
        )
    except SkillExistsError as exc:
        return _error(409, "skill_exists", str(exc))
    except InvalidSkillNameError as exc:
        return _error(422, "invalid_skill_name", str(exc))
    except ValueError as exc:  # 走到这里基本只剩 SKILL.md 格式问题（gate 已先扫过）
        return _error(422, "skill_invalid_frontmatter", str(exc))
    return {"skill": detail}


# ---- 技能 CRUD ----


@router.post("/api/v1/skills")
async def create_skill(body: CreateSkillRequest, http_request: Request):
    """新建本地技能：正文不合规 422；重名 409（不会覆盖既有技能）。"""
    try:
        detail = await _service(http_request).create(body.name, body.content, body.files)
    except InvalidSkillNameError as exc:
        return _error(422, "invalid_skill_name", str(exc))
    except SkillFormatError as exc:
        return _error(422, "skill_invalid_frontmatter", str(exc))
    except SkillExistsError as exc:
        return _error(409, "skill_exists", str(exc))
    except ValueError as exc:  # 引用文件路径/扩展名非法（normalize_relpath）
        return _error(422, "skill_invalid_file", str(exc))
    return {"skill": detail}


@router.get("/api/v1/skills/{name}")
async def get_skill(name: str, http_request: Request):
    """技能详情：正文 + 引用文件清单 + 安全门报告（本地技能只告警不拦）。"""
    try:
        return {"skill": await _service(http_request).get(name)}
    except (SkillNotFoundError, InvalidSkillNameError) as exc:
        return _error(404, "skill_not_found", str(exc))


@router.patch("/api/v1/skills/{name}")
async def update_skill(name: str, body: UpdateSkillRequest, http_request: Request):
    """启停 / 改正文 / 加引用文件；响应里的 gate findings 供编辑器红黄条。"""
    try:
        detail = await _service(http_request).update(
            name, enabled=body.enabled, content=body.content, files=body.files
        )
    except (SkillNotFoundError, InvalidSkillNameError) as exc:
        return _error(404, "skill_not_found", str(exc))
    except SkillFormatError as exc:
        return _error(422, "skill_invalid_frontmatter", str(exc))
    except ValueError as exc:
        return _error(422, "skill_invalid_file", str(exc))
    return {"skill": detail}


@router.delete("/api/v1/skills/{name}")
async def delete_skill(name: str, http_request: Request):
    """删除技能：磁盘目录与 skills_meta 行双清。"""
    try:
        await _service(http_request).delete(name)
    except (SkillNotFoundError, InvalidSkillNameError) as exc:
        return _error(404, "skill_not_found", str(exc))
    return {"deleted": name}
