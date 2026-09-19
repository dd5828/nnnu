"""后端启动入口：编程式 uvicorn.run（Windows Proactor + 设置驱动的端口）。

用编程式启动而非 uvicorn CLI：保证 Windows 事件循环策略在 uvicorn 之前
设置，并控制"先 bootstrap 再从 network 设置读端口"的时序。

开发期热重启由仓库根 dev.py 的 watchfiles 循环负责（本入口始终单进程、
不带 uvicorn 内置 reload——其 Windows 实现经 multiprocessing spawn，
在本机 venv 重定向器下启动慢、输出丢失、终止留孤儿进程）。
"""

import asyncio
import sys

if sys.platform == "win32":
    # 必须在任何 uvicorn / 子进程使用之前设置（P7 沙箱子进程依赖）
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())


def _backend_port() -> int:
    """从 network 设置读取端口；设置服务不可用时回退默认 8001。"""
    try:
        from nnnu.services.settings.service import get_settings_service

        values = get_settings_service().load_area("network")
        return int(values.get("backend_port", 8001))
    except Exception:
        return 8001


def main() -> None:
    import uvicorn

    from nnnu.runtime import bootstrap

    bootstrap.ensure_bootstrap()
    bootstrap.configure_logging()
    uvicorn.run(
        "nnnu.api.main:app",
        host="0.0.0.0",
        port=_backend_port(),
        access_log=False,
    )


if __name__ == "__main__":
    main()
