from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

from .config import settings
from .errors import AppError
from .lifecycle import lifespan
from .queries import anime_query as _anime_query
from .queries import group_query as _group_query
from .routers import anime, libraries, matching, media, settings as settings_router
from .routers import sources, system, tasks
from .routers.media import resolve_media_stream_path as _resolve_media_stream_path
from .services.bulk_matching import (
    run_bulk_search_confirm as _run_bulk_search_confirm,
)
from .source_settings import (
    DEFAULT_SETTINGS,
    SCRAPER_NAMES,
    enabled_scraper_names,
)
from .task_events import (
    publish_task_event as _publish_task_event,
)
from .task_events import (
    task_event_payload as _task_event_payload,
)
from .task_events import (
    task_event_stream as _task_event_stream,
)


def create_app() -> FastAPI:
    application = FastAPI(
        title="Anime Manager API",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.frontend_origin, "http://localhost:5173"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    application.include_router(system.router)
    application.include_router(libraries.router)
    application.include_router(tasks.router)
    application.include_router(media.router)
    application.include_router(sources.router)
    application.include_router(matching.router)
    application.include_router(anime.router)
    application.include_router(settings_router.router)
    return application


app = create_app()


@app.exception_handler(AppError)
async def app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "code": exc.code,
            "message": exc.message,
            "details": exc.details,
            "retryable": exc.retryable,
        },
    )


@app.exception_handler(IntegrityError)
async def integrity_handler(_request: Request, exc: IntegrityError) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "code": "CONFLICT",
            "message": "数据与现有记录冲突",
            "details": str(exc.orig),
            "retryable": False,
        },
    )


@app.exception_handler(RequestValidationError)
async def validation_handler(
    _request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "code": "VALIDATION_ERROR",
            "message": "请求参数校验失败",
            "details": exc.errors(),
            "retryable": False,
        },
    )
