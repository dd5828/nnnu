"""会话与消息模型（§6.8 / §8.2 表结构）。"""

import time
from typing import Any, Literal

from pydantic import BaseModel, Field

from nnnu.core.ids import new_id

MessageRole = Literal["user", "assistant", "tool", "system"]


class Session(BaseModel):
    id: str
    title: str = ""
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    capability: str = "chat"
    model: str | None = None
    persona: str | None = None
    kb_ids: list[str] = Field(default_factory=list)
    tool_overrides: dict[str, Any] = Field(default_factory=dict)
    language: str = "zh"

    @classmethod
    def new(cls, **kwargs) -> "Session":
        return cls(id=new_id("sess"), **kwargs)


class Message(BaseModel):
    id: str
    session_id: str
    role: MessageRole
    content: str = ""
    thinking: str | None = None
    tool_calls: list[dict] = Field(default_factory=list)
    citations: list[dict] = Field(default_factory=list)
    cost: dict | None = None
    created_at: float = Field(default_factory=time.time)

    @classmethod
    def new(cls, *, session_id: str, role: MessageRole, **kwargs) -> "Message":
        return cls(id=new_id("msg"), session_id=session_id, role=role, **kwargs)
