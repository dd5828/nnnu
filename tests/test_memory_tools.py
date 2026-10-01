"""记忆工具（§7.10）：read_memory / write_memory 的参数面、守卫与引用锚点。

零 LLM：工具本身只读写本地文件。
"""

import pytest
from httpx import ASGITransport, AsyncClient

from nnnu.core.tool_protocol import ToolContext
from nnnu.services.memory import trace
from nnnu.services.memory.models import MemoryEntry
from nnnu.services.memory.service import MemoryService, set_memory_service
from nnnu.services.memory.state import doc_key
from nnnu.tools.builtin.memory_tools import ReadMemoryTool, WriteMemoryTool


@pytest.fixture
def service(tmp_path, tmp_home):
    svc = MemoryService(tmp_path)
    set_memory_service(svc)
    yield svc
    set_memory_service(None)


def _ctx(*, turn_id: str = "turn-1", **args) -> ToolContext:
    return ToolContext(turn_id=turn_id, session_id="sess-1", args=args)


def _seed_entry(service: MemoryService, layer: str, key: str, text: str, refs: list[str]) -> None:
    entry = MemoryEntry(id=None, date="2026-09-18", text=text, refs=refs, layer=layer, key=key)
    doc = service.store.load(layer, key)
    doc.entries.append(entry)
    service.store.save(layer, key, doc)


# ---- read_memory ----


async def test_read_memory_unwired(service):
    set_memory_service(None)
    result = await ReadMemoryTool().run(_ctx())
    assert result.ok is False and "未启用" in result.output


async def test_read_memory_empty(service):
    result = await ReadMemoryTool().run(_ctx())
    assert result.ok is True and result.output == "暂无记忆。"
    assert result.detail == {"empty": True}


async def test_read_memory_reads_l3_and_current_surface_l2(service):
    _seed_entry(service, "l3", "profile", "用户在做信号处理方向的学习", ["L2:chat#mem-1a2b3c4d"])
    _seed_entry(service, "l2", "chat", "用户偏好先看结论再看推导", ["L1:chat/2026-09.jsonl#3"])
    _seed_entry(service, "l2", "chat", "偏好：公式要配图解释", ["L1:chat/2026-09.jsonl#9"])
    _seed_entry(service, "l2", "deep_solve", "别的分面不该出现", ["L1:deep_solve/2026-09.jsonl#1"])

    result = await ReadMemoryTool().run(_ctx())
    assert result.ok is True
    assert "用户在做信号处理方向的学习" in result.output
    assert "用户偏好先看结论再看推导" in result.output
    assert "别的分面不该出现" not in result.output
    assert result.detail["layer"] == "all" and result.detail["surface"] == "chat"
    assert result.detail["truncated"] is False

    # l2 单层：不带 L3
    only_l2 = await ReadMemoryTool().run(_ctx(layer="l2"))
    assert "信号处理" not in only_l2.output and "先看结论" in only_l2.output
    # 显式指分面
    other = await ReadMemoryTool().run(_ctx(surface="deep_solve"))
    assert "别的分面不该出现" in other.output
    # 未注册的回合（没走过 begin_turn）退到默认分面 chat
    assert (await ReadMemoryTool().run(_ctx(turn_id="turn-unseen"))).detail["surface"] == "chat"


async def test_read_memory_budget_and_full(service):
    # 单篇：默认 ~1200 字（full ~4000 字），条目数也被限
    for index in range(30):
        _seed_entry(service, "l3", "profile", f"画像第{index:02d}条：" + "长" * 88, [])
    compact = await ReadMemoryTool().run(_ctx(layer="l3"))
    full = await ReadMemoryTool().run(_ctx(layer="l3", full=True))
    assert "画像第29条" not in compact.output and "画像第29条" in full.output
    assert len(compact.output) < len(full.output)
    assert compact.detail["truncated"] is False  # 单篇没撞 2400 总上限


async def test_read_memory_hard_cap(service):
    # 全景：L3 四篇 + 一面 L2 加总撞 2400 硬上限（full 8000）
    for doc in ("profile", "recent", "scope", "preferences"):
        for index in range(30):
            _seed_entry(service, "l3", doc, f"{doc}第{index:02d}条：" + "长" * 88, [])
    for index in range(30):
        _seed_entry(service, "l2", "chat", f"观察第{index:02d}条：" + "长" * 88, [])
    compact = await ReadMemoryTool().run(_ctx())
    full = await ReadMemoryTool().run(_ctx(full=True))
    assert compact.detail["truncated"] is True and compact.detail["chars"] == 2400
    assert full.detail["truncated"] is True and full.detail["chars"] == 8000


async def test_read_memory_validation(service):
    assert (await ReadMemoryTool().run(_ctx(layer="l9"))).ok is False
    assert (await ReadMemoryTool().run(_ctx(surface="nope"))).ok is False


async def test_read_memory_marks_stale(service):
    _seed_entry(service, "l2", "chat", "过期的旧偏好", ["L1:chat/2026-09.jsonl#2"])
    doc = service.store.load("l2", "chat")
    doc.entries[0].stale = True
    service.store.save("l2", "chat", doc)
    result = await ReadMemoryTool().run(_ctx(layer="l2"))
    assert "已被标为过期" in result.output


# ---- write_memory ----


async def test_write_memory_requires_turn_anchor(service):
    result = await WriteMemoryTool().run(_ctx(text="用户在做信号处理方向的学习"))
    assert result.ok is False and "锚点" in result.output
    assert service.store.load("l2", "chat").entries == []  # 没锚点就不写
    set_memory_service(None)
    assert (await WriteMemoryTool().run(_ctx(text="x"))).ok is False


async def test_write_memory_appends_with_l1_ref(service):
    await service.begin_turn("turn-1", "sess-1", "chat", "记住我在做信号处理")
    result = await WriteMemoryTool().run(_ctx(text="用户在做信号处理方向的学习"))
    assert result.ok is True
    entry_id = result.detail["entry_id"]
    assert entry_id and result.detail["surface"] == "chat"

    entries = service.store.load("l2", "chat").entries
    assert len(entries) == 1 and entries[0].id == entry_id and entries[0].date
    ref_body = entries[0].refs[0][len("L1:") :]
    assert trace.resolve_ref(service.data_root, ref_body) is not None  # 引用天然可解析
    meta = service.state.entry_meta(doc_key("l2", "chat"), entry_id)
    assert meta is not None and meta["origin"] == "model" and meta["edited"] is False

    # 重复写：归一化去重，不新增
    again = await WriteMemoryTool().run(_ctx(text="用户在做信号处理方向的学习"))
    assert again.ok is True and again.detail == {"duplicate": True}
    assert len(service.store.load("l2", "chat").entries) == 1


async def test_write_memory_banned_and_limits(service):
    await service.begin_turn("turn-1", "sess-1", "chat", "问题")
    banned = await WriteMemoryTool().run(_ctx(text="用户总是跳过推导过程"))
    assert banned.ok is False and "总是" in banned.output
    assert service.store.load("l2", "chat").entries == []
    # 引号内的转述豁免
    quoted = await WriteMemoryTool().run(
        _ctx(text="用户说过「我总是看不懂证明」，所以现在优先讲直觉")
    )
    assert quoted.ok is True
    # 超长截到 400 字
    long = await WriteMemoryTool().run(_ctx(text="观" * 500))
    assert long.ok is True
    stored = service.store.load("l2", "chat").entries[-1]
    assert len(stored.text) == 400
    # case：显式分面 → 写进那个文件，但引用仍指本回合真实锚点（chat 行）
    other = await WriteMemoryTool().run(_ctx(text="解方程时偏好先化标准型", surface="deep_solve"))
    assert other.ok is True
    entry = service.store.load("l2", "deep_solve").entries[0]
    assert entry.refs[0].startswith("L1:chat/")
    assert (await WriteMemoryTool().run(_ctx(text="x", surface="nope"))).ok is False


# ---- 提示词键与插件目录 ----


async def test_tool_catalog_keys_render(tmp_home, repo_prompts):
    """chat.yaml 的 tool_descriptions / tool_cost_hints 两语言都真渲染出文案。"""
    from nnnu.api.main import create_app

    app = create_app()
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            for lang, marker in (("zh", "长期"), ("en", "long-term")):
                data = (await client.get("/api/v1/plugins", params={"lang": lang})).json()
                tools = {item["definition"]["name"]: item["definition"] for item in data["tools"]}
                for name in ("read_memory", "write_memory"):
                    definition = tools[name]
                    assert definition["mount"] == "user_toggleable"
                    assert marker in definition["description"], name  # 渲染成功而非回退键
                    assert definition["cost_hint"]
