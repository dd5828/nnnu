"""笔记本与记录模型（§7.15 / §8.2 表结构）。"""

import time
from typing import Literal

from pydantic import BaseModel, Field

from nnnu.core.ids import new_id

# 批一只产 chat（聊天问答）/ solve（深度解题）/ note（手写笔记）三种；
# question/research 随 §7.15 后续条目（题库、深度研究）再加。
RecordType = Literal["chat", "solve", "note"]


class Notebook(BaseModel):
    id: str
    name: str
    description: str | None = None
    created_at: float = Field(default_factory=time.time)

    @classmethod
    def new(cls, **kwargs) -> "Notebook":
        return cls(id=new_id("nb"), **kwargs)


class NotebookRecord(BaseModel):
    id: str
    notebook_id: str
    type: RecordType
    title: str
    content_md: str
    source_ref: str | None = None  # 来源（如会话 id），供「记录从哪来」追溯
    created_at: float = Field(default_factory=time.time)

    @classmethod
    def new(cls, *, notebook_id: str, **kwargs) -> "NotebookRecord":
        return cls(id=new_id("nbr"), notebook_id=notebook_id, **kwargs)
