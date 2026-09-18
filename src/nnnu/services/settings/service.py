"""设置服务：spec 驱动的 JSON 读写。

设计要点（§5 配置铁律 + §8.4）：
- load 每次现读文件、无缓存 → 外部改 JSON 即时生效；
- 加载宽容（未知字段保留、缺字段补默认、非法值回退默认并告警）；
- 落盘原子写（同目录临时文件 + fsync + Path.replace），Windows 下重试。
"""

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from nnnu.runtime import home
from nnnu.services.settings.spec import SPECS, AreaSpec, SettingField

logger = logging.getLogger(__name__)


def _settings_dir() -> Path:
    return home.get_data_root() / "user" / "settings"


def _area_path(name: str) -> Path:
    return _settings_dir() / f"{name}.json"


def _defaults(spec: AreaSpec) -> dict[str, Any]:
    return {field.key: field.default for field in spec.fields}


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
    raise ValueError(f"未知字段类型 {field.type}")


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    """原子写 JSON；Windows 下 PermissionError（杀软/句柄瞬锁）指数退避重试。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    for attempt in range(3):
        try:
            fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(text)
                    fh.flush()
                    os.fsync(fh.fileno())
                Path(tmp_name).replace(path)
            except BaseException:
                Path(tmp_name).unlink(missing_ok=True)
                raise
            return
        except PermissionError:
            if attempt == 2:
                raise
            time.sleep(0.05 * (attempt + 1))


class SettingsService:
    """设置服务单例（参考仓库服务层模式：模块级 getter）。"""

    def load_area(self, name: str) -> dict[str, Any]:
        """每次现读文件（无缓存），保证外部改 JSON 即时生效。"""
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
        # 逐字段校验：非法值回退默认并告警（宽容加载）
        for field in spec.fields:
            if field.key in loaded:
                try:
                    merged[field.key] = _validate_field(field, merged[field.key])
                except ValueError as exc:
                    logger.warning("设置 %s.%s 非法（%s），回退默认", name, field.key, exc)
                    merged[field.key] = field.default
        return merged

    def save_area(self, name: str, values: dict[str, Any]) -> dict[str, Any]:
        """局部更新：校验 → 合并（文件中的未知键保留）→ 原子写 → 回读确认。"""
        spec = SPECS.get(name)
        if spec is None:
            raise KeyError(name)
        current = self.load_area(name)
        for field in spec.fields:
            if field.key in values:
                current[field.key] = _validate_field(field, values[field.key])
        _atomic_write(_area_path(name), current)
        return self.load_area(name)

    def seed_missing(self) -> None:
        """只播种缺失的设置文件，绝不覆盖用户文件（挂入启动引导）。"""
        for name, spec in SPECS.items():
            path = _area_path(name)
            if not path.exists():
                _atomic_write(path, _defaults(spec))


_settings_service: SettingsService | None = None


def get_settings_service() -> SettingsService:
    global _settings_service
    if _settings_service is None:
        _settings_service = SettingsService()
    return _settings_service
