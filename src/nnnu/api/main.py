"""FastAPI 应用装配：lifespan 引导、中间件与路由挂载。"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from nnnu import __version__
from nnnu.api.routers import health, settings
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
            ("logging", bootstrap.configure_logging),
            ("prompt-parity", _check_prompt_parity),
        ):
            try:
                func()
            except Exception:
                logger.exception("启动步骤 %s 失败，继续启动", step)
        yield
        # shutdown 钩子占位（P1 起逐子系统 stop）

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
    return app


# 模块级实例：uvicorn 字符串导入（reload 模式必需）与容器入口使用；
# 测试走 create_app() 工厂以隔离 lifespan
app = create_app()
