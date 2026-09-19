"""日志链路：turn_log_context 让回合任务树日志带 turn_id/session_id（§12.4）。"""

import logging

import pytest

from nnnu.runtime.log_ctx import TurnContextFilter, _turn_id, turn_log_context


def test_turn_context_propagates_to_log_records():
    records: list[logging.LogRecord] = []

    class CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("nnnu.test.logctx")
    handler = CaptureHandler()
    handler.addFilter(TurnContextFilter())
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        with turn_log_context("turn-1", "sess-1"):
            logger.info("回合内日志")
        logger.info("回合外日志")
    finally:
        logger.removeHandler(handler)

    assert records[0].turn_id == "turn-1"
    assert records[0].session_id == "sess-1"
    # 回合外为空（字段仍在，值为 None——JSON 行结构稳定）
    assert records[1].turn_id is None
    assert records[1].session_id is None


def test_turn_context_restored_after_exception():
    with pytest.raises(RuntimeError):
        with turn_log_context("turn-2", "sess-2"):
            raise RuntimeError("boom")
    # 上下文退出后 contextvar 复位
    assert _turn_id.get() is None
