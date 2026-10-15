"""技能服务（§7.16）：skills_meta 元数据 + 磁盘正文的对账与增删改查。

列表以磁盘为准、DB 补元数据：用户手动往 data/user/skills/ 丢一个目录也算技能
（自动补登为 local 行）；DB 有行但目录没了 → `missing: true`（用户手删目录的
对账）。安装/卸载/编辑/启停/拦截全部落审计（§11.6）。

本地自建/编辑**不硬拦**安全门——findings 照带，编辑器展示；硬拦只在市场安装
（hub.py 的 install）。
"""

from __future__ import annotations

import logging
import time

from nnnu.runtime.home import get_data_root
from nnnu.services.audit import audit_log
from nnnu.services.sessions.db import Database
from nnnu.services.skills.gate import scan_skill
from nnnu.services.skills.spec import (
    SKILL_MD,
    SKILL_NAME_RE,
    InvalidSkillNameError,
    SkillFormatError,
    parse_skill_md,
)
from nnnu.services.skills.store import SkillStore

logger = logging.getLogger(__name__)


class SkillNotFoundError(ValueError):
    """技能不存在（磁盘与库都没有）。"""


class SkillExistsError(ValueError):
    """技能已存在（磁盘目录或库里有行）。"""


class SkillService:
    def __init__(self, db: Database, *, store: SkillStore | None = None):
        self._db = db
        self._store = store or SkillStore(get_data_root())
        # note_payload 的 mtime 缓存（每回合渲染系统提示会问一次）
        self._note_sig: tuple[tuple[str, float], ...] | None = None
        self._note_cache: list[dict] | None = None

    @property
    def store(self) -> SkillStore:
        return self._store

    # ---- 查询 ----

    async def exists(self, name: str) -> bool:
        if not SKILL_NAME_RE.match(name or ""):
            return False
        if self._store.exists(name):
            return True
        return await self._row(name) is not None

    async def list_all(self) -> list[dict]:
        """全部技能（磁盘 ∪ 库）；磁盘有库里没有的自动补登为本地技能。

        （不叫 list：类里有个同名方法会把注解里的 list[...] 顶掉，mypy 过不了。）
        """
        disk = set(self._store.list_names())
        rows = {str(r["name"]): r for r in await self._db.fetch_all("SELECT * FROM skills_meta")}
        for name in sorted(disk - rows.keys()):
            await self._db.execute(
                "INSERT OR IGNORE INTO skills_meta(name, source, enabled, installed_at) VALUES(?, 'local', 1, ?)",
                (name, time.time()),
            )
            rows[name] = {
                "name": name,
                "source": "local",
                "hub_ref": None,
                "enabled": 1,
                "installed_at": time.time(),
            }
        return [
            self._summarize(name, rows.get(name), name not in disk)
            for name in sorted(set(disk) | set(rows))
        ]

    async def get(self, name: str) -> dict:
        """详情：正文 + 引用文件清单 + 安全门报告 + 来源快照。"""
        if not await self.exists(name):
            raise SkillNotFoundError(f"技能不存在：{name}")
        row = await self._row(name)
        if not self._store.exists(name):
            return {
                "name": name,
                "description": "",
                "when_to_use": "",
                "source": str(row["source"]) if row else "local",
                "hub_ref": row["hub_ref"] if row else None,
                "enabled": bool(row["enabled"]) if row else False,
                "installed_at": row["installed_at"] if row else None,
                "missing": True,
                "broken": "技能目录已不在磁盘上（可能被手工删除）",
                "content": "",
                "files": [],
                "gate": None,
                "provenance": None,
            }
        content = self._store.read(name)
        try:
            front, _ = parse_skill_md(content)
            broken: str | None = None
        except SkillFormatError as exc:
            front, broken = None, str(exc)
        ref_paths = self._store.list_ref_files(name)
        ref_files: dict[str, str] = {}
        for rel in ref_paths:
            try:
                ref_files[rel] = self._store.read_ref_file(name, rel)
            except OSError as exc:  # 读不动就跳过，不拖垮详情页
                logger.warning("技能引用文件读取失败：%s/%s（%s）", name, rel, exc)
        report = scan_skill(content, ref_files, total_bytes=self._store.total_bytes(name))
        return {
            "name": name,
            "description": front.description if front else "",
            "when_to_use": front.when_to_use if front else "",
            "source": str(row["source"]) if row else "local",
            "hub_ref": row["hub_ref"] if row else None,
            "enabled": bool(row["enabled"]) if row else True,
            "installed_at": row["installed_at"] if row else None,
            "missing": False,
            "broken": broken,
            "content": content,
            "files": [
                {"path": rel, "size": len(ref_files.get(rel, "").encode("utf-8"))}
                for rel in ref_paths
            ],
            "gate": report.to_dict(),
            "provenance": self._store.read_provenance(name),
        }

    async def enabled_names(self) -> list[str]:
        """启用且磁盘在位的技能名（挂载门用；目录没了视为不可用，fail-closed）。"""
        rows = await self._db.fetch_all("SELECT name FROM skills_meta WHERE enabled = 1")
        out = []
        for row in rows:
            name = str(row["name"])
            if not SKILL_NAME_RE.match(name):
                continue
            try:
                if self._store.exists(name):
                    out.append(name)
            except InvalidSkillNameError:
                continue
        return sorted(out)

    async def note_payload(self) -> list[dict]:
        """启用技能的 name/description/when_to_use（系统提示清单用；mtime 缓存）。"""
        names = tuple(await self.enabled_names())
        signature = tuple((name, self._skill_mtime(name)) for name in names)
        if self._note_cache is not None and self._note_sig == signature:
            return self._note_cache
        payload: list[dict] = []
        for name in names:
            try:
                front, _ = parse_skill_md(self._store.read(name))
            except (OSError, SkillFormatError, InvalidSkillNameError) as exc:
                logger.warning("技能内容不可用，跳过注入：%s（%s）", name, exc)
                continue
            payload.append(
                {"name": name, "description": front.description, "when_to_use": front.when_to_use}
            )
        self._note_sig = signature
        self._note_cache = payload
        return payload

    # ---- 写入 ----

    async def create(
        self,
        name: str,
        content: str,
        files: dict[str, str] | None = None,
        *,
        source: str = "local",
        hub_ref: str | None = None,
        provenance: dict | None = None,
    ) -> dict:
        if not SKILL_NAME_RE.match(name or ""):
            raise InvalidSkillNameError(
                f"技能名不合法：{name!r}（小写字母开头，仅小写字母/数字/连字符，≤48 字符）"
            )
        if await self.exists(name):
            raise SkillExistsError(f"技能已存在：{name}")
        front, _ = parse_skill_md(content)  # 不合格这里就抛 SkillFormatError
        if front.name != name:
            raise SkillFormatError(f"frontmatter 的 name（{front.name}）与技能名（{name}）不一致")
        self._store.write(name, content)
        try:
            for rel, ref_content in (files or {}).items():
                self._store.write_ref_file(name, rel, ref_content)
            if provenance is not None:
                self._store.write_provenance(name, provenance)
            await self._db.execute(
                "INSERT INTO skills_meta(name, source, hub_ref, enabled, installed_at) VALUES(?, ?, ?, 1, ?)",
                (name, source, hub_ref, time.time()),
            )
        except BaseException:  # 落库失败就把刚写的目录撤掉，别留半截
            self._store.remove(name)
            raise
        audit_log("skill_create", name=name, source=source, files=len(files or {}), hub_ref=hub_ref)
        return await self.get(name)

    async def update(
        self,
        name: str,
        *,
        enabled: bool | None = None,
        content: str | None = None,
        files: dict[str, str] | None = None,
    ) -> dict:
        if not await self.exists(name):
            raise SkillNotFoundError(f"技能不存在：{name}")
        if content is not None:
            front, _ = parse_skill_md(content)
            if front.name != name:
                raise SkillFormatError(
                    f"frontmatter 的 name（{front.name}）与技能名（{name}）不一致"
                )
            self._store.write(name, content)
            await self._ensure_row(name)
            audit_log("skill_update", name=name, kind="content")
        if files:
            written = [
                self._store.write_ref_file(name, rel, ref_content)
                for rel, ref_content in files.items()
            ]
            await self._ensure_row(name)
            audit_log("skill_update", name=name, kind="files", files=written)
        if enabled is not None:
            await self._ensure_row(name)
            await self._db.execute(
                "UPDATE skills_meta SET enabled = ? WHERE name = ?", (1 if enabled else 0, name)
            )
            audit_log("skill_toggle", name=name, enabled=bool(enabled))
        return await self.get(name)

    async def delete(self, name: str) -> None:
        if not SKILL_NAME_RE.match(name or ""):
            raise SkillNotFoundError(f"技能不存在：{name}")
        had_dir = self._store.remove(name)
        cursor = await self._db.execute("DELETE FROM skills_meta WHERE name = ?", (name,))
        if not had_dir and cursor.rowcount == 0:
            raise SkillNotFoundError(f"技能不存在：{name}")
        audit_log("skill_delete", name=name, had_dir=had_dir)

    # ---- 内部 ----

    async def _row(self, name: str) -> dict | None:
        return await self._db.fetch_one("SELECT * FROM skills_meta WHERE name = ?", (name,))

    async def _ensure_row(self, name: str) -> None:
        """磁盘上有、库里没有的技能（手工放入的目录）补一行，再改启用态/正文。"""
        if await self._row(name) is None:
            await self._db.execute(
                "INSERT OR IGNORE INTO skills_meta(name, source, enabled, installed_at) VALUES(?, 'local', 1, ?)",
                (name, time.time()),
            )

    def _skill_mtime(self, name: str) -> float:
        try:
            return (self._store.skill_dir(name) / SKILL_MD).stat().st_mtime
        except OSError:
            return 0.0

    def _summarize(self, name: str, row: dict | None, missing: bool) -> dict:
        description = ""
        when_to_use = ""
        broken: str | None = None
        files_count = 0
        if not missing:
            try:
                front, _ = parse_skill_md(self._store.read(name))
                description, when_to_use = front.description, front.when_to_use
            except SkillFormatError as exc:
                broken = str(exc)
            except OSError as exc:
                missing, broken = True, str(exc)
            else:
                files_count = len(self._store.list_ref_files(name))
        return {
            "name": name,
            "description": description,
            "when_to_use": when_to_use,
            "source": str(row["source"]) if row else "local",
            "hub_ref": row["hub_ref"] if row else None,
            "enabled": bool(row["enabled"]) if row else False,
            "installed_at": row["installed_at"] if row else None,
            "files_count": files_count,
            "missing": missing,
            "broken": broken,
        }


# ---- 单例装配（工具/挂载门用）——照 notebook 的存取器模式 ----

_skill_service: SkillService | None = None


def get_skill_service() -> SkillService:
    if _skill_service is None:
        raise RuntimeError("技能服务未装配（lifespan 未调用 set_skill_service）")
    return _skill_service


def set_skill_service(service: SkillService | None) -> None:
    global _skill_service
    _skill_service = service


__all__ = [
    "SkillExistsError",
    "SkillNotFoundError",
    "SkillService",
    "get_skill_service",
    "set_skill_service",
]
