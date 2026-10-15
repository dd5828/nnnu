"""技能运行时（§7.16）：挂载门、系统提示清单注入与 read_skill/load_tools 工具。"""

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from nnnu.capabilities._shared import MountedTools, mount_tools
from nnnu.core.context import SessionRef, UnifiedContext
from nnnu.core.tool_protocol import ToolContext
from nnnu.services.i18n.prompts import PromptManager
from nnnu.services.llm.factory import install_scripted, uninstall_scripted
from nnnu.services.llm.protocol import LLMChunk, LLMRequest, LLMToolCall
from nnnu.services.llm.scripted import ScriptedLLM, ScriptedStep
from nnnu.services.sessions.models import Message
from nnnu.services.skills.spec import compose_skill_md
from nnnu.tools.builtin.skill_tools import READ_SKILL_MAX_CHARS, LoadToolsTool, ReadSkillTool

REPO_PROMPTS = Path(__file__).resolve().parents[1] / "prompts"

GOOD = compose_skill_md(
    "my-flash",
    "把材料做成闪卡。",
    "1. 先提炼概念。\n2. 逐张制卡。",
    when_to_use="用户要求做闪卡时",
)


@pytest.fixture(autouse=True)
def _clean_llm_injection():
    uninstall_scripted()
    yield
    uninstall_scripted()


class _RecordingLLM(ScriptedLLM):
    """调用当场抄一份消息快照（同一个 list 会被回合后续追加污染，照 test_chat_rest 先例）。"""

    def __init__(self, steps: list[ScriptedStep]) -> None:
        super().__init__(steps)
        self.records: list[list[dict]] = []

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMChunk]:
        self.records.append([dict(message) for message in request.messages])
        async for chunk in super().stream(request):
            yield chunk


def _ctx(language: str = "zh", **kwargs) -> UnifiedContext:
    return UnifiedContext(
        session=SessionRef(id="s1"),
        capability="chat",
        message=Message.new(session_id="s1", role="user", content="帮我做闪卡"),
        language=language,
        **kwargs,
    )


def _tool_ctx(
    args: dict | None = None, metadata: dict | None = None, language: str = "zh"
) -> ToolContext:
    return ToolContext(
        turn_id="turn-1",
        session_id="s1",
        language=language,
        args=args or {},
        metadata=metadata or {},
    )


async def _create(client, name: str = "my-flash", content: str = GOOD, files: dict | None = None):
    response = await client.post(
        "/api/v1/skills", json={"name": name, "content": content, "files": files}
    )
    assert response.status_code == 200, response.text


# ---- 挂载门与清单注入 ----


async def test_enabled_skill_opens_gate_and_renders_note(client, repo_prompts):
    await _create(client)
    ctx = _ctx()
    mounted = await mount_tools(ctx)
    assert {"read_skill", "load_tools"} <= ctx.tool_flags.context
    assert {"read_skill", "load_tools"} <= set(mounted.tools)
    assert "my-flash" in mounted.skills_note
    assert "把材料做成闪卡。" in mounted.skills_note
    assert "用户要求做闪卡时" in mounted.skills_note
    assert "read_skill" in mounted.skills_note  # 清单末尾附了本回合可用工具
    assert ctx.metadata["mounted_tool_names"] == sorted(mounted.tools)

    en = _ctx(language="en")
    mounted_en = await mount_tools(en)
    assert "Available skills" in mounted_en.skills_note


async def test_disabled_skill_closes_gate(client, repo_prompts):
    await _create(client)
    await client.patch("/api/v1/skills/my-flash", json={"enabled": False})
    ctx = _ctx()
    mounted = await mount_tools(ctx)
    assert "read_skill" not in mounted.tools
    assert mounted.skills_note == ""


async def test_mount_without_service_fails_closed(repo_prompts):
    from nnnu.runtime.bootstrap import register_builtins
    from nnnu.services.skills.service import set_skill_service

    register_builtins()
    set_skill_service(None)
    ctx = _ctx()
    mounted = await mount_tools(ctx)
    assert "read_skill" not in mounted.tools
    assert mounted.skills_note == "" and not ctx.tool_flags.context


async def test_allowlist_without_skill_tool_drops_note(client, repo_prompts):
    await _create(client)
    mounted = await mount_tools(_ctx(), allowlist=("rag",))
    assert mounted.skills_note == ""


def test_filtered_carries_skills_note():
    assert MountedTools(skills_note="N").filtered(("read_skill",)).skills_note == "N"


# ---- read_skill ----


async def test_read_skill_returns_script(client, repo_prompts):
    await _create(client)
    result = await ReadSkillTool().run(
        _tool_ctx({"name": "my-flash"}, {"mounted_tool_names": ["read_skill"]})
    )
    assert result.ok is True
    assert "把材料做成闪卡。" in result.output
    assert "1. 先提炼概念。" in result.output
    assert "何时用" in result.output
    assert "本回合可用工具：read_skill" in result.output
    assert result.detail == {"skill": "my-flash", "files": [], "truncated": False}


async def test_read_skill_not_found_lists_available(client, repo_prompts):
    await _create(client)
    result = await ReadSkillTool().run(_tool_ctx({"name": "nope"}))
    assert result.ok is False
    assert "nope" in result.output
    assert "my-flash" in result.output  # 可用清单：模型据此改口


async def test_read_skill_refuses_disabled(client, repo_prompts):
    await _create(client)
    await client.patch("/api/v1/skills/my-flash", json={"enabled": False})
    result = await ReadSkillTool().run(_tool_ctx({"name": "my-flash"}))
    assert result.ok is False
    assert "停用" in result.output


async def test_read_skill_file_roundtrip(client, repo_prompts):
    content = compose_skill_md("with-file", "带引用文件的技能。", "先读模板再制卡。")
    await _create(
        client, name="with-file", content=content, files={"templates/card.md": "卡片模板正文"}
    )
    listed = await ReadSkillTool().run(_tool_ctx({"name": "with-file"}))
    assert listed.ok is True
    assert "templates/card.md" in listed.output
    assert listed.detail["files"] == ["templates/card.md"]

    read = await ReadSkillTool().run(_tool_ctx({"name": "with-file", "file": "templates/card.md"}))
    assert read.ok is True and "卡片模板正文" in read.output
    assert read.detail == {"skill": "with-file", "file": "templates/card.md"}

    missing = await ReadSkillTool().run(_tool_ctx({"name": "with-file", "file": "nope.md"}))
    assert missing.ok is False and "templates/card.md" in missing.output

    escape = await ReadSkillTool().run(_tool_ctx({"name": "with-file", "file": "../SKILL.md"}))
    assert escape.ok is False


async def test_read_skill_truncates_long_script(client, repo_prompts):
    body = "开头。" + "长" * (READ_SKILL_MAX_CHARS + 500)
    await _create(
        client, name="long-skill", content=compose_skill_md("long-skill", "很长的剧本。", body)
    )
    result = await ReadSkillTool().run(_tool_ctx({"name": "long-skill"}))
    assert result.ok is True
    assert result.detail["truncated"] is True
    assert "长" * (READ_SKILL_MAX_CHARS + 500) not in result.output
    assert len(result.output) < READ_SKILL_MAX_CHARS + 1000


async def test_read_skill_without_service_says_unavailable(repo_prompts):
    from nnnu.services.skills.service import set_skill_service

    set_skill_service(None)
    result = await ReadSkillTool().run(_tool_ctx({"name": "x"}))
    assert result.ok is False
    assert "没装配" in result.output


# ---- load_tools ----


async def test_load_tools_lists_mounted_and_others(client, repo_prompts):
    await _create(client)
    result = await LoadToolsTool().run(
        _tool_ctx(metadata={"mounted_tool_names": ["rag", "read_skill"]})
    )
    assert result.ok is True
    assert "本回合可以调用的工具" in result.output
    assert "其他已安装但本回合没挂的工具" in result.output
    assert "read_skill" in result.output and "exec" in result.output  # 挂的与没挂的都列出
    assert result.detail["mounted"] == ["rag", "read_skill"]
    # 说明文案渲染的是实际提示词，不是回退的键名
    assert "tool_descriptions" not in result.output


# ---- 对话全链路（脚本化 LLM）与提示词键 ----


async def test_chat_turn_reads_skill_via_tool_call(client, repo_prompts):
    """建技能 → 对话触发 read_skill：门开了、清单进了系统提示、工具真跑出剧本正文。"""
    await _create(client)
    recorder = _RecordingLLM(
        [
            ScriptedStep(
                tool_calls=[
                    LLMToolCall(
                        id="c1", name="read_skill", arguments=json.dumps({"name": "my-flash"})
                    )
                ],
                finish_reason="tool_calls",
            ),
            ScriptedStep(chunks=["照剧本做完了"]),
        ]
    )
    install_scripted(lambda: recorder)

    response = await client.post("/api/v1/chat", json={"message": "帮我做闪卡"})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "completed"

    system = recorder.records[0][0]
    assert system["role"] == "system"
    assert "my-flash" in system["content"]
    tool_messages = [m for m in recorder.records[1] if m.get("role") == "tool"]
    assert any("1. 先提炼概念。" in str(m.get("content")) for m in tool_messages)


async def test_disabled_skill_not_reachable_in_turn(client, repo_prompts):
    """停用后系统提示不再有清单，模型调 read_skill 也拿不到（tool 应答是拒绝文案）。"""
    await _create(client)
    await client.patch("/api/v1/skills/my-flash", json={"enabled": False})
    recorder = _RecordingLLM(
        [
            ScriptedStep(
                tool_calls=[LLMToolCall(id="c1", name="load_tools", arguments=json.dumps({}))],
                finish_reason="tool_calls",
            ),
            ScriptedStep(chunks=["好的"]),
        ]
    )
    install_scripted(lambda: recorder)
    response = await client.post("/api/v1/chat", json={"message": "帮我做闪卡"})
    assert response.status_code == 200, response.text
    assert "my-flash" not in recorder.records[0][0]["content"]


def test_skills_prompt_keys_render_both_languages(repo_prompts):
    manager = PromptManager(prompts_root=REPO_PROMPTS)
    assert [issue for issue in manager.check_parity() if issue.startswith("skills")] == []
    cases = {
        "note": {"skills": "- a：b", "tools": "read_skill"},
        "skill_header": {"name": "n", "description": "d"},
        "when_to_use_line": {"when_to_use": "w"},
        "truncated": {"limit": 1},
        "files_header": {"files": "f"},
        "tools_hint": {"tools": "t"},
        "not_found": {"name": "n", "available": "a"},
        "disabled": {"name": "n", "available": "a"},
        "unavailable": {},
        "broken": {"name": "n", "error": "e"},
        "empty_list": {},
        "file_header": {"name": "n", "file": "f"},
        "file_not_found": {"name": "n", "file": "f", "files": "fs"},
        "file_truncated": {"limit": 1},
        "tools_header": {},
        "tools_other_header": {},
        "tool_line": {"name": "n", "description": "d"},
    }
    for lang in ("zh", "en"):
        for key, variables in cases.items():
            rendered = manager.render("skills", lang, key, **variables)
            assert rendered, (lang, key)
            assert not any(part in rendered for part in ("{skills}", "{tools}", "{name}")), (
                lang,
                key,
            )


def test_book_and_co_writer_allowlists_include_skill_tools():
    from nnnu.book.chat import ALLOWED_TOOLS as BOOK_TOOLS
    from nnnu.co_writer.edit import ALLOWED_TOOLS as EDIT_TOOLS

    assert {"read_skill", "load_tools"} <= set(BOOK_TOOLS)
    assert {"read_skill", "load_tools"} <= set(EDIT_TOOLS)
