"""启动引导：幂等创建 data 目录树与结构化日志。

schema 版本文件自 v2 起由 services/sessions/schema.py 唯一负责（§8.4 单写者），
本模块不再写入。
"""

import json
import logging
import logging.handlers
import os
from pathlib import Path

from nnnu.runtime import home

# §8.1 目录布局：启动时随 data 根目录自动创建（全部 gitignore）
DATA_SUBDIRS: tuple[str, ...] = (
    "user/settings",
    "user/memory/trace",
    "user/memory/L2",
    "user/memory/L3",
    "user/sessions",
    "user/notebooks",
    "user/question_bank",
    "user/books",
    "user/co_writer",
    "user/skills",
    "user/knowledge",
    "user/uploads",
    "user/workspace",
    "user/logs",
    "user/exports",
    "partners",
    "system/user-secrets",
)

_LOGGING_CONFIGURED = False


class JsonLineFormatter(logging.Formatter):
    """§12.4 结构化日志：每行 JSON（time/level/logger/turn_id/session_id/event/exception）。"""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "turn_id": getattr(record, "turn_id", None),
            "session_id": getattr(record, "session_id", None),
            "event": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def ensure_bootstrap() -> Path:
    """幂等创建 data 目录树，返回 data 根目录（schema 迁移见 services/sessions/schema.py）。"""
    data_root = home.get_data_root()
    for sub in DATA_SUBDIRS:
        (data_root / sub).mkdir(parents=True, exist_ok=True)
    # 播种缺失的设置 JSON（绝不覆盖用户文件）；局部导入避免循环依赖
    from nnnu.services.settings.service import get_settings_service

    get_settings_service().seed_missing()
    return data_root


def register_builtins() -> None:
    """内置工具/能力注册清单（§6.11）：幂等，随阶段追加。"""
    from nnnu.capabilities.chat.capability import ChatCapability
    from nnnu.runtime.registry.capability_registry import get_capability_registry
    from nnnu.runtime.registry.tool_registry import get_tool_registry
    from nnnu.tools.builtin.ask_user import AskUserTool
    from nnnu.tools.builtin.attachment_search import AttachmentSearchTool
    from nnnu.tools.builtin.code_execution import CodeExecutionTool
    from nnnu.tools.builtin.exec_tool import ExecTool
    from nnnu.tools.builtin.file_tools import (
        ListWorkspaceTool,
        ReadWorkspaceFileTool,
        WriteWorkspaceFileTool,
    )
    from nnnu.tools.builtin.paper_search_tool import PaperSearchTool
    from nnnu.tools.builtin.web_fetch import WebFetchTool
    from nnnu.tools.builtin.web_search import WebSearchTool

    get_capability_registry().register(ChatCapability.manifest, ChatCapability)
    get_tool_registry().register(AskUserTool())
    get_tool_registry().register(AttachmentSearchTool())
    get_tool_registry().register(CodeExecutionTool())
    get_tool_registry().register(ExecTool())
    get_tool_registry().register(ListWorkspaceTool())
    get_tool_registry().register(ReadWorkspaceFileTool())
    get_tool_registry().register(WriteWorkspaceFileTool())
    get_tool_registry().register(WebSearchTool())
    get_tool_registry().register(PaperSearchTool())
    get_tool_registry().register(WebFetchTool())


def configure_logging(level: str | None = None) -> None:
    """应用日志：data/user/logs/{app,error}.log，JSON 行，10MB 轮转保留 7 份。

    进程内只配置一次；NNNU_LOG_LEVEL 环境变量控制级别（默认 info）。
    """
    global _LOGGING_CONFIGURED
    if _LOGGING_CONFIGURED:
        return
    _LOGGING_CONFIGURED = True

    raw_level = level or os.environ.get("NNNU_LOG_LEVEL") or "info"
    level = raw_level.upper()
    logs_dir = home.get_data_root() / "user" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    formatter = JsonLineFormatter()
    # §12.4 链路关联：回合任务树的所有日志行自动带 turn_id/session_id
    from nnnu.runtime.log_ctx import TurnContextFilter

    turn_filter = TurnContextFilter()

    app_handler = logging.handlers.RotatingFileHandler(
        logs_dir / "app.log", maxBytes=10 * 1024 * 1024, backupCount=7, encoding="utf-8"
    )
    app_handler.setFormatter(formatter)
    app_handler.setLevel(level)
    app_handler.addFilter(turn_filter)

    error_handler = logging.handlers.RotatingFileHandler(
        logs_dir / "error.log", maxBytes=10 * 1024 * 1024, backupCount=7, encoding="utf-8"
    )
    error_handler.setFormatter(formatter)
    error_handler.setLevel("ERROR")
    error_handler.addFilter(turn_filter)

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(app_handler)
    root.addHandler(error_handler)
