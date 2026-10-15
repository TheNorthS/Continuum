import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.api.middleware import OperationalMiddleware
from app.api.security_middleware import SecurityMiddleware
from app.api.v1 import attachments, auth, chat, graph, health, knowledge, memory, observability, security
from app.common.errors import AppError
from app.common.redaction import RedactionFilter
from app.config import Settings
from app.infrastructure.container import Container


def create_app(settings: Settings | None = None):
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app):
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
        from pydantic import SecretStr

        values = [v.get_secret_value() for v in settings.__dict__.values() if isinstance(v, SecretStr)]
        redactor = RedactionFilter(values)
        for handler in logging.getLogger().handlers:
            handler.addFilter(redactor)
        services = Container(settings)
        app.state.container = services
        try:
            await services.start()
            yield
        finally:
            await services.close()
            for handler in logging.getLogger().handlers:
                handler.removeFilter(redactor)

    app = FastAPI(
        title="知恒 · Continuum API",
        description=(
            "知识与记忆双驱动的长程多模态智能体平台\n\n"
            "Knowledge- and Memory-Driven Platform for Long-Horizon Multimodal Agents"
        ),
        version="1.0.0",
        lifespan=lifespan,
        docs_url=None if settings.environment == "production" else "/docs",
        redoc_url=None if settings.environment == "production" else "/redoc",
        openapi_url=None if settings.environment == "production" else "/openapi.json",
    )

    def error(request, code, message, status):
        return JSONResponse(
            {
                "error": {
                    "code": code,
                    "message": message,
                    "request_id": getattr(request.state, "request_id", ""),
                }
            },
            status_code=status,
        )

    @app.exception_handler(AppError)
    async def application_error(request: Request, exc: AppError):
        return error(request, exc.code, exc.message, exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return error(request, "validation_error", "请求参数不合法，请检查输入长度和格式", 422)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        return error(request, "http_error", str(exc.detail), exc.status_code)

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        logging.getLogger(__name__).error(
            "unhandled_error request_id=%s type=%s",
            getattr(request.state, "request_id", ""),
            type(exc).__name__,
        )
        return error(request, "internal_error", "服务内部错误，请稍后重试", 500)

    for router in (
        attachments.router,
        memory.router,
        auth.router,
        chat.router,
        knowledge.router,
        health.router,
        graph.router,
        observability.router,
        security.router,
    ):
        app.include_router(router, prefix="/api/v1")
    app.add_middleware(OperationalMiddleware, settings=settings)
    app.add_middleware(SecurityMiddleware, settings=settings)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-Proof"],
        expose_headers=["X-Request-ID"],
    )
    return app


app = create_app()
