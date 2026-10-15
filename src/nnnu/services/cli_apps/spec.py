"""命令行应用目录的条目结构与安装计划（§7.18）。

三条贯穿本模块的规矩：

* **安装命令是解析出来的，不是直接跑的**。目录里写的是给人看的一行命令
  （`pip install X`、`npm install -g X`），落成结构化的 `InstallPlan` 之前先过一遍
  形状检查：管道、重定向、`&&`、命令替换一律不收，pip/npm 各只许一个包。
  跑安装的动作在 install.py（**沙箱外**、argv 化、超时 300s），这里只管「能不能装、
  怎么装」这件事的表态。
* **调用参数按模板渲染，模板里只有占位符**。模型给的是 `{input: ..., extra: [...]}`
  这样的字典，`render_argv` 把它填进 `argv_template` 得到 argv 数组——不经过 shell，
  所以参数里带空格、引号、`&&` 都不会变成第二个命令。
* **每个条目都有 `guide`**：首次调用时随输出前缀注入（runner.py），因为模型对
  「这个 CLI 怎么用」的默认记忆经常是编的——现查 `--help` 又太贵。
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

#: 安装种类：已装（探测 PATH）/ pip（装进应用自己的 venv）/ npm（装进应用目录）/ 自带
INSTALL_KINDS = ("preinstalled", "pip", "npm", "bundled")

#: 安装命令里出现这些一律不收：它们是 shell 语法，收下就等于开了一个 shell 口子
UNSAFE_TOKENS = ("|", "&&", ";", ">", "<", "$(", "`", "\n", "&>", "||")

#: 步骤（子命令）形状：`pip install X` / `python -m pip install X` / `npm install -g X`
_PIP_LAUNCHERS = ("pip", "pip3", "python", "python3", "py")

APP_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,40}")
_FULL_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_EMBED_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def text_for(value: Any, lang: str) -> str:
    """双语字段取值：`{"zh":…, "en":…}` 取当前语言，缺了回退另一种，纯字符串原样返回。"""
    if isinstance(value, Mapping):
        text = value.get(lang) or value.get("en") or value.get("zh") or ""
        return str(text)
    return str(value or "")


@dataclass(slots=True)
class InstallPlan:
    """怎么把这件应用弄到手：`kind` 决定走哪条路，`target` 是包名或程序名。"""

    kind: str = "preinstalled"
    target: str = ""
    package_manager: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "target": self.target, "package_manager": self.package_manager}


@dataclass(slots=True)
class CliAppEntry:
    """一条目录条目（`catalogs/cli_apps.json` 的一项，或用户自定义的一条）。"""

    id: str
    name: str
    description: dict[str, str] = field(default_factory=dict)
    category: str = ""
    entry_point: str = ""  # 装好后要跑的程序名（PATH / venv / node_modules/.bin 里找）
    cmd: str = ""  # 程序名覆盖（一般空着，用 entry_point）
    argv_template: list[str] = field(default_factory=list)
    params_schema: dict[str, Any] = field(default_factory=dict)
    guide: dict[str, str] = field(default_factory=dict)
    guide_url: str = ""
    install: InstallPlan = field(default_factory=InstallPlan)
    version: str = ""
    requires: dict[str, str] = field(default_factory=dict)
    homepage: str = ""
    platform: str = "any"
    default_timeout_s: int = 120
    network: bool = False  # 这件应用要不要联网（还要过设置里的沙箱联网总开关）
    origin: str = "local"  # catalog（内置目录）/ local（用户添加）
    source_url: str = ""

    @property
    def program(self) -> str:
        return self.cmd or self.entry_point

    @property
    def params(self) -> dict[str, Any]:
        props = (self.params_schema or {}).get("properties")
        return props if isinstance(props, dict) else {}

    @property
    def required_params(self) -> list[str]:
        required = (self.params_schema or {}).get("required") or []
        return [str(item) for item in required]

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": dict(self.description),
            "category": self.category,
            "entry_point": self.entry_point,
            "cmd": self.cmd,
            "argv_template": list(self.argv_template),
            "params_schema": dict(self.params_schema),
            "guide": dict(self.guide),
            "guide_url": self.guide_url,
            "install": self.install.as_dict(),
            "version": self.version,
            "requires": dict(self.requires),
            "homepage": self.homepage,
            "platform": self.platform,
            "default_timeout_s": self.default_timeout_s,
            "network": self.network,
            "origin": self.origin,
            "source_url": self.source_url,
        }


def plan_install(install_cmd: str, *, entry_point: str = "") -> InstallPlan | None:
    """一行给人看的安装命令 → `InstallPlan`；拿不准的一律返回 None（=不支持）。

    None 不是「报错」，是「这条我们不代劳」：目录里留着它、界面上写明它是自装，
    总比把一句来路不明的 shell 命令塞进 subprocess 强。空命令 = 本机自带
    （用 `shutil.which(entry_point)` 探一下就知道在不在）。
    """
    command = (install_cmd or "").strip()
    if not command:
        return InstallPlan(kind="preinstalled", target=entry_point)
    if any(token in command for token in UNSAFE_TOKENS):
        return None
    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    if not parts:
        return None

    head = parts[0]
    if head in _PIP_LAUNCHERS:
        if head in ("pip", "pip3"):
            packages = parts[2:] if len(parts) > 1 and parts[1] == "install" else []
        elif len(parts) >= 4 and parts[1] == "-m" and parts[2] == "pip" and parts[3] == "install":
            packages = parts[4:]
        else:
            return None
        return _single_package("pip", packages)
    if head in ("npm", "pnpm", "yarn"):
        if len(parts) < 3 or parts[1] != "install":
            return None
        return _single_package("npm", parts[2:])
    return None  # brew / cargo / uv / curl / cd … 自己不代劳


def _single_package(kind: str, tokens: list[str]) -> InstallPlan | None:
    """去掉 `-g`/`-U` 之类的开关后**必须只剩一个包**：一次装一堆没法记账。"""
    packages = [token for token in tokens if not token.startswith("-")]
    if len(packages) != 1:
        return None
    return InstallPlan(kind=kind, target=packages[0], package_manager=kind)


def render_argv(entry: CliAppEntry, args: Mapping[str, Any] | None = None) -> list[str]:
    """参数字典 → argv 尾部（不含程序名）；缺必填参数抛 ValueError。

    模板里能写三种东西：
    - `{name}` 单独一个 token：值是列表就展开成多个 token（`{extra}` 就是这么用的）；
      值是空/None 时整个 token 略过——可选开关在模板里可以照写不误；
    - `xxx{name}yyy`：字符串内嵌替换（`--lang={lang}`、`{name}.mp3`）；
    - 字面量：原样带上。
    """
    values: dict[str, Any] = dict(args or {})
    missing = [name for name in entry.required_params if values.get(name) in (None, "", [], {})]
    if missing:
        raise ValueError("缺少必填参数：" + "、".join(missing))

    out: list[str] = []
    spliced_extra = False
    for token in entry.argv_template:
        if token == "{extra}":
            out.extend(str(item) for item in values.get("extra") or [])
            spliced_extra = True
            continue
        full = _FULL_PLACEHOLDER.fullmatch(token)
        if full:
            value = values.get(full.group(1))
            if value in (None, ""):
                continue
            if isinstance(value, (list, tuple)):
                out.extend(str(item) for item in value)
            else:
                out.append(str(value))
            continue
        out.append(_EMBED_PLACEHOLDER.sub(lambda match: str(values.get(match.group(1), "")), token))
    # 模板没给 {extra} 的位置：追加到末尾（大多数 CLI 的附加开关都在最后）
    if not spliced_extra:
        out.extend(str(item) for item in values.get("extra") or [])
    return out


def parse_entry(data: Mapping[str, Any], *, origin: str = "local") -> CliAppEntry:
    """目录 JSON 的一项 → 条目对象；形状不对就抛 ValueError（目录是数据，必须能挡住手改坏）。"""
    entry_id = str(data.get("id") or "").strip()
    if not APP_ID_RE.fullmatch(entry_id):
        raise ValueError(f"应用 id 不合法：{entry_id!r}（小写字母开头，只许小写字母/数字/连字符）")
    entry_point = str(data.get("entry_point") or "").strip()
    if not entry_point and not str(data.get("cmd") or "").strip():
        raise ValueError(f"{entry_id}: 必须给 entry_point 或 cmd")

    raw_install = data.get("install") or {}
    kind = str(raw_install.get("kind") or "preinstalled")
    if kind not in INSTALL_KINDS:
        raise ValueError(f"{entry_id}: 不认识的安装方式 {kind!r}")
    install = InstallPlan(
        kind=kind,
        target=str(raw_install.get("target") or ""),
        package_manager=str(raw_install.get("package_manager") or ""),
    )
    if kind in ("pip", "npm") and not install.target:
        raise ValueError(f"{entry_id}: {kind} 安装必须给 target（包名）")

    template = [str(token) for token in (data.get("argv_template") or [])]
    schema = data.get("params_schema") or {}
    if not isinstance(schema, dict):
        raise ValueError(f"{entry_id}: params_schema 必须是对象")
    required = [str(item) for item in (schema.get("required") or [])]
    props: dict[str, object] = schema.get("properties") or {}
    if not isinstance(props, dict):
        props = {}
    for name in required:
        if name not in props:
            raise ValueError(f"{entry_id}: 必填参数 {name!r} 没有在 params_schema 里定义")

    timeout = int(data.get("default_timeout_s") or 120)
    return CliAppEntry(
        id=entry_id,
        name=str(data.get("name") or entry_id),
        description=dict(data.get("description") or {}),
        category=str(data.get("category") or ""),
        entry_point=entry_point,
        cmd=str(data.get("cmd") or ""),
        argv_template=template,
        params_schema=schema,
        guide=dict(data.get("guide") or {}),
        guide_url=str(data.get("guide_url") or ""),
        install=install,
        version=str(data.get("version") or ""),
        requires=dict(data.get("requires") or {}),
        homepage=str(data.get("homepage") or ""),
        platform=str(data.get("platform") or "any"),
        default_timeout_s=timeout,
        network=bool(data.get("network", False)),
        origin=origin,
        source_url=str(data.get("source_url") or ""),
    )
