"""统一上下文（§6.7）：所有能力的回合输入唯一入口。

两类上下文的生命周期（参考仓库语义）：
- 粘性会话上下文：模型/persona/知识库/语言——存在会话上，跨回合保持
  （编排器从会话偏好合并，本模型只承载当前回合的解析结果）；
- 一次性引用：附件/历史会话/笔记本/题库——仅当回合有效（refs 字段）。
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from nnnu.core.tool_protocol import ToolMountFlags
from nnnu.services.cost.tracker import CostTracker
from nnnu.services.sessions.models import Message


class SessionRef(BaseModel):
    id: str
    title: str = ""


class Attachment(BaseModel):
    name: str
    path: str | None = None  # data/user/uploads/ 下归档路径（P2 附件落盘）
    mime: str | None = None


class KbRef(BaseModel):
    kb_id: str


class NotebookRef(BaseModel):
    notebook_id: str
    record_id: str | None = None


class QuestionRef(BaseModel):
    question_id: str


class PersonaRef(BaseModel):
    id: str
    description: str | None = None


class ModelRef(BaseModel):
    provider: str
    model: str


class UnifiedContext(BaseModel):
    # CostTracker 是普通类（非 pydantic 模型），需放行任意类型字段
    model_config = ConfigDict(arbitrary_types_allowed=True)

    session: SessionRef
    capability: str
    message: Message  # 当前用户消息（含附件引用）
    attachments: list[Attachment] = Field(default_factory=list)
    kb_refs: list[KbRef] = Field(default_factory=list)
    notebook_refs: list[NotebookRef] = Field(default_factory=list)
    history_refs: list[SessionRef] = Field(default_factory=list)
    question_refs: list[QuestionRef] = Field(default_factory=list)
    tool_flags: ToolMountFlags = Field(default_factory=ToolMountFlags)
    config: dict[str, Any] = Field(default_factory=dict)  # 能力配置（用户设置 + 请求覆盖）
    persona: PersonaRef | None = None
    language: str = "zh"  # en | zh（影响提示词与输出语言）
    model: ModelRef | None = None  # 模型覆盖
    cost: CostTracker = Field(default_factory=CostTracker)
    metadata: dict[str, Any] = Field(default_factory=dict)  # 运行期通道（ask_user_fn 等）
