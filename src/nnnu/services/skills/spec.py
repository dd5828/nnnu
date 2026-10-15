"""技能格式（§7.16）：SKILL.md 解析与校验。

技能 = 一个目录：SKILL.md（YAML frontmatter + Markdown 正文）+ 可选引用文件。
frontmatter 必须给 name（线上标识，也是目录名）与 description（模型靠它判断
该不该用这个技能）；when_to_use 可选、给人看。其余 frontmatter 键（tags、
requires 之类）宽容保留在 extra 里——兼容 Agent-Skills 与上游技能包，不强制。

体积上限：单 SKILL.md ≤256KB、整包 ≤2MB、引用文件 ≤20 个、单引用文件 ≤512KB。
超限由 gate 判 oversized——本地保存放行并告警，市场安装拒绝。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import yaml

# 技能名 = 目录名 = 线上标识：小写字母开头，仅小写字母/数字/连字符，≤48 字符
SKILL_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,47}$")

# 体积上限（见模块 docstring）
MAX_SKILL_MD_BYTES = 256 * 1024
MAX_SKILL_TOTAL_BYTES = 2 * 1024 * 1024
MAX_REF_FILES = 20
MAX_REF_FILE_BYTES = 512 * 1024
MAX_DESCRIPTION_CHARS = 1024
MAX_WHEN_TO_USE_CHARS = 512

# 引用文件禁用的扩展名（可执行/压缩载荷；store 写盘与 gate 扫描共用）
BINARY_EXTS = frozenset(
    {
        ".exe",
        ".dll",
        ".so",
        ".dylib",
        ".msi",
        ".bat",
        ".cmd",
        ".ps1",
        ".vbs",
        ".scr",
        ".jar",
        ".zip",
        ".7z",
    }
)

SKILL_MD = "SKILL.md"
PROVENANCE_FILE = ".provenance.json"  # 市场安装的来源快照（本地技能没有）

_FRONTMATTER_KEYS = ("name", "description", "when_to_use")


class SkillFormatError(ValueError):
    """SKILL.md 不符合格式（缺 frontmatter / 非法 YAML / 字段越界 / 正文为空）。"""


class InvalidSkillNameError(ValueError):
    """技能名不合法（不匹配 SKILL_NAME_RE）。"""


@dataclass(frozen=True, slots=True)
class SkillFrontmatter:
    name: str
    description: str
    when_to_use: str = ""
    extra: dict[str, Any] = field(default_factory=dict)  # 三件套之外的键，原样保留


def parse_skill_md(text: str) -> tuple[SkillFrontmatter, str]:
    """解析 SKILL.md → (frontmatter, 正文)；不合格抛 SkillFormatError。"""
    stripped = text.lstrip("\ufeff")  # 容忍 BOM（记事本存过的文件常有）
    lines = stripped.splitlines()
    if not lines or lines[0].strip() != "---":
        raise SkillFormatError("SKILL.md 必须以 YAML frontmatter（首行 ---）开头")
    close = next(
        (idx for idx in range(1, len(lines)) if lines[idx].strip() in ("---", "...")), None
    )
    if close is None:
        raise SkillFormatError("frontmatter 没有结束行（---）")
    raw = "\n".join(lines[1:close])
    try:
        loaded = yaml.safe_load(raw) if raw.strip() else {}
    except yaml.YAMLError as exc:
        raise SkillFormatError(f"frontmatter 不是合法 YAML：{exc}") from exc
    if not isinstance(loaded, dict):
        raise SkillFormatError("frontmatter 必须是键值映射")

    name = loaded.get("name")
    if not isinstance(name, str) or not SKILL_NAME_RE.match(name):
        raise SkillFormatError(
            "frontmatter 的 name 缺失或不合规（小写字母开头，仅小写字母/数字/连字符，≤48 字符）"
        )
    description = loaded.get("description")
    if not isinstance(description, str) or not description.strip():
        raise SkillFormatError("frontmatter 的 description 缺失或为空")
    if len(description) > MAX_DESCRIPTION_CHARS:
        raise SkillFormatError(f"description 超过 {MAX_DESCRIPTION_CHARS} 字符")
    when_to_use = loaded.get("when_to_use")
    if when_to_use is None:
        when_to_use = ""
    if not isinstance(when_to_use, str):
        raise SkillFormatError("when_to_use 必须是字符串")
    if len(when_to_use) > MAX_WHEN_TO_USE_CHARS:
        raise SkillFormatError(f"when_to_use 超过 {MAX_WHEN_TO_USE_CHARS} 字符")

    body = "\n".join(lines[close + 1 :]).strip("\n")
    if not body.strip():
        raise SkillFormatError("正文为空——技能正文是给模型看的操作剧本")

    extra = {key: value for key, value in loaded.items() if key not in _FRONTMATTER_KEYS}
    return (
        SkillFrontmatter(
            name=name, description=description.strip(), when_to_use=when_to_use.strip(), extra=extra
        ),
        body,
    )


def compose_skill_md(name: str, description: str, body: str, *, when_to_use: str = "") -> str:
    """按规范拼一份 SKILL.md（内置目录与测试用）。编辑器保存的是全文，不走这里。

    YAML 转义交给 safe_dump——前端自己拼 YAML 容易踩引号/换行的坑。
    """
    front: dict[str, Any] = {"name": name, "description": description}
    if when_to_use:
        front["when_to_use"] = when_to_use
    head = yaml.safe_dump(front, allow_unicode=True, sort_keys=False).strip()
    return f"---\n{head}\n---\n\n{body.strip()}\n"


def note_line(fm: SkillFrontmatter) -> str:
    """启用技能在系统提示清单里的单行式样（§6.3「有技能 → read_skill」）。"""
    if fm.when_to_use:
        return f"- {fm.name}：{fm.description}（何时用：{fm.when_to_use}）"
    return f"- {fm.name}：{fm.description}"
