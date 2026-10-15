"""命令行应用服务（§7.18）：目录 + 安装记录的门面。

「装没装」的判据是**程序现在找不找得到**（install.is_installed），不是记录里有没有行：
用户手删了 venv、PATH 换了版本，记录都还在，以记录为准会把「能跑」说成「没装」。
记录（`apps.json` 的 `installed`）只用来记「什么时候装的、装在哪儿」，界面显示与排障用。

自加条目也走这里：`add_custom` 收一条 JSON（用户在 API 上贴的），校验后并进 apps.json。
"""

from __future__ import annotations

import time
from typing import Any

from nnnu.services.cli_apps import install as installer
from nnnu.services.cli_apps.catalog import (
    all_entries,
    custom_entries,
    get_entry,
    read_user_file,
    save_custom_entries,
    search,
    write_user_file,
)
from nnnu.services.cli_apps.install import InstallResult, is_installed
from nnnu.services.cli_apps.spec import CliAppEntry, parse_entry, text_for


class CliAppNotFoundError(ValueError):
    """目录里没有这件应用。"""


class CliAppUnsupportedError(ValueError):
    """这件应用我们不代劳安装（目录里标了自装），或者平台对不上。"""


class CliAppService:
    """进程内单例（同 SkillService）：无状态查询 + 安装/卸载动作。"""

    # ---- 查询 ----

    def installed_entries(self) -> list[CliAppEntry]:
        """已装的应用（程序真的在）。"""
        return [entry for entry in all_entries() if is_installed(entry)]

    def installed_ids(self) -> set[str]:
        return {entry.id for entry in self.installed_entries()}

    def entry(self, app_id: str, *, require: bool = True) -> CliAppEntry | None:
        entry = get_entry(app_id)
        if entry is None and require:
            raise CliAppNotFoundError(f"目录里没有这件应用：{app_id}")
        return entry

    def list(self, query: str = "", *, installed_only: bool = False) -> list[dict[str, Any]]:
        """目录（可按名字搜索）+ 状态；`installed_only` 只留已装的（工具的清单用）。"""
        entries = search(query)
        rows: list[dict[str, Any]] = []
        for entry in entries:
            installed = is_installed(entry)
            if installed_only and not installed:
                continue
            rows.append(self.describe(entry, installed=installed))
        rows.sort(key=lambda row: (not row["installed"], row["name"]))
        return rows

    def describe(self, entry: CliAppEntry, *, installed: bool | None = None) -> dict[str, Any]:
        data = entry.as_dict()
        found = installer.resolve_program(entry)
        data["installed"] = is_installed(entry) if installed is None else installed
        data["program_path"] = str(found or "")
        data["platform_supported"] = installer.platform_supported(entry)
        record = (read_user_file().get("installed") or {}).get(entry.id) or {}
        data["installed_at"] = record.get("installed_at")
        return data

    def describe_text(self, entry: CliAppEntry, lang: str) -> str:
        """一行给人/模型看的说明（工具输出用）。"""
        return f"{entry.name}（{entry.id}）：{text_for(entry.description, lang)}"

    # ---- 安装与卸载 ----

    async def install(self, app_id: str) -> InstallResult:
        entry = self.entry(app_id)
        assert entry is not None  # require=True 时要么给对象要么抛
        result = await installer.install(entry)
        if result.ok:
            self._remember(entry, result)
        return result

    async def install_custom(self, payload: dict[str, Any]) -> tuple[CliAppEntry, InstallResult]:
        """用户自加一件应用并立刻装：先校验入参（形状不对就不落盘），再装。"""
        try:
            entry = parse_entry({**payload, "origin": "local"}, origin="local")
        except ValueError as exc:
            raise CliAppUnsupportedError(str(exc)) from exc
        result = await installer.install(entry)
        if result.ok:
            entries = {item.id: item for item in custom_entries()}
            entries[entry.id] = entry
            save_custom_entries(list(entries.values()))
            self._remember(entry, result)
        return entry, result

    async def uninstall(self, app_id: str) -> InstallResult:
        entry = self.entry(app_id)
        assert entry is not None
        result = await installer.uninstall(entry)
        if result.ok:
            data = read_user_file()
            installed = dict(data.get("installed") or {})
            installed.pop(app_id, None)
            data["installed"] = installed
            write_user_file(data)
        return result

    def _remember(self, entry: CliAppEntry, result: InstallResult) -> None:
        data = read_user_file()
        installed = dict(data.get("installed") or {})
        installed[entry.id] = {
            "kind": result.kind,
            "program": result.program,
            "installed_at": int(time.time()),
        }
        data["installed"] = installed
        write_user_file(data)


_service: CliAppService | None = None


def get_cli_app_service() -> CliAppService:
    global _service
    if _service is None:
        _service = CliAppService()
    return _service


def set_cli_app_service(service: CliAppService | None) -> None:
    global _service
    _service = service


__all__ = [
    "CliAppNotFoundError",
    "CliAppService",
    "CliAppUnsupportedError",
    "get_cli_app_service",
    "set_cli_app_service",
]
