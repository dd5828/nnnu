"""渲染服务（§7.8）：真机 Manim 与假渲染器的共同接口。

真渲染要 manim + ffmpeg（+ LaTeX，仅在用到 Tex 时；本仓库提示词明令禁止 Tex，
所以依赖检测不查 LaTeX）。用户拍板：本机与 CI 都没装、也不装——所以
`build_render_service()` 按 NNNU_MANIM_MOCK 环境变量选实现，测试与 E2E 全走
假渲染器；真实现（ManimRenderService）照常写完整，真机装好依赖即可用，
但不进自动化测试（P7 遗留清单）。

假渲染器留了一个注入口：代码里含 NNNU_MOCK_FAIL 时渲染失败——code_retry 的
修复路径靠它验收（§7.8 验收：故意制造错误的代码能被修复）。产物是占位字节，
不假装是能解码的视频；前端 ArtifactCard 对播放失败有兜底文案。
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import shutil
import sys
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from nnnu.services.render.artifacts import RenderStore
from nnnu.services.render.models import QUALITY_FLAGS, RenderResult
from nnnu.services.render.validation import extract_scene_class

logger = logging.getLogger(__name__)

# 单次渲染的墙钟上限：normal 档 30 秒动画在慢机上也就两三分钟，超了就当挂了杀掉，
# 不然一个死循环的动画能把整个回合钉住
RENDER_TIMEOUT_S = 300

# 日志回传上限（进 RenderResult.log，喂修复提示词与正文节选；完整 stdout 在
# 渲染目录里不落盘——诊断够用的量级就行）
MAX_LOG_CHARS = 4000

# on_log 回调：每来一行日志调一次（可以是同步或协程函数，内部统一 await 化）
LogSink = Callable[[str], Awaitable[None] | None]

# 假渲染器的占位产物（不是可解码视频；E2E 只断言卡片与下载链路）
MOCK_PLACEHOLDER_BYTES = b"NNNU-MOCK-VIDEO-PLACEHOLDER"
MOCK_FAIL_MARKER = "NNNU_MOCK_FAIL"


@dataclass(slots=True)
class DependencyReport:
    """依赖体检结果：missing 为空即可用，非空是缺的组件名（i18n 文案在提示词层拼）。"""

    missing: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing


def manim_dependency_report() -> DependencyReport:
    """真渲染的依赖体检：manim 模块 + ffmpeg 可执行文件。"""
    missing: list[str] = []
    if importlib.util.find_spec("manim") is None:
        missing.append("manim")
    if shutil.which("ffmpeg") is None:
        missing.append("ffmpeg")
    return DependencyReport(missing=missing)


class RenderService(ABC):
    """渲染器接口：可用性体检 + 一次渲染尝试。"""

    def __init__(self, store: RenderStore) -> None:
        self.store = store

    @abstractmethod
    def available(self) -> DependencyReport:
        """渲染依赖是否齐（能力层在花 LLM 调用之前先问这里）。"""

    @abstractmethod
    async def render(
        self,
        render_id: str,
        *,
        code: str,
        quality: str = "medium",
        attempts: int = 1,
        on_log: LogSink | None = None,
    ) -> RenderResult:
        """渲染一段 manim 代码；失败返回 ok=False + error + 日志尾巴。"""


class ManimRenderService(RenderService):
    """真机渲染：`python -m manim` 子进程，逐行回吐日志，超时杀进程。"""

    def __init__(self, store: RenderStore, *, timeout_s: int = RENDER_TIMEOUT_S) -> None:
        super().__init__(store)
        self.timeout_s = timeout_s

    def available(self) -> DependencyReport:
        return manim_dependency_report()

    async def render(
        self,
        render_id: str,
        *,
        code: str,
        quality: str = "medium",
        attempts: int = 1,
        on_log: LogSink | None = None,
    ) -> RenderResult:
        scene = extract_scene_class(code)
        if scene is None:
            return RenderResult(ok=False, error="代码里没有找到继承 Scene 的场景类")
        directory = self.store.ensure_dir(render_id)
        directory.mkdir(parents=True, exist_ok=True)
        script = directory / "scene.py"
        script.write_text(code, encoding="utf-8")
        media = directory / "media"
        command = [
            sys.executable,
            "-m",
            "manim",
            f"-q{QUALITY_FLAGS.get(quality, 'm')}",
            "--disable_caching",
            "--media_dir",
            str(media),
            str(script),
            scene,
        ]
        logger.info("manim 渲染开始 id=%s 场景=%s", render_id, scene)
        logs: list[str] = []
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(directory),
        )

        async def _pump() -> None:
            assert proc.stdout is not None
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    return
                line = raw.decode("utf-8", errors="replace").rstrip()
                logs.append(line)
                if on_log is not None:
                    outcome = on_log(line)
                    if asyncio.iscoroutine(outcome):
                        await outcome

        try:
            await asyncio.wait_for(_drain(proc, _pump()), timeout=self.timeout_s)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return RenderResult(ok=False, log=_tail(logs), error=f"渲染超时（>{self.timeout_s}s）")
        if proc.returncode != 0:
            return RenderResult(ok=False, log=_tail(logs), error=f"manim 退出码 {proc.returncode}")
        video = next(iter(sorted(media.glob("videos/**/*.mp4"))), None)
        if video is None:
            return RenderResult(ok=False, log=_tail(logs), error="渲染完成但没找到输出视频")
        meta = self.store.save(
            render_id, video, filename="animation.mp4", kind="video", attempts=attempts
        )
        return RenderResult(ok=True, meta=meta, log=_tail(logs))


class MockManimRenderService(RenderService):
    """假渲染器（NNNU_MANIM_MOCK=1）：不跑真进程，日志与控制流照真渲染走。"""

    def available(self) -> DependencyReport:
        return DependencyReport()  # 假装依赖齐了

    async def render(
        self,
        render_id: str,
        *,
        code: str,
        quality: str = "medium",
        attempts: int = 1,
        on_log: LogSink | None = None,
    ) -> RenderResult:
        directory = self.store.ensure_dir(render_id)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "scene.py").write_text(code, encoding="utf-8")
        scene = extract_scene_class(code)
        lines = [
            "[mock] 假渲染器启动（没有真跑 manim）",
            f"[mock] 质量档 {quality}，场景类 {scene or '（未找到）'}",
        ]
        if MOCK_FAIL_MARKER in code:
            lines.append(f"[mock] 检测到 {MOCK_FAIL_MARKER} 注入标记，本次渲染失败")
            for line in lines:
                await _fire(on_log, line)
            return RenderResult(
                ok=False, log=_tail(lines), error="假渲染器注入的失败（NNNU_MOCK_FAIL）"
            )
        lines.append("[mock] 写出占位产物 animation.mp4")
        for line in lines:
            await _fire(on_log, line)
        meta = self.store.save(
            render_id,
            MOCK_PLACEHOLDER_BYTES,
            filename="animation.mp4",
            kind="video",
            attempts=attempts,
        )
        return RenderResult(ok=True, meta=meta, log=_tail(lines))


async def _drain(proc: asyncio.subprocess.Process, pump: Awaitable[None]) -> None:
    """等「进程退出」与「日志抽干」两件事都完成（stdout 可能比进程晚关）。"""
    await asyncio.gather(proc.wait(), pump)


async def _fire(on_log: LogSink | None, line: str) -> None:
    if on_log is None:
        return
    outcome = on_log(line)
    if asyncio.iscoroutine(outcome):
        await outcome


def _tail(logs: list[str]) -> str:
    joined = "\n".join(logs)
    if len(joined) <= MAX_LOG_CHARS:
        return joined
    return f"…（前文略）\n{joined[-MAX_LOG_CHARS:]}"


def build_render_service(store: RenderStore) -> RenderService:
    """按环境变量选实现：NNNU_MANIM_MOCK 打开时用假渲染器（测试/E2E）。"""
    if os.environ.get("NNNU_MANIM_MOCK", "").strip().lower() in ("1", "true", "yes"):
        return MockManimRenderService(store)
    return ManimRenderService(store)


# ---- 单例（main.py lifespan 装配；能力层 get_render_service 取用，同 KB 服务） ----

_service: RenderService | None = None


def set_render_service(service: RenderService | None) -> None:
    global _service
    _service = service


def get_render_service() -> RenderService:
    if _service is None:
        raise RuntimeError("渲染服务尚未装配（main.py lifespan 未跑或已关闭）")
    return _service
