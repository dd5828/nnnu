"""笔记本与记录模型（§7.15 / §8.2 表结构）。"""

import time
from typing import Literal

from pydantic import BaseModel, Field

from nnnu.core.ids import new_id

# 批一产 chat（聊天问答）/ solve（深度解题）/ note（手写笔记）；
# question 是批二加的（出题能力的题目清单可存进笔记本）；research 随 §7.15 后续条目再加；
# visualize / math_animator 是 P7 加的（渲染块与动画产物也能存进笔记本）；
# co_writer 是 P9 加的（协作写作的文档存到笔记本）。
# solve / question 是历史类型：解题、出题两个能力已下线（2026-10-03），保留类型只为
# 读旧记录，新写入不再产生。
# 与 `service.py:RECORD_TYPES`、`export.py:_TYPE_LABELS`、前端
# `web/types/api.ts:NotebookRecordType` 与 `web/lib/capabilities.ts:recordTypeFor` 同表。
RecordType = Literal[
    "chat", "solve", "note", "question", "research", "visualize", "math_animator", "co_writer"
]


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
