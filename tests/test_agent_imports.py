"""导入第三方 CLI 历史会话（§7.12 刀三）：解析、幂等、白名单、路径收口与 REST。

夹具照着 Claude Code 真身 jsonl 的形状造（元信息散在不同事件的行上、`message.content`
是块数组），只保留这个功能真正读到的那几个字段——多造无用字段反而会把「到底依赖
什么」这件事盖住。**一律不碰真的 `~/.claude/projects`**：扫描根全靠白名单夹具钉住。
"""

import json
import os
import time

import pytest

from nnnu.services.agents.imports import (
    SEARCH_TEXT_MAX_CHARS,
    AgentImportInvalid,
    AgentImportService,
    AgentSessionNotFound,
    transcript_path,
)
from nnnu.services.sessions.db import Database
from nnnu.services.sessions.schema import db_path, migrate

SESSION_A = "11111111-2222-3333-4444-555555555555"
SESSION_B = "99999999-8888-7777-6666-555555555555"


# ---- 夹具 ----


def _user(text: str, *, ts: str = "2026-10-01T10:00:00.000Z", **extra) -> dict:
    return {
        "type": "user",
        "message": {"role": "user", "content": text},
        "timestamp": ts,
        "cwd": "D:\\github\\666\\demo",
        **extra,
    }


def _assistant(blocks: list[dict], *, ts: str = "2026-10-01T10:00:05.000Z", **extra) -> dict:
    return {
        "type": "assistant",
        "message": {"id": "msg_1", "role": "assistant", "content": blocks},
        "timestamp": ts,
        **extra,
    }


def _write(path, lines: list) -> None:
    """写一份「历史 jsonl」；字符串行原样写（用来塞坏行）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(
        line if isinstance(line, str) else json.dumps(line, ensure_ascii=False) for line in lines
    )
    path.write_text(body + "\n", encoding="utf-8")


@pytest.fixture
def projects(tmp_path, monkeypatch):
    """假的项目历史根 + 白名单指向它（默认根是真 home，测试里不能碰）。"""
    root = tmp_path / "projects"
    (root / "proj-alpha").mkdir(parents=True)
    monkeypatch.setenv("NNNU_AGENTS_IMPORT_ROOTS", str(root))
    return root


@pytest.fixture
async def imports(tmp_home):
    """真库（tmp 数据目录）+ 迁移到位，直接构造服务。"""
    data_root = tmp_home / "data"
    migrate(data_root)
    db = Database(db_path(data_root))
    await db.connect()
    try:
        yield AgentImportService(db)
    finally:
        await db.close()


def _one_session(root, *, session_id: str = SESSION_A, title: str = "把快排改成三路切分"):
    path = root / "proj-alpha" / f"{session_id}.jsonl"
    _write(
        path,
        [
            _user("帮我看看 qsort.py"),
            _assistant(
                [
                    {"type": "thinking", "thinking": "先读文件"},
                    {"type": "text", "text": "我看了一下这个文件。"},
                    {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file": "qsort.py"}},
                ]
            ),
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "t1", "content": "def qsort..."}
                    ],
                },
                "timestamp": "2026-10-01T10:00:06.000Z",
            },
            _assistant([{"type": "text", "text": "改成三路切分后重复元素不再退化。"}]),
            {"type": "summary", "summary": "改了快排", "aiTitle": title},
        ],
    )
    return path


# ---- 解析 ----


def test_parse_extracts_text_and_skips_noise(tmp_path):
    from nnnu.services.agents.imports import parse_transcript

    path = tmp_path / f"{SESSION_A}.jsonl"
    _write(
        path,
        [
            _user("帮我看看 qsort.py"),
            "这一行不是 JSON",
            _assistant(
                [
                    {"type": "text", "text": "我看了一下。"},
                    {"type": "tool_use", "name": "Bash"},
                    {"type": "thinking", "thinking": "别收我"},
                ]
            ),
            _assistant([{"type": "text", "text": "这是子代理内部往返"}], isSidechain=True),
            _user("这是系统注入", isMeta=True),
            _assistant([{"type": "text", "text": "改成三路切分更快。"}]),
        ],
    )
    parsed = parse_transcript(path)
    assert parsed.source_id == SESSION_A
    assert parsed.cwd == "D:\\github\\666\\demo"
    assert parsed.bad_lines == 1
    assert [message.role for message in parsed.messages] == ["user", "assistant", "assistant"]
    assert parsed.messages[0].text == "帮我看看 qsort.py"
    # 工具调用压成一行标记；thinking 不收
    assert parsed.messages[1].text.endswith("（调用了工具：Bash）")
    assert "别收我" not in json.dumps([m.to_line() for m in parsed.messages], ensure_ascii=False)
    assert all("子代理内部" not in m.text and "系统注入" not in m.text for m in parsed.messages)
    assert parsed.started_at is not None and parsed.ended_at >= parsed.started_at


def test_parse_survives_a_file_of_garbage(tmp_path):
    from nnnu.services.agents.imports import parse_transcript

    path = tmp_path / "broken.jsonl"
    _write(path, ["}{", "[]", "null", "看起来像 JSON 的普通文本"])
    parsed = parse_transcript(path)
    assert parsed.messages == []
    assert parsed.bad_lines == 4  # 坏行只计数，不抛


def test_parse_falls_back_to_the_first_user_message_for_title(tmp_path):
    from nnnu.services.agents.imports import MAX_TITLE_CHARS, parse_transcript

    path = tmp_path / f"{SESSION_A}.jsonl"
    long_question = "为什么" + "这么长" * 60
    _write(path, [_user(long_question), _assistant([{"type": "text", "text": "答"}])])
    parsed = parse_transcript(path)
    assert parsed.title == long_question[:MAX_TITLE_CHARS]  # 没有 aiTitle 就用首条用户消息


def test_parse_keeps_later_title_and_reads_plain_string_content(tmp_path):
    from nnnu.services.agents.imports import parse_transcript

    path = tmp_path / f"{SESSION_A}.jsonl"
    _write(
        path,
        [
            _user("最初的标题"),
            {"type": "summary", "aiTitle": "旧标题"},
            _assistant([{"type": "text", "text": "答"}]),
            {"type": "summary", "aiTitle": "最终标题"},
        ],
    )
    parsed = parse_transcript(path)
    assert parsed.title == "最终标题"  # 后写的更准，覆盖


# ---- 扫描 / 导入 ----


async def test_scan_lists_candidates_with_import_state(imports, projects):
    _one_session(projects)
    first = await imports.scan()
    assert first["root"] == str(projects)
    assert len(first["sessions"]) == 1
    entry = first["sessions"][0]
    assert entry["title"] == "把快排改成三路切分"
    assert entry["project"] == "proj-alpha"
    assert entry["message_count"] == 3
    assert entry["imported"] is False and entry["changed"] is False

    await imports.import_sessions()
    second = await imports.scan()
    assert second["sessions"][0]["imported"] is True
    assert second["sessions"][0]["changed"] is False


async def test_import_writes_transcript_and_is_idempotent(imports, projects):
    source = _one_session(projects)
    result = await imports.import_sessions()
    assert len(result["imported"]) == 1
    session_id = result["imported"][0]["id"]
    assert session_id.startswith("imp-")

    path = transcript_path(session_id)
    assert path.is_file()
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [line["role"] for line in lines] == ["user", "assistant", "assistant"]

    # 再导一次：同 id、不重写、算「已是最新」
    again = await imports.import_sessions()
    assert again["imported"] == [] and again["unchanged"] == [session_id]
    listed = await imports.list_sessions()
    assert listed["total"] == 1

    # 来源那边又聊了两句 → digest 变了，整条覆盖重导
    _write(
        source,
        [
            _user("帮我看看 qsort.py"),
            _assistant([{"type": "text", "text": "改成三路切分后重复元素不再退化。"}]),
            _user("再帮我加个测试"),
        ],
    )
    changed = await imports.import_sessions()
    assert changed["imported"][0]["id"] == session_id  # 确定性 id：不新增条目
    assert changed["imported"][0]["changed"] is True
    assert (await imports.list_sessions())["total"] == 1
    detail = await imports.read_transcript(session_id)
    assert detail["total"] == 3
    assert detail["messages"][-1]["text"] == "再帮我加个测试"


async def test_import_only_selected_ids_and_reports_missing(imports, projects):
    _one_session(projects, session_id=SESSION_A)
    _one_session(projects, session_id=SESSION_B, title="另一段历史")
    scanned = await imports.scan()
    wanted = next(entry for entry in scanned["sessions"] if entry["source_id"] == SESSION_B)

    result = await imports.import_sessions(ids=[wanted["id"], "imp-deadbeef"])
    assert [entry["id"] for entry in result["imported"]] == [wanted["id"]]
    assert result["errors"] == [
        {"id": "imp-deadbeef", "reason": "这一轮没扫到（历史被删了？或不在所选天数内）"}
    ]
    listed = await imports.list_sessions()
    assert listed["total"] == 1 and listed["sessions"][0]["source_id"] == SESSION_B


async def test_days_filter_uses_file_mtime(imports, projects):
    fresh = _one_session(projects, session_id=SESSION_A)
    old = _one_session(projects, session_id=SESSION_B, title="很久以前")
    stale = time.time() - 40 * 86400
    os.utime(old, (stale, stale))

    recent = await imports.scan(days=3)
    assert [entry["path"] for entry in recent["sessions"]] == [str(fresh)]

    everything = await imports.scan()
    assert len(everything["sessions"]) == 2


async def test_skipped_files_are_reported_not_silently_dropped(imports, projects):
    _one_session(projects)
    empty = projects / "proj-alpha" / "empty.jsonl"
    _write(empty, [{"type": "summary", "aiTitle": "只有标题"}] * 3)
    result = await imports.scan()
    assert any(item["path"] == str(empty) for item in result["skipped"])
    assert len(result["sessions"]) == 1  # 没正文的那份不进候选


# ---- 白名单与路径收口 ----


async def test_root_outside_the_whitelist_is_rejected(imports, projects, tmp_path):
    secret = tmp_path / "elsewhere"
    secret.mkdir()
    with pytest.raises(AgentImportInvalid, match="只允许扫描这些目录"):
        await imports.scan(root=str(secret))
    with pytest.raises(AgentImportInvalid):
        await imports.import_sessions(root=str(tmp_path))


async def test_root_inside_the_whitelist_is_allowed(imports, projects):
    _one_session(projects)
    result = await imports.scan(root=str(projects / "proj-alpha"))
    assert result["root"] == str(projects / "proj-alpha")
    assert len(result["sessions"]) == 1


async def test_a_missing_root_is_reported_not_raised(imports, projects):
    # 根不存在（历史目录还没建出来）：如实说，不抛
    result = await imports.scan(root=str(projects / "没有这个目录"))
    assert result["sessions"] == []
    assert result["skipped"] and "不存在" in result["skipped"][0]["reason"]


def test_transcript_path_rejects_traversal_and_bad_shapes(tmp_home):
    for bad in ("../../evil", "imp-123", "imp-ZZZZZZZZ", "", "imp-00000000/../x"):
        with pytest.raises(AgentImportInvalid):
            transcript_path(bad)


async def test_bad_ids_are_treated_as_missing(imports):
    assert await imports.get_session("../../etc/passwd") is None
    assert await imports.get_session("imp-nope") is None
    with pytest.raises(AgentSessionNotFound):
        await imports.delete("../imp-00000000")
    with pytest.raises(AgentSessionNotFound):
        await imports.read_transcript("imp-deadbeef")


async def test_delete_removes_row_and_transcript(imports, projects):
    _one_session(projects)
    session_id = (await imports.import_sessions())["imported"][0]["id"]
    path = transcript_path(session_id)
    assert path.is_file()

    assert await imports.delete(session_id) == {"deleted": session_id}
    assert not path.exists()
    assert await imports.get_session(session_id) is None
    assert (await imports.list_sessions())["total"] == 0
    with pytest.raises(AgentSessionNotFound):
        await imports.delete(session_id)


# ---- 搜索与分页 ----


async def test_search_matches_title_and_body_without_wildcards(imports, projects):
    _one_session(projects, session_id=SESSION_A, title="进度 50% 的那次讨论")
    _one_session(projects, session_id=SESSION_B, title="下划线_标题")
    await imports.import_sessions()

    hit = await imports.list_sessions(q="50%")
    assert [entry["title"] for entry in hit["sessions"]] == ["进度 50% 的那次讨论"]
    assert (await imports.list_sessions(q="%"))["total"] == 1  # `%` 按字面搜，不是通配
    assert (await imports.list_sessions(q="_"))["total"] == 1
    assert (await imports.list_sessions(q="三路切分"))["total"] == 2  # 正文前段也搜


async def test_read_transcript_paginates(imports, projects):
    path = projects / "proj-alpha" / f"{SESSION_A}.jsonl"
    lines = []
    for index in range(12):
        lines.append(_user(f"问题 {index}", ts="2026-10-01T10:00:00.000Z"))
        lines.append(_assistant([{"type": "text", "text": f"回答 {index}"}]))
    _write(path, lines)
    session_id = (await imports.import_sessions())["imported"][0]["id"]

    page = await imports.read_transcript(session_id, limit=5, offset=0)
    assert page["total"] == 24 and page["has_more"] is True
    assert [message["text"] for message in page["messages"]] == [
        "问题 0",
        "回答 0",
        "问题 1",
        "回答 1",
        "问题 2",
    ]
    tail = await imports.read_transcript(session_id, limit=5, offset=20)
    assert tail["has_more"] is False and len(tail["messages"]) == 4
    assert tail["messages"][-1]["text"] == "回答 11"


async def test_search_text_is_capped(imports, projects):
    path = projects / "proj-alpha" / f"{SESSION_A}.jsonl"
    _write(path, [_user("长" * 3000) for _ in range(5)])
    await imports.import_sessions()
    rows = await imports._db.fetch_all("SELECT search_text FROM agent_sessions")
    assert len(rows[0]["search_text"]) <= SEARCH_TEXT_MAX_CHARS


# ---- REST ----


async def test_api_import_roundtrip(client, projects):
    _one_session(projects)
    scanned = (
        await client.get("/api/v1/agents/imports/candidates", params={"root": str(projects)})
    ).json()
    assert scanned["sessions"][0]["imported"] is False
    assert scanned["sources"] == ["claude_code"]

    imported = (
        await client.post(
            "/api/v1/agents/imports", json={"root": str(projects), "ids": [], "days": 0}
        )
    ).json()
    session_id = imported["imported"][0]["id"]

    listed = (await client.get("/api/v1/agents/imports", params={"q": "三路"})).json()
    assert listed["total"] == 1
    assert listed["sessions"][0]["message_count"] == 3

    detail = (await client.get(f"/api/v1/agents/imports/{session_id}")).json()
    assert detail["total"] == 3
    assert "三路切分" in detail["messages"][-1]["text"]

    assert (await client.delete(f"/api/v1/agents/imports/{session_id}")).status_code == 200
    assert (await client.get(f"/api/v1/agents/imports/{session_id}")).status_code == 404


async def test_api_rejects_a_foreign_root(client, projects, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    scan = await client.get("/api/v1/agents/imports/candidates", params={"root": str(outside)})
    assert scan.status_code == 400
    assert scan.json()["error"]["code"] == "agent_import_invalid"

    post = await client.post("/api/v1/agents/imports", json={"root": str(outside)})
    assert post.status_code == 400
    assert post.json()["error"]["code"] == "agent_import_invalid"


async def test_api_unknown_or_malformed_id_is_404(client, projects):
    for bad in ("imp-deadbeef", "imp-123", "..%2F..%2Fetc"):
        assert (await client.get(f"/api/v1/agents/imports/{bad}")).status_code == 404
        assert (await client.delete(f"/api/v1/agents/imports/{bad}")).status_code == 404
