"""FastAPI 应用装配：lifespan 引导、中间件、路由挂载与运行时依赖注入。

app.state.db / app.state.runtime：lifespan 创建（测试隔离天然），
路由经 request.app.state 取用——不搞模块级单例。
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from nnnu import __version__
from nnnu.api.routers import (
    attachments,
    chat,
    cost,
    health,
    knowledge,
    notebooks,
    plugins,
    sessions,
    settings,
    unified_ws,
)
from nnnu.runtime import bootstrap

logger = logging.getLogger(__name__)


def _check_prompt_parity() -> None:
    """启动校验提示词中英键集合一致（§10.1），差异仅告警。"""
    from nnnu.services.i18n.prompts import get_prompt_manager

    for issue in get_prompt_manager().check_parity():
        logger.warning("提示词中英不一致: %s", issue)


# P0 默认本机前端来源（§11.5）；P2 起读取 network.cors_origins 设置项
DEFAULT_CORS_ORIGINS = [
    "http://localhost:3782",
    "http://127.0.0.1:3782",
]


def create_app() -> FastAPI:
    """应用工厂：装配 lifespan、中间件与路由。"""

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # 目录树自举失败不阻断（后续步骤各自降级）
        try:
            bootstrap.ensure_bootstrap()
        except Exception:
            logger.exception("启动步骤 bootstrap 失败，继续启动")
        # schema 迁移是硬步骤（§8.4）：失败中止启动，绝不静默丢数据
        from nnnu.runtime import home as runtime_home
        from nnnu.services.sessions import schema

        schema.migrate(runtime_home.get_data_root())
        # 其余子系统单独 try/except：单个失败不阻断启动
        for step, func in (
            ("registry", bootstrap.register_builtins),
            ("logging", bootstrap.configure_logging),
            ("prompt-parity", _check_prompt_parity),
        ):
            try:
                func()
            except Exception:
                logger.exception("启动步骤 %s 失败，继续启动", step)
        # 运行时装配：数据库 + 会话/成本服务 + 编排器 + TurnRuntime（§6.5 唯一收敛点）
        from nnnu.runtime.orchestrator import ChatOrchestrator
        from nnnu.runtime.registry.capability_registry import get_capability_registry
        from nnnu.runtime.registry.tool_registry import get_tool_registry
        from nnnu.runtime.turn_runtime import TurnRequest, TurnRuntimeManager
        from nnnu.services.cost.service import CostService
        from nnnu.services.files.service import AttachmentsService
        from nnnu.services.notebooks.service import NotebookService
        from nnnu.services.sessions.db import Database
        from nnnu.services.sessions.schema import db_path
        from nnnu.services.sessions.service import SessionManager

        db = Database(db_path(runtime_home.get_data_root()))
        await db.connect()
        session_manager = SessionManager(db)
        cost_service = CostService(db)
        orchestrator = ChatOrchestrator(
            capabilities=get_capability_registry(), tools=get_tool_registry()
        )
        _app.state.db = db
        _app.state.attachments = AttachmentsService(db, runtime_home.get_data_root())
        _app.state.notebooks = NotebookService(db)
        _app.state.runtime = TurnRuntimeManager(
            sessions=session_manager, costs=cost_service, orchestrator=orchestrator
        )
        # cron 调度：到点任务以新回合执行（§7.2），装配进 app.state 供工具层取用
        from nnnu.services.cron.scheduler import CronService, set_cron_service

        cron_service = CronService(db)
        set_cron_service(cron_service)

        async def cron_executor(job) -> str:
            # 能力跟着任务绑定的会话走：挂在解题会话上的定时任务不该跑成聊天
            request = TurnRequest(
                message=job.prompt,
                session_id=job.session_id,
                capability=await _app.state.runtime.session_capability(job.session_id),
            )
            try:
                await _app.state.runtime.start_turn(request)
                return "ok"
            except Exception as exc:
                return f"error: {exc}"

        cron_service.set_executor(cron_executor)
        await cron_service.start()
        _app.state.cron = cron_service
        # 知识库（§7.9）：嵌入服务 + KB 服务装配进单例与 app.state，
        # 启动时 recover_stale 收掉上次进程留下的半成品构建（幂等）
        from nnnu.services.embedding.service import (
            EmbeddingService,
            set_embedding_service,
        )
        from nnnu.services.knowledge.service import KBService, set_kb_service

        data_root = runtime_home.get_data_root()
        embedding = EmbeddingService()
        set_embedding_service(embedding)
        kb_service = KBService(data_root, embedder=embedding)
        set_kb_service(kb_service)
        _app.state.kb = kb_service
        try:
            await kb_service.recover_stale()
        except Exception:  # 恢复失败不该拦住启动：库各自的 manifest 仍是权威
            logger.exception("知识库启动恢复失败，继续启动")
        yield
        await kb_service.shutdown()
        set_kb_service(None)
        set_embedding_service(None)
        await embedding.aclose()
        await cron_service.stop()
        await db.close()

    app = FastAPI(
        title="nnnu API",
        version=__version__,
        lifespan=lifespan,
        redirect_slashes=False,
    )

    @app.exception_handler(Exception)
    async def json_error_boundary(_request: Request, exc: Exception) -> JSONResponse:
        """未捕获异常统一 500 JSON 信封；经异常中间件返回才能带 CORS 头。"""
        logger.exception("未处理异常")
        return JSONResponse(
            status_code=500,
            content={
                "error": {"code": "internal_error", "message": str(exc), "recoverable": False}
            },
        )

    # 中间件顺序：CORS 在最外层，500 响应也能带上允许头
    app.add_middleware(
        CORSMiddleware,
        allow_origins=DEFAULT_CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    app.include_router(settings.router)
    app.include_router(attachments.router)
    app.include_router(knowledge.router)
    app.include_router(notebooks.router)
    app.include_router(plugins.router)
    app.include_router(chat.router)
    app.include_router(sessions.router)
    app.include_router(cost.router)
    app.include_router(unified_ws.router)
    return app


# 模块级实例：uvicorn 字符串导入（reload 模式必需）与容器入口使用；
# 测试走 create_app() 工厂以隔离 lifespan
app = create_app()
