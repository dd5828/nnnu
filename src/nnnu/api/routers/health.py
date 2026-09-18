"""健康检查：版本、内存与运行时长；根路径就绪探测。"""

import platform
import sys
import time

from fastapi import APIRouter

from nnnu import __version__

router = APIRouter()

_START_TS = time.monotonic()


def _memory_info() -> dict | None:
    """进程 RSS 与宿主可用内存；psutil 为软依赖，缺失时返回 None。"""
    try:
        import psutil
    except ImportError:
        return None
    proc = psutil.Process()
    vm = psutil.virtual_memory()
    return {
        "rss_mb": round(proc.memory_info().rss / 1024 / 1024, 1),
        "percent": vm.percent,
        "available_mb": round(vm.available / 1024 / 1024, 1),
    }


@router.get("/api/v1/health")
async def health() -> dict:
    return {
        "status": "online",
        "version": __version__,
        "python": sys.version.split()[0],
        "platform": platform.system().lower(),
        "memory": _memory_info(),
        "uptime_s": round(time.monotonic() - _START_TS, 1),
    }


@router.get("/")
async def root() -> dict:
    """就绪探测：启动脚本与容器 healthcheck 打根路径。"""
    return {"message": "nnnu API", "version": __version__}
