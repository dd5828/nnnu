"""后端启动入口：编程式 uvicorn.run（Windows Proactor + 热重载开关）。

用编程式启动而非 uvicorn CLI：保证 Windows 事件循环策略在 uvicorn 之前
设置，并控制"先 bootstrap 再从 network 设置读端口"的时序。
"""

import asyncio
import os
import sys

if sys.platform == "win32":
    # 必须在任何 uvicorn / 子进程使用之前设置（P7 沙箱子进程依赖）
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())


def _dev_reload() -> bool:
    return os.environ.get("NNNU_DEV_RELOAD", "").strip().lower() in {"1", "true", "yes", "on"}


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
        reload=_dev_reload(),
        reload_excludes=[
            "data/**",
            ".venv/**",
            "venv/**",
            "web/**",
            ".git/**",
            "**/__pycache__/**",
        ],
        access_log=False,
    )


if __name__ == "__main__":
    main()
