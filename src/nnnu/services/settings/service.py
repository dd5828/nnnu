"""设置服务：spec 驱动的 JSON 读写 + 草稿-应用两段式（§7.19）。

设计要点（§5 配置铁律 + §8.4）：
- load 每次现读文件、无缓存 → 外部改 JSON 即时生效；
- 加载宽容（未知字段保留、缺字段补默认、非法值回退默认并告警）；
- 落盘原子写（同目录临时文件 + fsync + Path.replace），Windows 下重试；
- secret 字段明文永不落设置 JSON：只存 user-secrets（经 SecretsStore），
  草稿里只记动作标记（set/clear），API 回显只给掩码；
- 草稿-应用：改动先存 draft → apply 时校验（models 区先探测）→ 成功才
  写正式文件并 promote 密钥，失败保留草稿与原配置。
"""

import json
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

from nnnu.runtime import home
from nnnu.services.settings.atomic import atomic_write_json
from nnnu.services.settings.spec import SPECS, AreaSpec, SettingField

logger = logging.getLogger(__name__)

# 草稿里 secret 字段的动作标记（明文密钥只经 SecretsStore 转运）
SECRET_ACTION_SET = "set"
SECRET_ACTION_CLEAR = "clear"

# apply 时探测回调：给定候选配置（不含明文密钥），返回错误文案或 None
ProbeFn = Callable[[dict[str, Any]], Awaitable[str | None]]


def _settings_dir() -> Path:
    return home.get_data_root() / "user" / "settings"


def _area_path(name: str) -> Path:
    return _settings_dir() / f"{name}.json"


def _draft_path(name: str) -> Path:
    return _settings_dir() / "drafts" / f"{name}.json"


def _defaults(spec: AreaSpec) -> dict[str, Any]:
    # secret 字段不进设置 JSON（§5 密钥铁律），默认值集合里不含它们
    return {field.key: field.default for field in spec.fields if field.type != "secret"}


def _validate_field(field: SettingField, value: Any) -> Any:
    """校验并规整单个值，非法值抛 ValueError。"""
    if field.type == "choice":
        choices = field.choices or ()
        if value not in choices:
            raise ValueError(f"{field.key} 必须是 {choices} 之一")
        return value
    if field.type == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{field.key} 必须是整数")
        return value
    if field.type == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{field.key} 必须是数字")
        number = float(value)
        if not 0 <= number <= 2:
            raise ValueError(f"{field.key} 必须在 0 到 2 之间")
        return number
    if field.type == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"{field.key} 必须是布尔值")
        return value
    if field.type == "string":
        if not isinstance(value, str):
            raise ValueError(f"{field.key} 必须是字符串")
        return value
    if field.type == "string_list":
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError(f"{field.key} 必须是字符串数组")
        return value
    if field.type == "secret":
        if not isinstance(value, str):
            raise ValueError(f"{field.key} 必须是字符串")
        return value
    raise ValueError(f"未知字段类型 {field.type}")


def _secret_fields(spec: AreaSpec) -> list[SettingField]:
    return [field for field in spec.fields if field.type == "secret"]


class DraftError(ValueError):
    """草稿缺失/非法等业务错误（API 层转 4xx）。"""


class ProbeError(DraftError):
    """apply 前探测失败：原配置与草稿均未动，用户修正后可重试。"""


class SettingsService:
    """设置服务单例（参考仓库服务层模式：模块级 getter）。"""

    def load_area(self, name: str) -> dict[str, Any]:
        """每次现读文件（无缓存），保证外部改 JSON 即时生效；secret 字段不进返回值。"""
        spec = SPECS.get(name)
        if spec is None:
            raise KeyError(name)
        loaded: dict[str, Any] = {}
        path = _area_path(name)
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    loaded = raw
            except json.JSONDecodeError:
                logger.warning("设置文件 %s 损坏，回退默认值", path)
        merged = {**_defaults(spec), **loaded}
        # 逐字段校验：非法值回退默认并告警（宽容加载）；secret 跳过（只剔除不外传）
        for field in spec.fields:
            if field.type != "secret" and field.key in loaded:
                try:
                    merged[field.key] = _validate_field(field, merged[field.key])
                except ValueError as exc:
                    logger.warning("设置 %s.%s 非法（%s），回退默认", name, field.key, exc)
                    merged[field.key] = field.default
        # secret 明文即使被误写进 JSON 也不外传（load 时统一剔除）
        for field in _secret_fields(spec):
            merged.pop(field.key, None)
        return merged

    def save_area(self, name: str, values: dict[str, Any]) -> dict[str, Any]:
        """局部更新（立即生效，P0 语义）：校验 → 合并 → 原子写 → 回读确认。

        secret 字段一律拒绝（密钥只经草稿-应用流程转运，§7.19）。
        """
        spec = SPECS.get(name)
        if spec is None:
            raise KeyError(name)
        secret_keys = {field.key for field in _secret_fields(spec)}
        if secret_keys & set(values):
            raise ValueError("密钥字段请走草稿-应用流程（draft/apply），不直接写设置")
        current = self.load_area(name)
        for field in spec.fields:
            if field.key in values:
                current[field.key] = _validate_field(field, values[field.key])
        atomic_write_json(_area_path(name), current)
        return self.load_area(name)

    # ---- 草稿-应用两段式（§7.19） ----

    def load_draft(self, name: str) -> dict[str, Any] | None:
        """读草稿（含 secret 动作标记，无明文）；无草稿返回 None，损坏按无草稿处理。"""
        if name not in SPECS:
            raise KeyError(name)
        path = _draft_path(name)
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("草稿文件 %s 损坏，按无草稿处理", path)
            return None
        return raw if isinstance(raw, dict) else None

    def save_draft(self, name: str, values: dict[str, Any]) -> dict[str, Any]:
        """保存草稿：校验候选值（含 secret 字段语义）→ 明文密钥转运进 SecretsStore
        的 pending 槽 → 草稿只落动作标记。返回草稿内容（给 API 回显）。
        """
        spec = SPECS.get(name)
        if spec is None:
            raise KeyError(name)
        current = self.load_area(name)
        merged = dict(current)
        for field in spec.fields:
            if field.key in values:
                merged[field.key] = _validate_field(field, values[field.key])
        # secret 字段 → 转运明文 + 记动作；非 secret 字段直接进草稿
        draft: dict[str, Any] = {}
        for field in spec.fields:
            if field.type != "secret" and field.key in values:
                draft[field.key] = merged[field.key]
        for field in _secret_fields(spec):
            if field.key not in values or values[field.key] is None:
                continue
            raw = values[field.key]
            if raw == SECRET_ACTION_CLEAR:
                draft[field.key] = SECRET_ACTION_CLEAR
                continue
            # 明文新密钥 → pending 槽（域/槽由字段声明，如 llm/api_key→provider、search→search_provider）
            from nnnu.services.secrets.store import get_secrets_store

            store = get_secrets_store()
            store.set_pending(field.secret_domain, self._secret_slot(field, merged), raw)
            draft[field.key] = SECRET_ACTION_SET
        _draft_path(name).parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(_draft_path(name), draft)
        return draft

    def delete_draft(self, name: str) -> None:
        """丢弃草稿；同时清掉本草稿转运过的 pending 密钥，不留孤儿明文。"""
        if name not in SPECS:
            raise KeyError(name)
        draft = self.load_draft(name)
        spec = SPECS[name]
        if draft is not None:
            from nnnu.services.secrets.store import get_secrets_store

            store = get_secrets_store()
            merged = {**self.load_area(name), **dict(draft)}
            for field in _secret_fields(spec):
                if draft.get(field.key) in (SECRET_ACTION_SET, SECRET_ACTION_CLEAR):
                    store.clear_pending(field.secret_domain, self._secret_slot(field, merged))
        _draft_path(name).unlink(missing_ok=True)

    async def apply_draft(self, name: str, probe_fn: ProbeFn | None = None) -> dict[str, Any]:
        """应用草稿：校验 → 探测（失败抛 ProbeError，原配置与草稿均保留）→
        写正式文件 + promote/清除密钥 → 删草稿。返回应用后的值（不含 secret）。
        """
        spec = SPECS.get(name)
        if spec is None:
            raise KeyError(name)
        draft = self.load_draft(name)
        if draft is None:
            raise DraftError("没有待应用的草稿，请先保存草稿")
        current = self.load_area(name)
        candidate = dict(current)
        for field in spec.fields:
            if field.type != "secret" and field.key in draft:
                candidate[field.key] = _validate_field(field, draft[field.key])
        if probe_fn is not None:
            error = await probe_fn(dict(candidate))
            if error is not None:
                raise ProbeError(error)
        # 落盘：secret 动作不进设置 JSON（§5 密钥铁律）
        atomic_write_json(_area_path(name), candidate)
        # 密钥动作：set → pending 提为正式；clear → 删除（apply 成功才动正式槽）
        from nnnu.services.secrets.store import get_secrets_store

        store = get_secrets_store()
        for field in _secret_fields(spec):
            action = draft.get(field.key)
            slot = self._secret_slot(field, candidate)
            if action == SECRET_ACTION_SET:
                store.promote(field.secret_domain, slot)
            elif action == SECRET_ACTION_CLEAR:
                store.clear(field.secret_domain, slot)
        _draft_path(name).unlink(missing_ok=True)
        return self.load_area(name)

    @staticmethod
    def _secret_slot(field: SettingField, merged: dict[str, Any]) -> str:
        """secret 槽名：取同区 secret_slot_of 字段的值（如 provider / search_provider）；
        字段留空时落 "default" 槽（读侧同规则对齐，见 services/search/service.py）。"""
        return str(merged.get(field.secret_slot_of, "") or "default")

    def seed_missing(self) -> None:
        """只播种缺失的设置文件，绝不覆盖用户文件（挂入启动引导）。"""
        for name, spec in SPECS.items():
            path = _area_path(name)
            if not path.exists():
                atomic_write_json(path, _defaults(spec))


_settings_service: SettingsService | None = None


def get_settings_service() -> SettingsService:
    global _settings_service
    if _settings_service is None:
        _settings_service = SettingsService()
    return _settings_service
