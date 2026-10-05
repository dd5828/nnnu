"""开发期一键启动：后端（watchfiles 热重启）+ 前端 next dev，经 Next 代理联调。

用法（仓库根目录）：
    python dev.py

- 用哪个解释器跑都行：不是 .venv 的会自动换成 .venv 的再起（外部解释器
  import 到的 nnnu 可能是别的安装，端口设置会读错项目，见技术问题记录 #41）。
- 两个端口都读设置区 network（后端 `backend_port` 默认 8001 / 前端 `frontend_port`
  默认 3782，设置页改完重启本脚本即生效）：
  后端 uvicorn 单进程运行（不带内置 reload），由本脚本 watchfiles 监视 src/ 与
  prompts/，变更时重启后端进程。不用 uvicorn --reload 的原因：其 Windows 实现走
  multiprocessing spawn，在本机 venv 重定向器下启动极慢、子进程输出丢失、终止时留孤儿进程。
  前端 next dev 自带热更新，启动时把 `NNNU_API_BASE_URL` 指到后端实际端口（proxy.ts
  据此反代，不设的话默认 8001——设置页改过后端端口就必须由这里喂齐）。
- 浏览器只访问前端端口，/api/* 与 /ws/* 由 web/proxy.ts 反代到后端 loopback。
- 前端退出则整体退出；后端退出则等待下次文件变更重启（等价 --reload 语义）；
  Ctrl+C 同时停掉两端。
"""

import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

# 重定向输出（如管道到日志文件）时固定 UTF-8，避免 Windows 下中文乱码
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent

WATCH_PATHS = ("src", "prompts")  # 后端热重启监视目录（相对仓库根）
RESTART_DEBOUNCE_S = 0.8


def _venv_python() -> str:
    """优先用项目 .venv 的解释器，缺失时回退当前解释器。"""
    for candidate in (".venv/Scripts/python.exe", ".venv/bin/python"):
        path = ROOT / candidate
        if path.is_file():
            return str(path)
    return sys.executable


class BackendSupervisor:
    """后端进程监督：启动、文件变更重启、退出回收（单进程，无子进程树）。"""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        """启动后端；失败（如端口占用）只报告，等下次文件变更重试。"""
        with self._lock:
            if self.proc is not None and self.proc.poll() is None:
                return
            self.proc = subprocess.Popen([_venv_python(), "-m", "nnnu.api.run_server"], cwd=ROOT)

    def restart(self) -> None:
        with self._lock:
            if self.proc is not None and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            print("[dev] 检测到源码变更，重启后端…", flush=True)
            self.proc = subprocess.Popen([_venv_python(), "-m", "nnnu.api.run_server"], cwd=ROOT)

    def stop(self) -> None:
        with self._lock:
            if self.proc is not None and self.proc.poll() is None:
                self.proc.terminate()
            if self.proc is not None:
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()

    def take_exit_code(self) -> int | None:
        """后端已自行退出时取出退出码并清空状态（交给 watch 循环重启）。"""
        with self._lock:
            if self.proc is None or self.proc.poll() is None:
                return None
            code = self.proc.returncode
            self.proc = None
            return code


def _ports() -> tuple[int, int]:
    """读设置区 network 的用户端口（与后端 run_server 同口径）；读不到时回退默认。"""
    try:
        from nnnu.services.settings.service import get_settings_service

        values = get_settings_service().load_area("network")
        return int(values.get("backend_port", 8001)), int(values.get("frontend_port", 3782))
    except Exception:
        return 8001, 3782


def _spawn_frontend(frontend_port: int, backend_port: int) -> subprocess.Popen:
    """照 playwright.config.ts 的起法：npx next dev -p 端口 + NNNU_API_BASE_URL 环境变量。"""
    npx = shutil.which("npx")
    if not npx:
        sys.exit("找不到 npx，请先安装 Node.js 22+（web/package.json engines）")
    env = {**os.environ, "NNNU_API_BASE_URL": f"http://127.0.0.1:{backend_port}"}
    return subprocess.Popen(
        [npx, "next", "dev", "-p", str(frontend_port)], cwd=ROOT / "web", env=env
    )


def _watch_loop(backend: BackendSupervisor, stop_event: threading.Event) -> None:
    """watchfiles 变更循环：批量变更去抖后重启后端。"""
    from watchfiles import watch

    try:
        paths = [str(ROOT / name) for name in WATCH_PATHS]
        for _changes in watch(*paths, stop_event=stop_event, debounce=1600):
            if stop_event.is_set():
                return
            backend.restart()
            time.sleep(RESTART_DEBOUNCE_S)
    except RuntimeError:
        # stop_event 置位后 watch 抛 RuntimeError 正常收尾
        return


def _ensure_venv_interpreter() -> None:
    """不是 .venv 的解释器就用它重新起一遍。

    外部解释器（如 E:\\python）装着的 nnnu 可能指向别的项目，`_ports()` 会读到
    别的项目设置：后端子进程用 .venv 起（读本项目设置），前端却拿到别的端口——
    早前 `python dev.py` 就这样把 NNNU_API_BASE_URL 指到了 8001（见技术问题记录 #41）。
    """
    target = _venv_python()
    if os.path.normcase(os.path.abspath(sys.executable)) == os.path.normcase(
        os.path.abspath(target)
    ):
        return
    print(f"[dev] 解释器 {sys.executable} 不是项目 .venv，换 {target} 重新启动…", flush=True)
    sys.exit(subprocess.call([target, str(Path(__file__).resolve()), *sys.argv[1:]]))


def main() -> None:
    _ensure_venv_interpreter()
    backend_port, frontend_port = _ports()
    print("启动 nnnu 开发环境：")
    print(f"  后端  http://127.0.0.1:{backend_port}  (uvicorn + watchfiles 热重启)")
    print(f"  前端  http://localhost:{frontend_port}   (next dev，/api/* 反代后端)")
    print("  Ctrl+C 同时停止两端\n")

    backend = BackendSupervisor()
    backend.start()
    frontend = _spawn_frontend(frontend_port, backend_port)

    stop_event = threading.Event()
    watcher = threading.Thread(target=_watch_loop, args=(backend, stop_event), daemon=True)
    watcher.start()

    try:
        while True:
            if frontend.poll() is not None:
                print(f"frontend 已退出 (code={frontend.returncode})，停止后端")
                stop_event.set()
                backend.stop()
                sys.exit(frontend.returncode)
            code = backend.take_exit_code()
            if code is not None:
                print(f"backend 已退出 (code={code})，等待源码变更后重启…", flush=True)
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n收到中断，停止两端…")
        stop_event.set()
        backend.stop()
        _stop_frontend(frontend)


def _stop_frontend(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


if __name__ == "__main__":
    main()
