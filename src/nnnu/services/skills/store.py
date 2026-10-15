"""技能目录读写（data/user/skills/<name>/，§8.1 目录已在）。

文件系统是技能正文的权威存储；skills_meta 表只存元数据与启用态。所有公开
方法先过 SKILL_NAME_RE、再做 resolve 防穿越——即使名字来自 API 参数，路径也
保证落在 skills 根目录内。写盘走原子写（同目录临时文件 + replace）。

引用文件（剧本附带的模板/清单）相对技能目录存放：禁绝对路径、禁 .. 段、
禁二进制扩展名、单文件与总数有上限（spec.py 常量）。
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

from nnnu.services.settings.atomic import atomic_write_json, atomic_write_text
from nnnu.services.skills.spec import (
    BINARY_EXTS,
    MAX_REF_FILE_BYTES,
    PROVENANCE_FILE,
    SKILL_MD,
    SKILL_NAME_RE,
    InvalidSkillNameError,
)

logger = logging.getLogger(__name__)


def normalize_relpath(relpath: str) -> str:
    """引用文件相对路径归一化；非法（绝对路径/.. 段/留空/覆盖 SKILL.md）抛 ValueError。"""
    rel = (relpath or "").replace("\\", "/").strip().strip("/")
    parts = [part for part in rel.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts) or ":" in rel:
        raise ValueError(f"引用文件路径不合法：{relpath!r}")
    normalized = "/".join(parts)
    if (
        normalized == SKILL_MD
        or normalized == PROVENANCE_FILE
        or any(part.startswith(".") for part in parts)
    ):
        raise ValueError(f"引用文件路径不合法：{relpath!r}")
    return normalized


class SkillStore:
    """技能目录的文件系统层（纯同步；服务层负责与 skills_meta 对账）。"""

    def __init__(self, data_root: str | Path):
        self._root = Path(data_root) / "user" / "skills"

    @property
    def root(self) -> Path:
        return self._root

    def skill_dir(self, name: str) -> Path:
        if not SKILL_NAME_RE.match(name or ""):
            raise InvalidSkillNameError(f"技能名不合法：{name!r}")
        root = self._root.resolve()
        path = (root / name).resolve()
        if not path.is_relative_to(root) or path == root:  # 正则已排除穿越，这里兜第二层
            raise InvalidSkillNameError(f"技能名不合法：{name!r}")
        return path

    def exists(self, name: str) -> bool:
        return (self.skill_dir(name) / SKILL_MD).is_file()

    def list_names(self) -> list[str]:
        """磁盘上的全部技能目录（含 SKILL.md；不校验内容是否可解析）。"""
        if not self._root.is_dir():
            return []
        return sorted(
            child.name
            for child in self._root.iterdir()
            if child.is_dir() and SKILL_NAME_RE.match(child.name) and (child / SKILL_MD).is_file()
        )

    def read(self, name: str) -> str:
        return (self.skill_dir(name) / SKILL_MD).read_text(encoding="utf-8")

    def write(self, name: str, content: str) -> None:
        target = self.skill_dir(name)
        target.mkdir(parents=True, exist_ok=True)
        atomic_write_text(target / SKILL_MD, content)

    def write_ref_file(self, name: str, relpath: str, content: str) -> str:
        rel = normalize_relpath(relpath)
        if Path(rel).suffix.lower() in BINARY_EXTS:
            raise ValueError(f"引用文件扩展名不允许：{rel}")
        if len(content.encode("utf-8")) > MAX_REF_FILE_BYTES:
            raise ValueError(f"引用文件超过 {MAX_REF_FILE_BYTES // 1024}KB：{rel}")
        atomic_write_text(self.skill_dir(name) / rel, content)
        return rel

    def list_ref_files(self, name: str) -> list[str]:
        """引用文件相对路径列表（不含 SKILL.md 与隐藏文件）。"""
        target = self.skill_dir(name)
        if not target.is_dir():
            return []
        out: list[str] = []
        for path in sorted(target.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(target).as_posix()
            if rel == SKILL_MD or any(part.startswith(".") for part in rel.split("/")):
                continue
            out.append(rel)
        return out

    def read_ref_file(self, name: str, relpath: str) -> str:
        rel = normalize_relpath(relpath)
        path = self.skill_dir(name) / rel
        if not path.is_file():
            raise FileNotFoundError(rel)
        return path.read_text(encoding="utf-8", errors="replace")

    def total_bytes(self, name: str) -> int:
        target = self.skill_dir(name)
        if not target.is_dir():
            return 0
        return sum(path.stat().st_size for path in target.rglob("*") if path.is_file())

    def remove(self, name: str) -> bool:
        target = self.skill_dir(name)
        if not target.is_dir():
            return False
        shutil.rmtree(target)
        return True

    def read_provenance(self, name: str) -> dict | None:
        path = self.skill_dir(name) / PROVENANCE_FILE
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:  # 坏文件不阻塞列表，日志可见
            logger.warning("技能来源快照读取失败：%s（%s）", path, exc)
            return None
        return data if isinstance(data, dict) else None

    def write_provenance(self, name: str, data: dict) -> None:
        atomic_write_json(self.skill_dir(name) / PROVENANCE_FILE, data)
