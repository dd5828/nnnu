"""学习回合的 ask_user 缝：**卡面归位** + **学习者原话落库**。

拆成八个工具后，出题是模型自己调 `ask_user` 发的（三段式的第二段）。这就留了两个口子，
本模块在 `ask_user_fn` 的外面包一层把它们堵上——这是学习路径唯一的传输层改动，
`runtime/turn_runtime.py` 只提供暂停点，不认识 learning（对应上游 loop hook 的
`on_user_resume`，`deeptutor/capabilities/mastery/loop.py`，只作对照）。

**口子一：模型可以在 ask_user 里换一道题。** 它先 `mastery_quiz` 登记了题 X（答案键
在服务端），转手却把题 Y 发给用户——用户答的题和判分的题就不是同一道了。所以发卡前，
只要本路径有未决的题，就把卡面**按库里那道题重写一遍**（题干/选项/小字），模型传什么
都不作数。用户看到的永远是有答案键的那道题。

**口子二：模型转述作答可以走样。** 学习者的原话在返回给模型**之前**先落进
`learning_interactions.user_answer`；`mastery_grade` 只认这一列，模型说什么都不算数。
同时把原话压在 `ctx.metadata` 上，定性门（`mastery_assess`）也取它——那里没有登记过的
题，只能靠这一手拿到原话。

两处都是 best-effort：没绑路径、没登记题、落库失败，一律当无事发生，绝不打断回合。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from nnnu.core.stream_bus import StreamBus
from nnnu.services.i18n.prompts import get_prompt_manager
from nnnu.services.learning.service import LearningService
from nnnu.services.question_bank.service import get_question_bank
from nnnu.tools.builtin.mastery.common import ANSWER_KEY, path_for
from nnnu.tools.builtin.mastery.quiz import CHOICE_TYPES

if TYPE_CHECKING:
    from nnnu.core.context import UnifiedContext

logger = logging.getLogger(__name__)

AskFn = Callable[..., Awaitable[str]]

# 选项标签由位置推导（题库的口径，见 services/question_bank/models.py）
LABELS = "ABCDEFGH"


def install(ctx: UnifiedContext, bus: StreamBus, service: LearningService) -> None:
    """把 `ctx.metadata["ask_user_fn"]` 换成包了一层的版本（没装暂停函数就什么都不做）。"""
    inner: AskFn | None = ctx.metadata.get("ask_user_fn")
    if inner is None:
        return
    ctx.metadata["ask_user_fn"] = _AskSeam(inner, ctx, bus, service)


class _AskSeam:
    """一层薄壳：进卡前归位卡面，出卡后落库原话（其余原样透传）。"""

    def __init__(
        self,
        inner: AskFn,
        ctx: UnifiedContext,
        bus: StreamBus,
        service: LearningService,
    ) -> None:
        self._inner = inner
        self._ctx = ctx
        self._bus = bus
        self._service = service

    async def __call__(
        self,
        question: str,
        options: list[dict[str, str]],
        ask_id: str,
        *,
        allow_free_text: bool = False,
        context: str = "",
    ) -> str:
        path = await self._path()
        if path is not None:
            bound = await self._bind_card(path.id)
            if bound is not None:
                question, options, context = bound
                # 归位后一律放开手输：选择题也让用户能直接敲标签，简答更得能敲
                allow_free_text = True
        reply = await self._inner(
            question, options, ask_id, allow_free_text=allow_free_text, context=context
        )
        self._ctx.metadata[ANSWER_KEY] = reply or ""
        if path is not None:
            await self._record(path.id, reply)
        return reply

    async def _path(self) -> Any:
        """每发一次卡都现查一次：模型可能**刚在本回合里**建好或切了路径。"""
        config_path_id = str(self._ctx.config.get("path_id") or "")
        try:
            return await path_for(self._service, self._ctx.session.id, config_path_id)
        except Exception:
            logger.exception("学习路径解析失败，本卡按无路径处理")
            return None

    async def _bind_card(self, path_id: str) -> tuple[str, list[dict[str, str]], str] | None:
        """把卡面换成库里那道题的题面；没有未决的题（或题被删了）就返回 None 放行。"""
        pending = await self._service.pending_interaction(path_id)
        if pending is None or not pending.question_id:
            return None  # 定性门没有登记题（question_id 空），不归位
        question = await get_question_bank().get_question(pending.question_id)
        if question is None:
            return None
        body = question.stem or pending.card_prompt
        options = (
            [{"label": label, "description": text} for label, text in zip(LABELS, question.options)]
            if question.type in CHOICE_TYPES
            else []
        )
        context = ""
        if question.knowledge_point:
            context = (
                get_prompt_manager()
                .render(
                    "mastery",
                    self._ctx.language,
                    "quiz.card_context",
                    title=question.knowledge_point,
                )
                .strip()
            )
        return body, options, context

    async def _record(self, path_id: str, reply: str) -> None:
        """原话落库（**不判分、不改状态**）：判分那一刻读的就是这一列。"""
        if not (reply or "").strip():
            return
        try:
            await self._service.record_question_answer(
                path_id,
                user_answer=reply,
                session_id=self._ctx.session.id,
                turn_id=self._bus.turn_id,
            )
        except Exception:
            logger.exception("学习作答落库失败 turn=%s", self._bus.turn_id)
