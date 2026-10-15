"""跑一件命令行应用（§7.18）：模板渲染 → 沙箱 argv 执行 → 结果截断。

三种「为什么」：

* **为什么 argv 而不是命令串**：这里的参数是**模型出的**，程序是目录里定死的，
  这个组合最不该过 shell。`ExecRequest(argv=[...])` 直 exec，参数里带 `&&`、引号、
  空格都只是参数（§11.2 的沙箱协议为此加了一个 argv 分支）。
* **为什么首跑要注入指南**：模型对某个 CLI 的记忆经常是编的（版本对不上、参数早改名）。
  指南跟着输出走一次，比让它连续试错便宜得多；同一进程里只注入一次（`_guide_seen`），
  第二次就只留一句「参考首跑指南」的短提示——每轮都贴一遍全文是纯浪费。
* **为什么截断 20k**：CLI 的输出动辄几十万字符（转码日志、目录清单），全塞回模型
  既贵又没用，尾巴上那句「已截断」比沉默强。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from nnnu.services.cli_apps.install import bin_dir, resolve_program
from nnnu.services.cli_apps.spec import CliAppEntry, render_argv, text_for
from nnnu.services.sandbox.service import get_sandbox_service
from nnnu.services.sandbox.spec import MAX_TIMEOUT_S, ExecRequest

logger = logging.getLogger(__name__)

RUN_OUTPUT_MAX_CHARS = 20000

#: 已经见过指南的应用（进程内）：首跑注入全文，之后只提醒一句
_guide_seen: set[str] = set()


@dataclass(slots=True)
class RunResult:
    ok: bool
    output: str = ""
    argv: list[str] = field(default_factory=list)
    exit_code: int | None = None
    duration_s: float = 0.0
    error: str = ""

    def as_detail(self, entry: CliAppEntry) -> dict:
        return {
            "app": entry.id,
            "argv": self.argv[1:],  # 程序路径对模型没用，省点字数
            "exit_code": self.exit_code,
            "duration_s": round(self.duration_s, 2),
        }


def _render(language: str, key: str, **values: object) -> str:
    from nnnu.services.i18n.prompts import get_prompt_manager

    return get_prompt_manager().render("cli_apps", language, key, **values)


def _format_output(result: RunResult, entry: CliAppEntry, language: str) -> str:
    """拼给模型的文本：首跑指南（若有）→ stdout → stderr → 收场说明。"""
    parts: list[str] = []
    if entry.id not in _guide_seen:
        _guide_seen.add(entry.id)
        guide = text_for(entry.guide, language).strip()
        if guide:
            parts.append(_render(language, "guide_header", app=entry.name) + "\n" + guide.strip())
    else:
        parts.append(_render(language, "guide_seen", app=entry.name))
    if result.output.strip():
        parts.append(result.output.strip())
    if result.error:
        parts.append(_render(language, "run_failed", error=result.error))
    return "\n\n".join(parts)


async def run_app(
    entry: CliAppEntry,
    args: dict | None = None,
    *,
    language: str = "zh",
    timeout_s: float | None = None,
    session_id: str = "",
    turn_id: str = "",
) -> RunResult:
    """跑一次应用；所有失败都落成一条能读的说明（工具层照原样回给模型）。

    三条失败路径（参数不对 / 没装 / 沙箱层出错）都要有 output：工具结果里 output 是
    模型唯一能读到的那段话，只给 ok=False 等于让它对着空白猜。
    """
    try:
        tokens = render_argv(entry, args)
    except ValueError as exc:
        return RunResult(
            ok=False,
            output=_render(language, "bad_args", error=str(exc)),
            error=str(exc),
        )

    program = resolve_program(entry)
    if program is None:
        message = _render(language, "not_installed", app=entry.name, program=entry.program)
        return RunResult(ok=False, argv=[entry.program, *tokens], output=message, error=message)

    argv = [str(program), *tokens]
    extra_env: dict[str, str] = {}
    if entry.install.kind in ("pip", "npm"):
        # 应用自己的 bin 目录挂到 PATH 最前面：它内部再调兄弟程序（ffmpeg 调 ffprobe 之类）也能找到
        base = os.environ.get("PATH", "")
        extra_env["PATH"] = f"{bin_dir(entry)}{os.pathsep}{base}"
    timeout = float(timeout_s or entry.default_timeout_s or 120)
    request = ExecRequest(
        argv=argv,
        timeout_s=min(timeout, float(MAX_TIMEOUT_S)),
        cwd_relative="",
        allow_network=entry.network,
        env=extra_env,
        session_id=session_id,
        turn_id=turn_id,
    )
    sandbox = get_sandbox_service()
    result = await sandbox.run(request)
    combined = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
    truncated = len(combined) > RUN_OUTPUT_MAX_CHARS
    if truncated:
        combined = combined[:RUN_OUTPUT_MAX_CHARS] + "\n" + _render(language, "output_truncated")
    run = RunResult(
        ok=result.ok,
        output=combined,
        argv=argv,
        exit_code=result.exit_code,
        duration_s=result.duration_s,
        error=result.error or "",
    )
    run.output = _format_output(run, entry, language)
    return run
