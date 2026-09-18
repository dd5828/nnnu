"""设置 API：按区读取/局部更新。

P0 为读写 + 即时生效（save 即应用）；草稿-应用两段式与连通性探测属 P2。
"""

from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from nnnu.services.settings.service import get_settings_service
from nnnu.services.settings.spec import SPECS

router = APIRouter()


def _error(status: int, code: str, message: str) -> JSONResponse:
    """§9.1 统一错误信封 {error:{code,message,recoverable}}。"""
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "recoverable": False}},
    )


def _fields_meta(area: str, values: dict[str, Any]) -> list[dict[str, Any]]:
    """spec 元数据 + 当前值（P2 设置表单自动渲染直接消费）。"""
    return [
        {
            "key": field.key,
            "type": field.type,
            "label_key": field.label_key,
            "effect": field.effect,
            "choices": list(field.choices) if field.choices else None,
            "current": values.get(field.key),
        }
        for field in SPECS[area].fields
    ]


class PutSettingsRequest(BaseModel):
    values: dict[str, Any]


@router.get("/api/v1/settings/{area}")
async def get_settings(area: str):
    try:
        values = get_settings_service().load_area(area)
    except KeyError:
        return _error(404, "unknown_area", f"未知设置区 {area}")
    return {"area": area, "values": values, "fields": _fields_meta(area, values)}


@router.put("/api/v1/settings/{area}")
async def put_settings(area: str, body: PutSettingsRequest):
    try:
        values = get_settings_service().save_area(area, body.values)
    except KeyError:
        return _error(404, "unknown_area", f"未知设置区 {area}")
    except ValueError as exc:
        return _error(422, "validation_error", str(exc))
    return {"area": area, "values": values}
