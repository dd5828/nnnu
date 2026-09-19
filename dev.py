"""开发期一键启动：后端（watchfiles 热重启）+ 前端 next dev，经 Next 代理联调。

用法（仓库根目录）：
    python dev.py

- 后端 127.0.0.1:8001：uvicorn 单进程运行（不带内置 reload），由本脚本
  watchfiles 监视 src/ 与 prompts/，变更时重启后端进程。
  不用 uvicorn --reload 的原因：其 Windows 实现走 multiprocessing spawn，
  在本机 venv 重定向器下启动极慢、子进程输出丢失、终止时留孤儿进程。
- 前端 http://localhost:3782（next dev 自带热更新），浏览器只访问前端端口，
  /api/* 与 /ws/* 由 web/proxy.ts 反代到后端 loopback。
- 前端退出则整体退出；后端退出则等待下次文件变更重启（等价 --reload 语义）；
  Ctrl+C 同时停掉两端。
"""

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


def _spawn_frontend() -> subprocess.Popen:
    npm = shutil.which("npm")
    if not npm:
        sys.exit("找不到 npm，请先安装 Node.js 22+（web/package.json engines）")
    return subprocess.Popen([npm, "run", "dev"], cwd=ROOT / "web")


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


def main() -> None:
    print("启动 nnnu 开发环境：")
    print("  后端  http://127.0.0.1:8001  (uvicorn + watchfiles 热重启)")
    print("  前端  http://localhost:3782   (next dev，/api/* 反代后端)")
    print("  Ctrl+C 同时停止两端\n")

    backend = BackendSupervisor()
    backend.start()
    frontend = _spawn_frontend()

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
