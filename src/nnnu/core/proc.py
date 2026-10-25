"""本机进程的通用操作（§11.2/§7.12）：目前只有一件事——把一棵进程树杀掉。

从沙箱 `services/sandbox/service.py` 提上来的（行为一字未改）。提的原因很实际：
子代理跑的是外部 CLI，它自己会再起子进程（node、python、工具子命令），超时或用户点
「停」时只杀父进程会留下一串孤儿继续跑；这套平台分支全仓只需要一份，放在中立模块里
两边共用，比从别人家的私有名 import 干净。

调用方注意两件事：

- 想让 `taskkill /T`（POSIX 的进程组）真的覆盖到子孙，起进程时要
  `start_new_session=(os.name != "nt")`——不然子进程不在自己的进程组里，POSIX 那支
  打不到它；
- 这个函数**不看结果也不吞异常**地尽力杀，返回后进程可能还没退出，要等退出得自己
  `await process.wait()`。
"""

from __future__ import annotations

import asyncio
import os


async def kill_tree(process: asyncio.subprocess.Process) -> None:
    """杀整棵进程树：Windows 走 taskkill /T，POSIX 走进程组。

    Windows 那支**要等 taskkill 退出再返回**（`killer.wait()`）：`/T` 是 taskkill 现查
    父进程链算出来的，它还没查完、调用方的单进程兜底 `process.kill()` 就把父进程打死的
    话，孙子进程当场变孤儿——taskkill 追到一半没了根，也就永远找不到它们（实测：父进程
    当场死、孙子继续写心跳；不等它就会时好时坏）。等它退掉，这棵树的死活已经定局，后面
    的单进程 kill 再怎么打也是打在尸体上。真卡住由调用方的超时收口（如 runner 的 3 秒）。
    """
    try:
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/F",
                "/T",
                "/PID",
                str(process.pid),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()  # 理由见 docstring；顺带把这根管道收干净
        else:
            killpg = getattr(os, "killpg", None)  # POSIX 专属，Windows 类型桩无此属性
            getpgid = getattr(os, "getpgid", None)
            if killpg is not None and getpgid is not None:
                killpg(getpgid(process.pid), 9)
    except (OSError, ProcessLookupError):
        try:
            process.kill()
        except ProcessLookupError:
            pass
