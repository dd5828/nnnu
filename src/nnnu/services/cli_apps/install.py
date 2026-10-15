"""命令行应用的安装与卸载（§7.18）：探测、pip、npm、自带。

**安装动作在沙箱外执行**，这不是疏忽：pip/npm 要联网、要写 site-packages，而沙箱
默认断网（§11.2）；再者它是用户在界面上点了确认的**管理动作**，不是模型自己想跑的
东西。代价是这里没有沙箱配额兜着，所以另有三道：argv 化（`plan_install` 已经把
命令解析成一包一包，见 spec.py）、超时 300 秒、全程落 `install.log` + 审计。

装到哪儿也定死了：`data/user/cli-apps/<id>/` 下面各装各的（pip 一个 venv、npm 一个
node_modules），互不污染、卸载就是删目录，也不用碰系统 Python / 全局 npm。
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from nnnu.services.cli_apps.catalog import app_dir
from nnnu.services.cli_apps.spec import CliAppEntry

logger = logging.getLogger(__name__)

INSTALL_TIMEOUT_S = 300.0
LOG_TAIL_CHARS = 4000
#: PATH 索引的存活时间：够一屏目录查完，也够用户装完东西再点一次「检测」
EXEC_INDEX_TTL_S = 30.0


@dataclass(slots=True)
class InstallResult:
    ok: bool
    kind: str = ""
    message: str = ""
    program: str = ""  # 装好后程序所在的完整路径（空 = 还没找到）
    log_path: str = ""
    duration_s: float = 0.0

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "kind": self.kind,
            "message": self.message,
            "program": self.program,
            "log_path": self.log_path,
            "duration_s": round(self.duration_s, 2),
        }


def current_platform() -> str:
    if os.name == "nt":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def platform_supported(entry: CliAppEntry) -> bool:
    return entry.platform in ("", "any", current_platform())


def venv_dir(entry: CliAppEntry) -> Path:
    return app_dir(entry.id) / "venv"


def venv_python(entry: CliAppEntry) -> Path:
    root = venv_dir(entry) / ("Scripts" if os.name == "nt" else "bin")
    return root / ("python.exe" if os.name == "nt" else "python")


def bin_dir(entry: CliAppEntry) -> Path:
    """应用自己的可执行文件目录（跑的时候挂到 PATH 最前面，见 runner.py）。"""
    if entry.install.kind == "pip":
        return venv_dir(entry) / ("Scripts" if os.name == "nt" else "bin")
    if entry.install.kind == "npm":
        return app_dir(entry.id) / "node_modules" / ".bin"
    return app_dir(entry.id)


def _with_ext(path: Path) -> Path:
    """Windows 上补 `.exe`/`.cmd`：目录里的 entry_point 一般不写扩展名。"""
    if os.name != "nt" or path.suffix:
        return path
    for suffix in (".exe", ".cmd", ".bat"):
        candidate = path.with_name(path.name + suffix)
        if candidate.is_file():
            return candidate
    return path


#: 程序名 → 完整路径；**不在表里就是 PATH 上没有**（表是整份快照，不是「查过的记下来」）。
_exec_index: tuple[float, dict[str, str | None]] = (0.0, {})


def _path_exts() -> tuple[str, ...]:
    raw = os.environ.get("PATHEXT") or ".COM;.EXE;.BAT;.CMD"
    return tuple(item.lower() for item in raw.split(os.pathsep) if item)


def _index_dir(directory: str, index: dict[str, str | None]) -> None:
    """把一个 PATH 目录装进索引：可执行文件的名字（含去扩展名的别名）→ 路径。"""
    try:
        with os.scandir(directory) as entries:
            for item in entries:
                try:
                    if not item.is_file():
                        continue
                except OSError:  # 断链的软链之类，跳过这一条就好
                    continue
                name = item.name
                if os.name == "nt":
                    ext = os.path.splitext(name)[1].lower()
                    if ext not in _path_exts():
                        continue
                    index.setdefault(name.lower(), item.path)
                    index.setdefault(name[: -len(ext)].lower(), item.path)
                else:
                    if not os.access(item.path, os.X_OK):
                        continue
                    index.setdefault(name, item.path)
    except OSError:
        return


def _exec_paths() -> dict[str, str | None]:
    """PATH 上有什么可执行文件的整本索引（TTL 缓存）。

    为什么不直接用 `shutil.which`：它按「程序数 × PATH 段数 × 扩展名」逐个 stat，
    本机实测**一次未命中就是 86ms**（PATH 有 49 段），61 条目录全扫 4.5 秒——
    目录页要卡半天，挂载门每回合还要再付一次。整本索引只扫一遍目录（几十毫秒），
    之后就是字典查。代价是结果有几秒到几十秒的保鲜期，装完东西由安装流程自己清缓存。
    """
    global _exec_index
    stamp, cached = _exec_index
    now = time.monotonic()
    if cached and now - stamp < EXEC_INDEX_TTL_S:
        return cached
    index: dict[str, str | None] = {}
    seen: set[str] = set()
    for directory in (os.environ.get("PATH") or "").split(os.pathsep):
        if not directory or directory in seen:
            continue
        seen.add(directory)
        _index_dir(directory, index)
    _exec_index = (now, index)
    return index


def clear_exec_index() -> None:
    """丢掉 PATH 索引（装机/卸载后调用，别让刚装好的程序在界面上还是「未装」）。"""
    global _exec_index
    _exec_index = (0.0, {})


def which_program(program: str) -> str | None:
    """在 PATH 上找程序：带路径的（绝对/相对）直接问 which，纯名字走索引。

    纯名字**不在索引里就是没有**，不退回逐段 `shutil.which`：索引是一整份 PATH
    快照（见 `_exec_paths`），而逐段 which 正是要避开的 86ms/次——61 条目录里
    一大半本机没装，退一次就够把目录页拖成十秒。索引最多旧 30 秒，我们自己装/
    卸的东西会主动清它（`clear_exec_index`）。

    顺带一句：Windows 上 `shutil.which` 还会在 PATH 前面塞当次的工作目录，
    这在服务进程里是不该有的解析口径（跑哪个程序不该取决于服务从哪儿启动）。
    """
    if not program:
        return None
    if os.sep in program or (os.altsep and os.altsep in program) or program.startswith("."):
        return shutil.which(program)
    index = _exec_paths()
    return index.get(program.lower() if os.name == "nt" else program)


def resolve_program(entry: CliAppEntry) -> Path | None:
    """找到能跑的程序：先看应用自己的目录，再看 PATH（自带的工具走 PATH）。"""
    program = entry.program
    if not program:
        return None
    if entry.install.kind in ("pip", "npm"):
        candidate = _with_ext(bin_dir(entry) / program)
        if candidate.is_file():
            return candidate
    if entry.install.kind == "bundled":
        candidate = app_dir(entry.id) / (entry.install.target or program)
        return candidate if candidate.is_file() else None
    found = which_program(program)
    return Path(found) if found else None


def is_installed(entry: CliAppEntry) -> bool:
    """装没装：**程序真的能跑**才算（记录只用来记住日志与卸载方式）。

    以 exe 为准而不是以记录为准，因为「装过一次」和「现在还能用」是两回事——
    用户手删了 venv、PATH 上换了版本，记录都还留着。
    """
    return platform_supported(entry) and resolve_program(entry) is not None


async def _run(
    argv: list[str], *, cwd: Path, log_path: Path, timeout_s: float
) -> tuple[int | None, str]:
    """跑一条安装命令，输出整段落进 install.log（失败时界面要看的就是它）。"""
    # .cmd/.bat 在 Windows 上得经命令解释器（CreateProcess 不认批处理），
    # 参数来自 plan_install 解析过的一包名，不带 shell 语法
    if os.name == "nt" and Path(argv[0]).suffix.lower() in (".cmd", ".bat"):
        argv = [os.environ.get("COMSPEC") or "cmd.exe", "/c", *argv]
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    cwd.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as exc:
        text = f"$ {' '.join(argv)}\n启动失败：{exc}\n"
        _append_log(log_path, text)
        return None, text
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout_s)
        code: int | None = process.returncode
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        code = None
        stdout = b""
        text = f"$ {' '.join(argv)}\n安装超时（{timeout_s:g} 秒），进程已终止\n"
        _append_log(log_path, text)
        return None, text + f"（耗时 {time.monotonic() - started:.1f}s）"
    text = f"$ {' '.join(argv)}\n" + stdout.decode("utf-8", errors="replace")
    _append_log(log_path, text + "\n")
    return code, text


def _append_log(log_path: Path, text: str) -> None:
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(text + "\n")
    except OSError:
        logger.warning("安装日志写不进去：%s", log_path, exc_info=True)


def log_tail(entry: CliAppEntry, limit: int = LOG_TAIL_CHARS) -> str:
    path = app_dir(entry.id) / "install.log"
    if not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-limit:]


async def install(entry: CliAppEntry) -> InstallResult:
    """按 `install.kind` 装一件应用；结果里带一句给人读的话与日志路径。"""
    from nnnu.services.audit import audit_log

    started = time.monotonic()
    log_path = app_dir(entry.id) / "install.log"
    clear_exec_index()  # 装完要立刻看得到结果，别让旧索引说「没装」
    if not platform_supported(entry):
        result = InstallResult(
            ok=False,
            kind=entry.install.kind,
            message=f"这件应用只支持 {entry.platform}，当前系统是 {current_platform()}",
        )
        _audit(entry, result, audit_log, action="cli_app_install")
        return result

    kind = entry.install.kind
    if kind == "preinstalled":
        found = resolve_program(entry)
        result = InstallResult(
            ok=found is not None,
            kind=kind,
            message=(
                f"已找到 {found}" if found else f"没找到 {entry.program}：它得先装好并在 PATH 里"
            ),
            program=str(found or ""),
        )
    elif kind == "bundled":
        found = resolve_program(entry)
        result = InstallResult(
            ok=found is not None,
            kind=kind,
            message=f"已找到 {found}" if found else "自带文件缺失（目录里的安装方式写错了？）",
            program=str(found or ""),
        )
    elif kind == "pip":
        result = await _install_pip(entry, log_path)
    elif kind == "npm":
        result = await _install_npm(entry, log_path)
    else:  # pragma: no cover —— parse_entry 已经挡过，兜底不留白
        result = InstallResult(ok=False, kind=kind, message=f"不认识的安装方式：{kind}")

    result.log_path = str(log_path)
    result.duration_s = time.monotonic() - started
    _audit(entry, result, audit_log, action="cli_app_install")
    return result


async def _install_pip(entry: CliAppEntry, log_path: Path) -> InstallResult:
    python = venv_python(entry)
    if not python.is_file():
        code, text = await _run(
            [sys.executable, "-m", "venv", str(venv_dir(entry))],
            cwd=app_dir(entry.id),
            log_path=log_path,
            timeout_s=INSTALL_TIMEOUT_S,
        )
        if code != 0:
            return InstallResult(ok=False, kind="pip", message=_short(text, "创建 venv 失败"))
    code, text = await _run(
        [str(python), "-m", "pip", "install", entry.install.target],
        cwd=app_dir(entry.id),
        log_path=log_path,
        timeout_s=INSTALL_TIMEOUT_S,
    )
    if code != 0:
        return InstallResult(ok=False, kind="pip", message=_short(text, "pip 安装失败"))
    found = resolve_program(entry)
    if found is None:
        return InstallResult(
            ok=False,
            kind="pip",
            message=f"装完了但没找到 {entry.program}：目录里的 entry_point 可能写错了",
        )
    return InstallResult(ok=True, kind="pip", message=f"装好了：{found}", program=str(found))


async def _install_npm(entry: CliAppEntry, log_path: Path) -> InstallResult:
    npm = shutil.which("npm")
    if not npm:
        return InstallResult(ok=False, kind="npm", message="没找到 npm：先装 Node.js")
    code, text = await _run(
        [npm, "install", "--prefix", str(app_dir(entry.id)), entry.install.target],
        cwd=app_dir(entry.id),
        log_path=log_path,
        timeout_s=INSTALL_TIMEOUT_S,
    )
    if code != 0:
        return InstallResult(ok=False, kind="npm", message=_short(text, "npm 安装失败"))
    found = resolve_program(entry)
    if found is None:
        return InstallResult(
            ok=False,
            kind="npm",
            message=f"装完了但没找到 {entry.program}：目录里的 entry_point 可能写错了",
        )
    return InstallResult(ok=True, kind="npm", message=f"装好了：{found}", program=str(found))


async def uninstall(entry: CliAppEntry) -> InstallResult:
    """卸载：pip/npm 删掉应用目录；自带的应用只从记录里划掉（程序在不在不归我们管）。"""
    from nnnu.services.audit import audit_log

    directory = app_dir(entry.id)
    clear_exec_index()
    removed = False
    if entry.install.kind in ("pip", "npm") and directory.is_dir():
        try:
            shutil.rmtree(directory)
            removed = True
        except OSError as exc:
            result = InstallResult(ok=False, kind=entry.install.kind, message=f"删不掉：{exc}")
            _audit(entry, result, audit_log, action="cli_app_uninstall")
            return result
    result = InstallResult(
        ok=True,
        kind=entry.install.kind,
        message="已卸载（应用自己带的程序不动）" if not removed else "已卸载",
    )
    _audit(entry, result, audit_log, action="cli_app_uninstall")
    return result


def _short(text: str, fallback: str) -> str:
    """把安装输出压成一句能看的：末尾几行通常就是那个真正的报错。"""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    tail = " / ".join(lines[-2:])
    return f"{fallback}：{tail[:300]}" if tail else fallback


def _audit(entry: CliAppEntry, result: InstallResult, audit_log, *, action: str) -> None:
    audit_log(
        action,
        app=entry.id,
        kind=result.kind,
        target=entry.install.target,
        ok=result.ok,
        message=result.message[:200],
    )
