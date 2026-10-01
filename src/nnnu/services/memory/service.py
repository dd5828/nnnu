"""记忆服务（§7.10）：编排器 L1Sink 的落地实现 + 整合单飞 + 阈值自动触发。

- **两条接缝**（runtime/orchestrator.py 的 L1Sink）：`begin_turn` 在能力开跑前
  落 user_message 行——write_memory 工具因此总能引用到一个「已经存在」的行；
  `record_turn` 在回合收尾把 bus.history 映射成其余事件。两处都吞异常：轨迹
  永远不许把回合拖挂（编排器侧还有第二道兜底）。
- **回合注册表** `turn_id → (surface, file, line)`：上限 500，超了丢最旧。
  工具/路由据此拿「本回合的引用锚点」；写失败的行不登记（不猜位置）。
- **单飞**：一把锁。手动撞锁返回 None（路由 409），自动撞锁静默跳过（下回合
  再试）。`busy` 还包含「已排队未开跑」的后台任务——后台发起必须先同步占位，
  否则连点两下「立即整合」会排出两轮。
- **自动触发**：每回合 +1；≥ `auto_threshold_turns` 且 `auto_enabled` 且未在跑
  → create_task 后台整合（任务挂 set 强引用防 GC）。不引 cron 做这件事：回合
  天然是计数点，cron 是用户任务调度器，语义对不上。
- **启动恢复**：上次进程死在 running 的 run → `recover_interrupted()` 记
  interrupted（shutdown 取消任务时 run_consolidation 的 finally 不会跑，账就
  停在 running，正是靠它收尾）。
"""

import asyncio
import logging
from pathlib import Path
from typing import Any

from nnnu.core.events import StreamEvent
from nnnu.core.ids import new_id
from nnnu.services.memory import trace
from nnnu.services.memory.consolidator.pipeline import Consolidator
from nnnu.services.memory.models import ConsolidationRun, MemoryConfig
from nnnu.services.memory.state import StateStore
from nnnu.services.memory.store import MemoryStore

logger = logging.getLogger(__name__)

TURN_REGISTRY_CAP = 500


class MemoryService:
    """记忆门面：L1 采集 + 整合入口；路由与工具层都从这里取。"""

    def __init__(self, data_root: Path) -> None:
        self.data_root = data_root
        self.store = MemoryStore(data_root)
        self.state = StateStore(data_root)
        self.writer = trace.TraceWriter(data_root)
        self._turns: dict[str, tuple[str, str, int]] = {}
        self._lock = asyncio.Lock()
        self._pending = False  # 已排队未开跑的后台整合（同步占位）
        self._tasks: set[asyncio.Task[Any]] = set()

    # ---- 设置 ----

    def config(self) -> MemoryConfig:
        """现读设置区（无缓存，改了就生效）；区还没落 SPECS 时用默认值。"""
        from nnnu.services.settings.service import get_settings_service

        try:
            values = get_settings_service().load_area("memory")
        except KeyError:  # 刀四才落 SPECS，之前的调用方拿默认值
            values = None
        return MemoryConfig.from_mapping(values)

    def language(self) -> str:
        """整合提示词语言跟外观设置的输出语言走（默认 zh）。"""
        from nnnu.services.settings.service import get_settings_service

        try:
            value = str(
                get_settings_service().load_area("appearance").get("output_language") or "zh"
            )
        except KeyError:
            value = "zh"
        return value if value in ("zh", "en") else "zh"

    # ---- L1Sink（编排器接缝）----

    async def begin_turn(
        self, turn_id: str, session_id: str | None, surface: str, user_message: str
    ) -> None:
        try:
            if not self.config().trace_enabled:
                return
            rows = [
                trace.user_message_row(
                    surface=surface, session_id=session_id, turn_id=turn_id, text=user_message
                )
            ]
            written = await self.writer.append(surface, rows)
            if written:  # 只有真写进去了才登记引用锚点
                self._remember(turn_id, surface, written[0])
        except Exception:
            logger.exception("L1 begin_turn 失败（turn=%s）", turn_id)

    async def record_turn(
        self, turn_id: str, session_id: str | None, events: list[StreamEvent]
    ) -> None:
        try:
            if not self.config().trace_enabled:
                return
            remembered = self._turns.get(turn_id)
            if remembered is None:
                return  # begin_turn 没落痕（关采/写失败）：不猜 surface，宁缺毋滥
            surface = remembered[0]
            rows = trace.rows_from_events(
                events, turn_id=turn_id, surface=surface, session_id=session_id
            )
            if rows:
                await self.writer.append(surface, rows)
            self._after_turn()
        except Exception:
            logger.exception("L1 record_turn 失败（turn=%s）", turn_id)

    # ---- 回合注册表 ----

    def surface_for_turn(self, turn_id: str) -> str | None:
        remembered = self._turns.get(turn_id)
        return remembered[0] if remembered else None

    def turn_ref(self, turn_id: str) -> tuple[str, str, int] | None:
        """本回合 user_message 行的 (surface, 月文件名, 行号)——工具写记忆的锚点。"""
        return self._turns.get(turn_id)

    def _remember(self, turn_id: str, surface: str, position: tuple[str, int]) -> None:
        filename, line = position
        self._turns[turn_id] = (surface, filename, line)
        while len(self._turns) > TURN_REGISTRY_CAP:
            self._turns.pop(next(iter(self._turns)))  # 丢最旧（dict 保持插入序）

    # ---- 整合 ----

    @property
    def busy(self) -> bool:
        """在跑或已排队（路由 409 判定 / 前端按钮禁用都看它）。"""
        return self._pending or self._lock.locked()

    async def consolidate(
        self,
        *,
        trigger: str = "manual",
        modes: list[str] | None = None,
        surfaces: list[str] | None = None,
        run: ConsolidationRun | None = None,
    ) -> ConsolidationRun | None:
        """跑一轮整合；单飞被占返回 None（手动 → 409，自动 → 跳过）。"""
        if self._lock.locked():  # 单线程事件循环：检查与 acquire 之间无 await，原子
            return None
        async with self._lock:
            consolidator = Consolidator(
                data_root=self.data_root,
                store=self.store,
                state=self.state,
                config=self.config(),
                lang=self.language(),
            )
            finished = await consolidator.run_consolidation(
                trigger=trigger, modes=modes, surfaces=surfaces, run=run
            )
            logger.info(
                "记忆整合结束 run=%s status=%s stats=%s",
                finished.id,
                finished.status,
                finished.stats,
            )
            return finished

    def start_consolidation(
        self,
        *,
        trigger: str = "manual",
        modes: list[str] | None = None,
        surfaces: list[str] | None = None,
    ) -> ConsolidationRun | None:
        """后台发起一轮整合：占到位返回 run（id 即刻可用，status=queued），忙返回 None。

        路由拿返回的 run 直接回 202——不必等整轮跑完（LLM 调用可能十几秒）。
        """
        if self.busy:
            return None
        run = ConsolidationRun(id=new_id("mrun"), trigger=trigger, status="queued")
        self._pending = True  # 同步占位：任务开跑前也挡住第二次点击
        task = asyncio.create_task(self._guarded(run, modes=modes, surfaces=surfaces))
        self._tasks.add(task)  # 强引用：不然任务可能被 GC 掉
        task.add_done_callback(self._tasks.discard)
        return run

    async def _guarded(
        self,
        run: ConsolidationRun,
        *,
        modes: list[str] | None = None,
        surfaces: list[str] | None = None,
    ) -> None:
        try:
            await self.consolidate(trigger=run.trigger, modes=modes, surfaces=surfaces, run=run)
        except Exception:  # 后台任务异常没人接：这里兜住并记日志
            logger.exception("后台整合失败（run=%s，下一轮重试）", run.id)
        finally:
            self._pending = False

    def _after_turn(self) -> None:
        config = self.config()
        if not config.auto_enabled:
            return
        turns = self.state.bump_turn()
        if turns < config.auto_threshold_turns:
            return
        self.start_consolidation(trigger="auto")  # 忙则静默跳过，下回合再试

    # ---- 生命周期 ----

    def recover_interrupted(self) -> bool:
        """启动恢复：上次进程死在 running 的 run 记 interrupted；有则返回 True。"""
        try:
            return self.state.mark_interrupted()
        except Exception:
            logger.exception("记忆运行状态恢复失败，继续启动")
            return False

    async def shutdown(self) -> None:
        """退出：取消在跑的后台整合（账停在 running，下次启动 recover 成 interrupted）。"""
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()


_service: MemoryService | None = None


def set_memory_service(service: MemoryService | None) -> None:
    """单例（照 learning/research 的做法）：工具层与路由共用一份。"""
    global _service
    _service = service


def get_memory_service() -> MemoryService | None:
    """未装配返回 None——工具据此回「记忆未启用」而不是抛异常（§7.10）。"""
    return _service
