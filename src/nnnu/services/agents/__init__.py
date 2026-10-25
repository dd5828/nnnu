"""子代理（§7.12）：连接配置、无头 CLI 实时咨询、历史会话导入。"""

from nnnu.services.agents.service import (
    AgentConnectionInvalid,
    AgentConnectionNotFound,
    AgentService,
    get_agent_service,
    set_agent_service,
)

__all__ = [
    "AgentConnectionInvalid",
    "AgentConnectionNotFound",
    "AgentService",
    "get_agent_service",
    "set_agent_service",
]
