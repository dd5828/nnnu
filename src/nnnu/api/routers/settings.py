"""设置 API：按区读取/局部更新 + 草稿-应用两段式 + 模型连通性探测（§7.19）。

- PUT /settings/{area}：立即生效（P0 语义保留；secret 字段拒绝——密钥只走草稿流）；
- PUT/GET/DELETE /settings/{area}/draft + POST /settings/{area}/apply：
  改动先存草稿 → apply 校验（models 区先探测）→ 成功才落盘并 promote 密钥，
  探测失败保留草稿与原配置（§7.19"失败回滚并提示"）；
- POST /settings/probe：独立连通性测试（表单"测试连接"按钮），不落任何数据；
- GET 回显密钥永远只有掩码（sk-****abcd），明文不出本进程（§7.19 密钥脱敏）。
"""

import os
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from nnnu.services.llm.probe import probe_models
from nnnu.services.llm.provider_registry import build_registry, find_by_id
from nnnu.services.settings.service import (
    DraftError,
    ProbeError,
    get_settings_service,
)
from nnnu.services.settings.spec import SPECS

router = APIRouter()


def _error(status: int, code: str, message: str) -> JSONResponse:
    """§9.1 统一错误信封 {error:{code,message,recoverable}}。"""
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "recoverable": False}},
    )


def _fields_meta(area: str, values: dict[str, Any]) -> list[dict[str, Any]]:
    """spec 元数据 + 当前值（P2 设置表单自动渲染直接消费；secret 字段不给明文）。"""
    return [
        {
            "key": field.key,
            "type": field.type,
            "label_key": field.label_key,
            "effect": field.effect,
            "choices": list(field.choices) if field.choices else None,
            "current": values.get(field.key) if field.type != "secret" else None,
        }
        for field in SPECS[area].fields
    ]


def _models_secret_summary(values: dict[str, Any]) -> dict[str, Any]:
    """models 区密钥摘要（按当前 provider 槽）：掩码 + 是否已配 + 是否有待应用候选。"""
    from nnnu.services.secrets.store import get_secrets_store

    provider = str(values.get("provider") or "")
    summary = get_secrets_store().summary("llm", provider)
    return {"api_key": summary}


def _resolve_probe_key(provider: str, body_key: str | None = None) -> str | None:
    """探测用密钥：请求体 > 草稿候选 > 已生效 > 环境变量（.env 兜底）。

    探测路径不在 LLM 工厂链里（那边才会加载 .env），这里先自己补一次加载。
    """
    from nnnu.services.llm.env import load_dotenv
    from nnnu.services.secrets.store import get_secrets_store

    if body_key:
        return body_key
    store = get_secrets_store()
    pending = store.get_pending("llm", provider)
    if pending:
        return pending
    active = store.get("llm", provider)
    if active:
        return active
    load_dotenv()
    spec = find_by_id(build_registry(), provider)
    if spec is not None and spec.api_key_env:
        return os.environ.get(spec.api_key_env)
    return None


async def _probe_models_candidate(candidate: dict[str, Any]) -> str | None:
    """apply 前的模型探测（§7.19）：失败返回文案，原配置与草稿都不动。"""
    provider = str(candidate.get("provider") or "")
    base_url = str(candidate.get("base_url") or "").strip() or None
    if not base_url:
        spec = find_by_id(build_registry(), provider)
        base_url = spec.base_url if spec is not None else None
    if not base_url:
        return "base_url 为空，无法探测（自定义端点必须填 base_url）"
    result = await probe_models(base_url, _resolve_probe_key(provider))
    return None if result.ok else result.error


class PutSettingsRequest(BaseModel):
    values: dict[str, Any]


class ProbeRequest(BaseModel):
    provider: str = ""
    base_url: str = ""
    model: str = ""
    api_key: str | None = None


# 注意：/settings/probe 必须声明在 /settings/{area} 之前，否则被吞成 area="probe"
@router.post("/api/v1/settings/probe")
async def probe_settings(body: ProbeRequest):
    provider = body.provider.strip()
    base_url = body.base_url.strip()
    if not base_url:
        spec = find_by_id(build_registry(), provider)
        base_url = spec.base_url or "" if spec is not None else ""
    result = await probe_models(base_url, _resolve_probe_key(provider, body.api_key))
    return {"ok": result.ok, "models": result.models, "error": result.error}


@router.get("/api/v1/settings/{area}")
async def get_settings(area: str):
    try:
        values = get_settings_service().load_area(area)
    except KeyError:
        return _error(404, "unknown_area", f"未知设置区 {area}")
    payload: dict[str, Any] = {"area": area, "values": values, "fields": _fields_meta(area, values)}
    if area == "models":
        payload["secrets"] = _models_secret_summary(values)
    return payload


@router.put("/api/v1/settings/{area}")
async def put_settings(area: str, body: PutSettingsRequest):
    try:
        values = get_settings_service().save_area(area, body.values)
    except KeyError:
        return _error(404, "unknown_area", f"未知设置区 {area}")
    except ValueError as exc:
        return _error(422, "validation_error", str(exc))
    return {"area": area, "values": values}


# ---- 草稿-应用（§7.19） ----


@router.put("/api/v1/settings/{area}/draft")
async def put_draft(area: str, body: PutSettingsRequest):
    try:
        draft = get_settings_service().save_draft(area, body.values)
    except KeyError:
        return _error(404, "unknown_area", f"未知设置区 {area}")
    except ValueError as exc:
        return _error(422, "validation_error", str(exc))
    return {"area": area, "draft": draft}


@router.get("/api/v1/settings/{area}/draft")
async def get_draft(area: str):
    try:
        draft = get_settings_service().load_draft(area)
    except KeyError:
        return _error(404, "unknown_area", f"未知设置区 {area}")
    if draft is None:
        return _error(404, "no_draft", f"设置区 {area} 没有待应用的草稿")
    return {"area": area, "draft": draft}


@router.delete("/api/v1/settings/{area}/draft")
async def delete_draft(area: str):
    try:
        get_settings_service().delete_draft(area)
    except KeyError:
        return _error(404, "unknown_area", f"未知设置区 {area}")
    return {"area": area, "deleted": True}


@router.post("/api/v1/settings/{area}/apply")
async def apply_draft(area: str):
    try:
        probe_fn = _probe_models_candidate if area == "models" else None
        values = await get_settings_service().apply_draft(area, probe_fn=probe_fn)
    except KeyError:
        return _error(404, "unknown_area", f"未知设置区 {area}")
    except ProbeError as exc:
        return _error(422, "probe_failed", str(exc))
    except DraftError as exc:
        return _error(409, "no_draft", str(exc))
    return {"area": area, "values": values}
