"""首回合记忆注入（§7.10 补做）：L3 摘录构建、只注入首回合、任何失败不炸回合。

零 LLM 的单测直接测 build_injection / memory_injection_note；首回合与否的整链
验证走 REST + 记录型 scripted LLM（快照每次调用的消息，见 test_chat_rest 的先例）。
"""

import json
from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest

from nnnu.capabilities import _shared
from nnnu.capabilities._shared import build_user_message, memory_injection_note
from nnnu.core.context import Attachment, SessionRef, UnifiedContext
from nnnu.runtime import home
from nnnu.services.llm.factory import install_scripted, uninstall_scripted
from nnnu.services.llm.protocol import LLMChunk, LLMRequest
from nnnu.services.llm.scripted import ScriptedLLM, ScriptedStep
from nnnu.services.memory.inject import build_injection
from nnnu.services.memory.models import MemoryConfig, MemoryEntry
from nnnu.services.memory.service import MemoryService, get_memory_service, set_memory_service
from nnnu.services.sessions.models import Message


@pytest.fixture(autouse=True)
def _clean_llm_injection():
    uninstall_scripted()
    yield
    uninstall_scripted()


@pytest.fixture
def service(tmp_path, tmp_home):
    svc = MemoryService(tmp_path)
    set_memory_service(svc)
    yield svc
    set_memory_service(None)


class _RecordingLLM(ScriptedLLM):
    def __init__(self, steps: list[ScriptedStep]) -> None:
        super().__init__(steps)
        self.records: list[list[dict]] = []

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMChunk]:
        self.records.append([dict(message) for message in request.messages])
        async for chunk in super().stream(request):
            yield chunk


def _seed(service: MemoryService, doc: str, text: str, *, stale: bool = False) -> None:
    entry = MemoryEntry(
        id=None, date="2026-09-18", text=text, refs=[], stale=stale, layer="l3", key=doc
    )
    loaded = service.store.load("l3", doc)
    loaded.entries.append(entry)
    service.store.save("l3", doc, loaded)


def _user_ctx(text: str, history: list[Message] | None = None) -> UnifiedContext:
    message = Message.new(session_id="sess-1", role="user", content=text)
    return UnifiedContext(
        session=SessionRef(id="sess-1"),
        capability="chat",
        message=message,
        metadata={"session_messages": [*(history or []), message]},
    )


# ---- build_injection ----


def test_build_injection_orders_docs_and_caps(service, repo_prompts):
    for doc in ("profile", "preferences", "scope", "recent"):
        for index in range(5):
            _seed(service, doc, f"{doc}第{index}条")
    lines = build_injection(service.store, "zh").splitlines()
    assert lines[0].startswith("以下是关于用户的长期记忆")
    body = lines[1:]
    assert len(body) == 16  # 四篇 × 每篇 4 条
    assert "- profile第0条" == body[0] and "- profile第4条" not in body
    starts = {
        doc: next(index for index, line in enumerate(body) if line.startswith(f"- {doc}"))
        for doc in ("profile", "preferences", "scope", "recent")
    }
    assert starts["profile"] < starts["preferences"] < starts["scope"] < starts["recent"]
    assert sum(len(line) for line in body) <= 700


def test_build_injection_stops_at_budget(service, repo_prompts):
    for index in range(8):
        _seed(service, "profile", f"第{index}条" + "长" * 96)
    body = build_injection(service.store, "zh").splitlines()[1:]
    assert 0 < len(body) < 8  # 整行放不下就停，不切半句
    assert sum(len(line) for line in body) <= 700


def test_build_injection_skips_stale_and_truncates(service, repo_prompts):
    _seed(service, "profile", "过期条目", stale=True)
    _seed(service, "profile", "长" * 300)
    body = build_injection(service.store, "zh").splitlines()[1:]
    assert body == ["- " + "长" * 120 + "…"]


def test_build_injection_empty_memory_returns_empty(service, repo_prompts):
    assert build_injection(service.store, "zh") == ""


def test_build_injection_english_header(service, repo_prompts):
    _seed(service, "profile", "用户喜欢先看结论")
    assert build_injection(service.store, "en").startswith("Below is long-term memory")


# ---- memory_injection_note ----


def _fake_service(monkeypatch, *, enabled: bool = True) -> list[str]:
    calls: list[str] = []

    def fake_build(store, lang):
        calls.append(lang)
        return "记忆块"

    monkeypatch.setattr("nnnu.services.memory.inject.build_injection", fake_build)
    fake = SimpleNamespace(store=None, config=lambda: MemoryConfig(inject_enabled=enabled))
    monkeypatch.setattr("nnnu.services.memory.service.get_memory_service", lambda: fake)
    return calls


def test_injection_note_first_turn_memoized(monkeypatch):
    calls = _fake_service(monkeypatch)
    ctx = _user_ctx("第一问")
    assert memory_injection_note(ctx) == "记忆块"
    assert memory_injection_note(ctx) == "记忆块"  # memo：同回合只构建一次
    assert calls == ["zh"]


def test_injection_note_skips_later_turns(monkeypatch):
    _fake_service(monkeypatch)
    history = [Message.new(session_id="sess-1", role="user", content="第一问")]
    history.append(Message.new(session_id="sess-1", role="assistant", content="答"))
    ctx = _user_ctx("第二问", history=history)
    assert memory_injection_note(ctx) == ""


def test_injection_note_respects_setting_off(monkeypatch):
    _fake_service(monkeypatch, enabled=False)
    assert memory_injection_note(_user_ctx("第一问")) == ""


def test_injection_note_unwired_service(monkeypatch):
    monkeypatch.setattr("nnnu.services.memory.service.get_memory_service", lambda: None)
    assert memory_injection_note(_user_ctx("第一问")) == ""


def test_injection_note_failure_is_swallowed(monkeypatch):
    def boom(store, lang):
        raise RuntimeError("炸")

    monkeypatch.setattr("nnnu.services.memory.inject.build_injection", boom)
    fake = SimpleNamespace(store=None, config=lambda: MemoryConfig())
    monkeypatch.setattr("nnnu.services.memory.service.get_memory_service", lambda: fake)
    assert memory_injection_note(_user_ctx("第一问")) == ""  # 不抛


# ---- build_user_message 装配 ----


async def test_note_lands_in_text_part_with_image(tmp_path, tmp_home, monkeypatch):
    svc = MemoryService(tmp_path)
    set_memory_service(svc)
    try:
        _seed(svc, "profile", "用户在做信号处理")

        async def fake_image(ctx, bus, attachment, directory, image_parts, provider_id, model):
            image_parts.append(
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}}
            )

        monkeypatch.setattr(_shared, "_inject_image", fake_image)
        directory = home.get_data_root() / "user" / "uploads" / "sess-1" / "att-1"
        directory.mkdir(parents=True)
        (directory / "parsed.json").write_text(json.dumps({"kind": "image"}), encoding="utf-8")
        message = Message.new(session_id="sess-1", role="user", content="看看图")
        ctx = UnifiedContext(
            session=SessionRef(id="sess-1"),
            capability="chat",
            message=message,
            attachments=[Attachment(id="att-1", name="pic.png", mime="image/png")],
            metadata={"session_messages": [message]},
        )
        payload = await build_user_message(ctx, object(), None, "m")
    finally:
        set_memory_service(None)
    assert isinstance(payload["content"], list)
    assert payload["content"][0]["type"] == "text"
    assert "用户在做信号处理" in payload["content"][0]["text"]
    assert payload["content"][1]["type"] == "image_url"


# ---- 整链：REST 首回合注入、次回合不注入 ----


async def test_inject_first_turn_only_end_to_end(client, repo_prompts):
    service = get_memory_service()
    assert service is not None
    _seed(service, "profile", "用户在做信号处理方向的学习")

    recorder = _RecordingLLM([ScriptedStep(chunks=["一"]), ScriptedStep(chunks=["二"])])
    install_scripted(lambda: recorder)
    first = await client.post("/api/v1/chat", json={"message": "你记得我什么？"})
    assert first.status_code == 200
    first_user = recorder.records[0][-1]
    assert first_user["role"] == "user"
    assert "以下是关于用户的长期记忆" in first_user["content"]
    assert "用户在做信号处理方向的学习" in first_user["content"]

    second = await client.post(
        "/api/v1/chat",
        json={"session_id": first.json()["session_id"], "message": "继续"},
    )
    assert second.status_code == 200
    assert "长期记忆" not in recorder.records[1][-1]["content"]


async def test_inject_setting_off_end_to_end(client, repo_prompts):
    service = get_memory_service()
    assert service is not None
    _seed(service, "profile", "用户在做信号处理方向的学习")
    resp = await client.put("/api/v1/settings/memory", json={"values": {"inject_enabled": False}})
    assert resp.status_code == 200

    recorder = _RecordingLLM([ScriptedStep(chunks=["答"])])
    install_scripted(lambda: recorder)
    turn = await client.post("/api/v1/chat", json={"message": "问"})
    assert turn.status_code == 200
    assert "信号处理" not in recorder.records[0][-1]["content"]


async def test_inject_failure_does_not_break_turn(client, repo_prompts, monkeypatch):
    service = get_memory_service()
    assert service is not None
    _seed(service, "profile", "用户在做信号处理方向的学习")

    def boom(store, lang):
        raise RuntimeError("炸")

    monkeypatch.setattr("nnnu.services.memory.inject.build_injection", boom)
    recorder = _RecordingLLM([ScriptedStep(chunks=["照常回答"])])
    install_scripted(lambda: recorder)
    resp = await client.post("/api/v1/chat", json={"message": "问"})
    assert resp.status_code == 200 and resp.json()["response"] == "照常回答"
    assert "长期记忆" not in recorder.records[0][-1]["content"]
