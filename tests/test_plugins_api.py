"""plugins 内省 API：清单、schema 与可用性。"""

from nnnu.runtime import bootstrap


async def test_plugins_lists_chat_and_ask_user(client):
    bootstrap.register_builtins()
    resp = await client.get("/api/v1/plugins")
    assert resp.status_code == 200
    data = resp.json()
    tool_names = [item["definition"]["name"] for item in data["tools"]]
    assert "ask_user" in tool_names
    ask_user = next(item for item in data["tools"] if item["definition"]["name"] == "ask_user")
    assert ask_user["available"] is True
    assert ask_user["definition"]["mount"] == "always"
    assert "parameters" in ask_user["definition"]
    capability_names = [item["name"] for item in data["capabilities"]]
    assert "chat" in capability_names
    chat = next(item for item in data["capabilities"] if item["name"] == "chat")
    assert chat["stages"] == []
    assert chat["default_model_role"] == "chat"
