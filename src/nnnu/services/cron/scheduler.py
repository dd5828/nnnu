"""cron 调度服务（§7.2 / §8.2）：cron 表达式解析 + 秒级精度循环 + SQLite 持久化。

adapted from DeepTutor (Apache-2.0) deeptutor/services/cron/service.py ——
保留其语义（JSON 持久化、单进程调度器、到期即执行并记簿），实现为：
- 仅 5 段 cron 表达式（§8.2 cron_jobs.schedule 即表达式）；
- 持久化走 cron_jobs 表（§8.2 原样）；
- 执行器由外部注入（lifespan 接 TurnRuntime.start_turn，到点以新回合执行）；
- 内存缓存任务集合（has_jobs 同步查询，供 context_gated 挂载判断）。
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

from nnnu.core.ids import new_id
from nnnu.services.sessions.db import Database

logger = logging.getLogger(__name__)

TICK_INTERVAL_S = 1.0
_NEXT_RUN_LOOKAHEAD_DAYS = 366  # compute_next_run 的护栏（防无解表达式死循环）

Executor = Callable[[Any], Awaitable[str]]


# ---- cron 表达式解析（5 段：min hour dom month dow；dow 0=周日） ----


class CronExprError(ValueError):
    """cron 表达式非法。"""


def _expand_field(field: str, low: int, high: int, name: str) -> set[int]:
    values: set[int] = set()
    for part in str(field).split(","):
        part = part.strip()
        if part in ("*", ""):
            values.update(range(low, high + 1))
        elif part.startswith("*/"):
            try:
                step = int(part[2:])
            except ValueError:
                raise CronExprError(f"{name} 段步长非法：{part}") from None
            if step <= 0:
                raise CronExprError(f"{name} 段步长必须为正：{part}")
            values.update(range(low, high + 1, step))
        elif "-" in part:
            try:
                start, _, end = part.partition("-")
                values.update(range(int(start), int(end) + 1))
            except ValueError:
                raise CronExprError(f"{name} 段区间非法：{part}") from None
        else:
            try:
                values.add(int(part))
            except ValueError:
                raise CronExprError(f"{name} 段数值非法：{part}") from None
    if not values or min(values) < low or max(values) > high:
        raise CronExprError(f"{name} 段超出范围 {low}-{high}：{field}")
    return values


def parse_cron(expr: str) -> dict[str, set[int]]:
    """5 段表达式 → 各段取值集合；格式错抛 CronExprError。"""
    parts = str(expr).strip().split()
    if len(parts) != 5:
        raise CronExprError(
            f"cron 表达式需要 5 段（分 时 日 月 周），实际 {len(parts)} 段：{expr!r}"
        )
    minute, hour, dom, month, dow = parts
    return {
        "minutes": _expand_field(minute, 0, 59, "分"),
        "hours": _expand_field(hour, 0, 23, "时"),
        "doms": _expand_field(dom, 1, 31, "日"),
        "months": _expand_field(month, 1, 12, "月"),
        "dows": _expand_field(dow, 0, 6, "周"),
    }


def cron_matches(expr: str, dt: datetime) -> bool:
    fields = parse_cron(expr)
    # 日/周任一受限时的标准 cron 语义：两者都受限取"或"，否则取受限的那个
    doms, dows = fields["doms"], fields["dows"]
    full_dom = doms == set(range(1, 32))
    full_dow = dows == set(range(0, 7))
    day_ok = dt.day in doms
    dow_ok = ((dt.weekday() + 1) % 7) in dows  # Monday=0 → cron 0=周日
    if not full_dom and not full_dow:
        day_match = day_ok or dow_ok
    elif not full_dom:
        day_match = day_ok
    elif not full_dow:
        day_match = dow_ok
    else:
        day_match = True
    return (
        dt.minute in fields["minutes"]
        and dt.hour in fields["hours"]
        and day_match
        and dt.month in fields["months"]
    )


def compute_next_run(expr: str, after: datetime) -> datetime:
    """严格晚于 after 的下一次命中（分钟粒度）；无解（一年内不命中）抛 CronExprError。"""
    fields = parse_cron(expr)
    full_dom = fields["doms"] == set(range(1, 32))
    full_dow = fields["dows"] == set(range(0, 7))
    dt = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(_NEXT_RUN_LOOKAHEAD_DAYS * 24 * 60):
        if dt.month not in fields["months"]:
            dt = dt.replace(day=1, hour=0, minute=0) + timedelta(days=32)
            dt = dt.replace(day=1)
            continue
        day_ok = dt.day in fields["doms"]
        dow_ok = ((dt.weekday() + 1) % 7) in fields["dows"]
        if not full_dom and not full_dow:
            day_match = day_ok or dow_ok
        elif not full_dom:
            day_match = day_ok
        elif not full_dow:
            day_match = dow_ok
        else:
            day_match = True
        if not day_match:
            dt = (dt + timedelta(days=1)).replace(hour=0, minute=0)
            continue
        if dt.hour not in fields["hours"]:
            dt = (dt + timedelta(hours=1)).replace(minute=0)
            continue
        if dt.minute not in fields["minutes"]:
            dt += timedelta(minutes=1)
            continue
        return dt
    raise CronExprError(f"cron 表达式一年内没有可执行的时间点：{expr!r}")


# ---- 服务 ----


@dataclass(slots=True)
class CronJob:
    id: str
    schedule: str
    prompt: str
    session_id: str | None
    enabled: bool
    last_run_at: float | None
    next_run_at: float
    created_at: float


class CronService:
    """进程内单例：任务 CRUD + 每秒检查到点任务交给执行器。"""

    def __init__(self, db: Database) -> None:
        self._db = db
        self._executor: Executor | None = None
        self._task: asyncio.Task | None = None
        self._jobs: dict[str, CronJob] = {}

    def set_executor(self, executor: Executor) -> None:
        self._executor = executor

    def has_jobs(self) -> bool:
        """同步查询（内存缓存）：供 context_gated 挂载判断。"""
        return any(job.enabled for job in self._jobs.values())

    async def load_jobs(self) -> None:
        rows = await self._db.fetch_all("SELECT * FROM cron_jobs")
        self._jobs = {
            str(row["id"]): CronJob(
                id=str(row["id"]),
                schedule=str(row["schedule"]),
                prompt=str(row["prompt"]),
                session_id=row["session_id"],
                enabled=bool(row["enabled"]),
                last_run_at=row["last_run_at"],
                next_run_at=float(row["next_run_at"]),
                created_at=float(row["created_at"]),
            )
            for row in rows
        }

    async def create(self, schedule: str, prompt: str, session_id: str | None = None) -> CronJob:
        parse_cron(schedule)  # 校验先行
        if not prompt.strip():
            raise CronExprError("任务内容（prompt）不能为空")
        now = time.time()
        next_run = compute_next_run(schedule, datetime.now()).timestamp()
        job = CronJob(
            id=new_id("cron"),
            schedule=schedule,
            prompt=prompt.strip(),
            session_id=session_id or None,
            enabled=True,
            last_run_at=None,
            next_run_at=next_run,
            created_at=now,
        )
        await self._db.execute(
            "INSERT INTO cron_jobs (id, schedule, prompt, session_id, enabled, "
            "last_run_at, next_run_at, created_at) VALUES (?, ?, ?, ?, 1, NULL, ?, ?)",
            (job.id, job.schedule, job.prompt, job.session_id, job.next_run_at, job.created_at),
        )
        self._jobs[job.id] = job
        logger.info("创建定时任务 %s：%s → %s", job.id, job.schedule, job.prompt[:40])
        return job

    async def delete(self, job_id: str) -> bool:
        if job_id not in self._jobs:
            return False
        await self._db.execute("DELETE FROM cron_jobs WHERE id = ?", (job_id,))
        self._jobs.pop(job_id, None)
        logger.info("删除定时任务 %s", job_id)
        return True

    async def list_jobs(self) -> list[CronJob]:
        return sorted(self._jobs.values(), key=lambda job: job.created_at)

    async def _tick(self, now: float | None = None) -> int:
        """检查到点任务并交给执行器（now 可注入，测试用）。返回本 tick 触发数。"""
        now = time.time() if now is None else now
        due = [job for job in self._jobs.values() if job.enabled and job.next_run_at <= now]
        for job in due:
            if self._executor is None:
                logger.warning("定时任务 %s 到期但执行器未装配，跳过", job.id)
                continue
            try:
                status = await self._executor(job)
            except Exception as exc:  # 执行器异常不拖垮调度循环
                logger.exception("定时任务 %s 执行异常", job.id)
                status = f"error: {exc}"
            job.last_run_at = now
            job.next_run_at = compute_next_run(
                job.schedule, datetime.fromtimestamp(now)
            ).timestamp()
            await self._db.execute(
                "UPDATE cron_jobs SET last_run_at = ?, next_run_at = ? WHERE id = ?",
                (job.last_run_at, job.next_run_at, job.id),
            )
            logger.info("定时任务 %s 执行：%s", job.id, status)
        return len(due)

    async def start(self) -> None:
        """启动调度循环（lifespan 调用；已启动则忽略）。"""
        if self._task is not None:
            return
        await self.load_jobs()
        self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while True:
            try:
                await self._tick()
            except Exception:
                logger.exception("cron 调度 tick 异常")
            await asyncio.sleep(TICK_INTERVAL_S)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None


_cron_service: CronService | None = None


def get_cron_service() -> CronService:
    if _cron_service is None:
        raise RuntimeError("cron 服务未装配（lifespan 未调用 set_cron_service）")
    return _cron_service


def set_cron_service(service: CronService) -> None:
    global _cron_service
    _cron_service = service


def reset_cron_service() -> None:
    """测试用：解除装配。"""
    global _cron_service
    _cron_service = None
