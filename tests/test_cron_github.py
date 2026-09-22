"""cron 调度与 github 工具（§7.2 / §12.1 fake clock）。"""

from datetime import datetime

import pytest

import nnnu.tools.builtin.github_query as github_module
from nnnu.core.tool_protocol import ToolContext
from nnnu.services.cron.scheduler import (
    CronExprError,
    CronService,
    compute_next_run,
    cron_matches,
    parse_cron,
    reset_cron_service,
    set_cron_service,
)
from nnnu.services.sessions.db import Database
from nnnu.services.sessions.schema import db_path, migrate
from nnnu.tools.builtin.cron_tool import CronTool
from nnnu.tools.builtin.github_query import GithubQueryTool
from nnnu.tools.builtin.github_query import _github_get as real_github_get


@pytest.fixture
async def cron_db(tmp_home):
    migrate(tmp_home / "data")
    db = Database(db_path(tmp_home / "data"))
    await db.connect()
    yield db
    await db.close()


@pytest.fixture
async def cron_service(cron_db):
    service = CronService(cron_db)
    set_cron_service(service)
    await service.load_jobs()
    yield service
    await service.stop()
    reset_cron_service()


def _ctx(**args) -> ToolContext:
    return ToolContext(turn_id="turn-test", session_id="sess-test", args=dict(args))


# ---- cron 表达式 ----


def test_parse_cron_valid():
    fields = parse_cron("*/15 8-10 1,15 * 0")
    assert fields["minutes"] == {0, 15, 30, 45}
    assert fields["hours"] == {8, 9, 10}
    assert fields["doms"] == {1, 15}
    assert fields["dows"] == {0}


def test_parse_cron_invalid():
    with pytest.raises(CronExprError):
        parse_cron("0 8 * *")  # 少一段
    with pytest.raises(CronExprError):
        parse_cron("60 8 * * *")  # 分钟越界
    with pytest.raises(CronExprError):
        parse_cron("a b c d e")  # 非数字


def test_cron_matches_basic():
    assert cron_matches("0 8 * * *", datetime(2026, 9, 22, 8, 0))
    assert not cron_matches("0 8 * * *", datetime(2026, 9, 22, 8, 1))
    assert cron_matches("*/15 * * * *", datetime(2026, 9, 22, 9, 45))
    assert not cron_matches("*/15 * * * *", datetime(2026, 9, 22, 9, 46))


def test_cron_matches_dow():
    # 2026-09-21 是周一（weekday=0 → cron dow=1）
    assert cron_matches("0 9 * * 1", datetime(2026, 9, 21, 9, 0))
    assert not cron_matches("0 9 * * 1", datetime(2026, 9, 22, 9, 0))


def test_compute_next_run():
    next_run = compute_next_run("0 8 * * *", datetime(2026, 9, 22, 7, 30))
    assert next_run == datetime(2026, 9, 22, 8, 0)
    # 已过当天 8 点 → 次日
    next_run = compute_next_run("0 8 * * *", datetime(2026, 9, 22, 8, 30))
    assert next_run == datetime(2026, 9, 23, 8, 0)
    # 每 15 分钟
    assert compute_next_run("*/15 * * * *", datetime(2026, 9, 22, 9, 46)) == datetime(
        2026, 9, 22, 10, 0
    )


# ---- 服务与工具 ----


async def test_cron_create_list_delete(cron_service):
    job = await cron_service.create("0 8 * * *", "给我出一道题", session_id="sess-test")
    assert job.schedule == "0 8 * * *"
    assert cron_service.has_jobs() is True
    jobs = await cron_service.list_jobs()
    assert len(jobs) == 1
    assert await cron_service.delete(job.id) is True
    assert await cron_service.delete(job.id) is False
    assert cron_service.has_jobs() is False


async def test_cron_create_invalid_expr(cron_service):
    with pytest.raises(CronExprError):
        await cron_service.create("99 99 * * *", "bad")


async def test_cron_tick_fires_due_jobs(cron_service):
    executed: list[str] = []

    async def fake_executor(job):
        executed.append(job.prompt)
        return "ok"

    cron_service.set_executor(fake_executor)
    job = await cron_service.create("0 8 * * *", "到点执行")
    # 直接注入"已到点"的 next_run_at，用假时钟 tick
    await cron_service._db.execute(
        "UPDATE cron_jobs SET next_run_at = ? WHERE id = ?", (1.0, job.id)
    )
    job.next_run_at = 1.0
    fired = await cron_service._tick(now=100.0)
    assert fired == 1
    assert executed == ["到点执行"]
    # 下次执行时间被推进到未来，不再重复触发
    assert job.next_run_at > 100.0
    assert await cron_service._tick(now=100.0) == 0


async def test_cron_tool_crud(cron_service):
    tool = CronTool()
    created = await tool.run(_ctx(action="create", schedule="0 8 * * *", prompt="每日一题"))
    assert created.ok is True
    assert "已创建定时任务" in created.output
    listing = await tool.run(_ctx(action="list"))
    assert "每日一题" in listing.output
    job_id = created.detail["job_id"]
    deleted = await tool.run(_ctx(action="delete", job_id=job_id))
    assert deleted.ok is True
    missing = await tool.run(_ctx(action="delete", job_id=job_id))
    assert missing.ok is False


async def test_cron_tool_invalid_schedule(cron_service):
    tool = CronTool()
    result = await tool.run(_ctx(action="create", schedule="not-a-cron", prompt="x"))
    assert result.ok is False


# ---- github 工具 ----


async def test_github_readme(monkeypatch):
    async def fake_get(path, *, raw=False, token=None):
        assert path == "/repos/owner/repo/readme"
        return "# 项目说明\n内容"

    monkeypatch.setattr(github_module, "_github_get", fake_get)
    result = await GithubQueryTool().run(_ctx(repo="owner/repo", what="readme"))
    assert result.ok is True
    assert "项目说明" in result.output


async def test_github_tree(monkeypatch):
    async def fake_get(path, *, raw=False, token=None):
        return {"tree": [{"path": "src", "type": "tree"}, {"path": "README.md", "type": "blob"}]}

    monkeypatch.setattr(github_module, "_github_get", fake_get)
    result = await GithubQueryTool().run(_ctx(repo="owner/repo", what="tree"))
    assert result.ok is True
    assert "src/" in result.output
    assert "README.md" in result.output


async def test_github_file_requires_path():
    result = await GithubQueryTool().run(_ctx(repo="owner/repo", what="file"))
    assert result.ok is False
    assert "path" in result.output


async def test_github_bad_repo_format():
    result = await GithubQueryTool().run(_ctx(repo="no-slash", what="readme"))
    assert result.ok is False
    assert "owner/name" in result.output


async def test_github_api_error(monkeypatch):
    async def fake_get(path, *, raw=False, token=None):
        raise RuntimeError("GitHub API 返回 HTTP 404")

    monkeypatch.setattr(github_module, "_github_get", fake_get)
    result = await GithubQueryTool().run(_ctx(repo="owner/missing", what="readme"))
    assert result.ok is False
    assert "404" in result.output
