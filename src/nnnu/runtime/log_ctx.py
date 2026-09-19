"""回合日志链路关联（§12.4）：contextvar + 日志过滤器。

TurnRuntime 把整个回合任务包在 turn_log_context 内——回合任务树里
（循环/工具/LLM 客户端）的所有日志行自动带 turn_id/session_id 字段，
P1 验收：grep 一个 turn_id 即可从 app.log 还原回合全序列。
"""

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

_turn_id: ContextVar[str | None] = ContextVar("turn_id", default=None)
_session_id: ContextVar[str | None] = ContextVar("session_id", default=None)


@contextmanager
def turn_log_context(turn_id: str, session_id: str | None) -> Iterator[None]:
    token_turn = _turn_id.set(turn_id)
    token_session = _session_id.set(session_id)
    try:
        yield
    finally:
        _turn_id.reset(token_turn)
        _session_id.reset(token_session)


class TurnContextFilter(logging.Filter):
    """把 contextvar 写入每条日志记录（空值也写，保证 JSON 行结构稳定）。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.turn_id = _turn_id.get()
        record.session_id = _session_id.get()
        return True
