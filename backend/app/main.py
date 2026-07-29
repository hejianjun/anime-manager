from __future__ import annotations

import asyncio
import json
import mimetypes
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import BackgroundTasks, Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from .config import settings
from .bulk_artifacts import build_bulk_artifact_plan, execute_bulk_artifact_plan
from .database import Base, SessionLocal, engine, get_db
from .errors import AppError
from .exporter import build_export_plan, export_anime, public_export_plan
from .matching import confirm_group, refresh_anime, save_selections, search_group
from .media_ops import (
    bind_group_to_anime,
    build_bulk_rename_plan,
    build_rename_plan,
    execute_bulk_rename_plan,
    execute_rename_plan,
)
from .models import (
    Anime,
    AppSetting,
    LibraryRoot,
    MatchGroup,
    MediaFile,
    ScrapeCandidate,
    SourceMapping,
    TaskRecord,
)
from .scanner import migrate_pending_folder_search_keywords, scan_library
from .schemas import (
    AnimeOut,
    AnimePatch,
    BindExistingRequest,
    BulkSearchConfirmRequest,
    ExportRequest,
    LibraryRootCreate,
    LibraryRootOut,
    LibraryRootPatch,
    MatchGroupOut,
    MatchGroupPatch,
    MediaFilePatch,
    RenameRequest,
    SearchRequest,
    SelectionRequest,
    SettingsPatch,
    TaskOut,
)
from .scrapers import SCRAPERS, AniDBScraper

scheduler = AsyncIOScheduler(timezone="UTC")
SCRAPER_NAMES = tuple(SCRAPERS)
task_event_subscribers: dict[int, set[asyncio.Queue[dict[str, Any]]]] = {}


def _task_event_payload(task: TaskRecord) -> dict[str, Any]:
    result = task.result or {}
    if task.kind == "bulk_search_confirm":
        result = {
            key: result.get(key, 0)
            for key in ("searched", "total", "confirmed", "skipped", "failed")
        }
    return {
        "id": task.id,
        "kind": task.kind,
        "status": task.status,
        "progress": task.progress,
        "message": task.message,
        "result": result,
        "error": task.error,
    }


async def _publish_task_event(task: TaskRecord) -> None:
    payload = _task_event_payload(task)
    for queue in tuple(task_event_subscribers.get(task.id, ())):
        if queue.full():
            queue.get_nowait()
        queue.put_nowait(payload)


async def _task_event_stream(task_id: int, session_factory=None):
    factory = session_factory or SessionLocal
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1)
    subscribers = task_event_subscribers.setdefault(task_id, set())
    subscribers.add(queue)
    try:
        with factory() as db:
            task = db.get(TaskRecord, task_id)
            if not task:
                return
            payload = _task_event_payload(task)
        yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        if payload["status"] in {"completed", "failed"}:
            return
        while True:
            try:
                payload = await asyncio.wait_for(queue.get(), timeout=15)
            except TimeoutError:
                yield ": keep-alive\n\n"
                continue
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            if payload["status"] in {"completed", "failed"}:
                return
    finally:
        subscribers.discard(queue)
        if not subscribers:
            task_event_subscribers.pop(task_id, None)


def enabled_scraper_names(db: Session) -> list[str]:
    row = db.get(AppSetting, "enabled_scrapers")
    configured = row.value if row and isinstance(row.value, list) else list(SCRAPER_NAMES)
    return [name for name in SCRAPER_NAMES if name in configured]


async def scheduled_title_refresh() -> None:
    with SessionLocal() as db:
        if "anidb" not in enabled_scraper_names(db):
            return
        try:
            await SCRAPERS["anidb"].refresh_titles(db)
        except AppError:
            pass


async def scheduled_metadata_refresh() -> None:
    with SessionLocal() as db:
        enabled = db.get(AppSetting, "scheduled_refresh")
        if not enabled or not enabled.value:
            return
        enabled_sources = enabled_scraper_names(db)
        anime_ids = db.scalars(select(Anime.id)).all()
        for anime_id in anime_ids:
            anime = db.scalar(_anime_query().where(Anime.id == anime_id))
            if anime:
                await refresh_anime(db, anime, enabled_sources)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    Base.metadata.create_all(engine)
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    with SessionLocal() as db:
        db.query(TaskRecord).filter(TaskRecord.status.in_(["pending", "running"])).update(
            {
                "status": "failed",
                "message": "应用重启导致任务中断",
                "error": {
                    "code": "TASK_INTERRUPTED",
                    "message": "应用重启导致任务中断，可安全重试",
                    "retryable": True,
                },
            },
            synchronize_session=False,
        )
        db.query(ScrapeCandidate).filter(ScrapeCandidate.is_mock.is_(True)).delete(
            synchronize_session=False
        )
        db.query(SourceMapping).filter(SourceMapping.is_mock.is_(True)).delete(
            synchronize_session=False
        )
        db.query(AppSetting).filter(AppSetting.key == "demo_scrapers").delete(
            synchronize_session=False
        )
        migrate_pending_folder_search_keywords(db)
        db.commit()
    if not scheduler.running:
        scheduler.add_job(
            scheduled_title_refresh,
            "interval",
            days=1,
            id="anidb-title-refresh",
            replace_existing=True,
        )
        scheduler.add_job(
            scheduled_metadata_refresh,
            "interval",
            hours=6,
            id="metadata-refresh",
            replace_existing=True,
        )
        scheduler.start()
    yield
    if scheduler.running:
        scheduler.shutdown(wait=False)


app = FastAPI(title="Anime Manager API", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin, "http://localhost:5173"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


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
async def validation_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "code": "VALIDATION_ERROR",
            "message": "请求参数校验失败",
            "details": exc.errors(),
            "retryable": False,
        },
    )


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/dashboard")
def dashboard(db: Session = Depends(get_db)) -> dict[str, int]:
    return {
        "files": db.scalar(select(func.count(MediaFile.id))) or 0,
        "anime": db.scalar(select(func.count(Anime.id))) or 0,
        "pending": db.scalar(
            select(func.count(MatchGroup.id)).where(
                MatchGroup.status == "pending",
                MatchGroup.files.any(),
            )
        )
        or 0,
        "missing": db.scalar(
            select(func.count(MediaFile.id)).where(MediaFile.status == "missing")
        )
        or 0,
        "running_tasks": db.scalar(
            select(func.count(TaskRecord.id)).where(TaskRecord.status.in_(["pending", "running"]))
        )
        or 0,
    }


@app.get("/api/library-roots", response_model=list[LibraryRootOut])
def list_roots(db: Session = Depends(get_db)):
    return db.scalars(select(LibraryRoot).order_by(LibraryRoot.id)).all()


@app.post("/api/library-roots", response_model=LibraryRootOut, status_code=201)
def create_root(payload: LibraryRootCreate, db: Session = Depends(get_db)):
    path = Path(payload.path).expanduser().resolve(strict=True)
    if not path.is_dir():
        raise AppError("INVALID_LIBRARY_ROOT", "媒体库路径不是目录")
    existing = db.scalar(select(LibraryRoot).where(LibraryRoot.path == str(path)))
    if existing:
        return existing
    root = LibraryRoot(path=str(path), enabled=payload.enabled)
    db.add(root)
    db.commit()
    db.refresh(root)
    return root


@app.patch("/api/library-roots/{root_id}", response_model=LibraryRootOut)
def patch_root(root_id: int, payload: LibraryRootPatch, db: Session = Depends(get_db)):
    root = db.get(LibraryRoot, root_id)
    if not root:
        raise AppError("NOT_FOUND", "媒体库不存在", status_code=404)
    if payload.path is not None:
        path = Path(payload.path).expanduser().resolve(strict=True)
        if not path.is_dir():
            raise AppError("INVALID_LIBRARY_ROOT", "媒体库路径不是目录")
        root.path = str(path)
    if payload.enabled is not None:
        root.enabled = payload.enabled
    db.commit()
    db.refresh(root)
    return root


def _run_scan(root_id: int, task_id: int) -> None:
    with SessionLocal() as db:
        scan_library(db, root_id, task_id)


@app.post("/api/library-roots/{root_id}/scan", response_model=TaskOut, status_code=202)
def start_scan(root_id: int, background: BackgroundTasks, db: Session = Depends(get_db)):
    root = db.get(LibraryRoot, root_id)
    if not root:
        raise AppError("NOT_FOUND", "媒体库不存在", status_code=404)
    task = TaskRecord(kind="scan_library", message="等待扫描")
    db.add(task)
    db.commit()
    db.refresh(task)
    background.add_task(_run_scan, root.id, task.id)
    return task


@app.get("/api/tasks/{task_id}", response_model=TaskOut)
def get_task(task_id: int, db: Session = Depends(get_db)):
    task = db.get(TaskRecord, task_id)
    if not task:
        raise AppError("NOT_FOUND", "任务不存在", status_code=404)
    return task


@app.get("/api/tasks/{task_id}/events")
def task_events(task_id: int, db: Session = Depends(get_db)):
    if not db.get(TaskRecord, task_id):
        raise AppError("NOT_FOUND", "任务不存在", status_code=404)
    return StreamingResponse(
        _task_event_stream(task_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@app.patch("/api/media-files/{media_id}", response_model=dict)
def patch_media_file(media_id: int, payload: MediaFilePatch, db: Session = Depends(get_db)):
    media = db.get(MediaFile, media_id)
    if not media:
        raise AppError("NOT_FOUND", "媒体文件不存在", status_code=404)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(media, key, value)
    db.commit()
    return {"id": media.id, "episode": media.episode, "parsed_title": media.parsed_title}


def _resolve_media_stream_path(media: MediaFile) -> Path:
    if media.status != "present":
        raise AppError("MEDIA_UNAVAILABLE", "媒体文件当前不可用", status_code=404)
    try:
        library_root = Path(media.library_root.path).resolve(strict=True)
        media_path = Path(media.path).resolve(strict=True)
        media_path.relative_to(library_root)
    except (OSError, ValueError):
        raise AppError("MEDIA_UNAVAILABLE", "媒体文件不存在或不在媒体库内", status_code=404)
    if not media_path.is_file():
        raise AppError("MEDIA_UNAVAILABLE", "媒体文件不存在", status_code=404)
    return media_path


@app.get("/api/media-files/{media_id}/stream", response_class=FileResponse)
def stream_media_file(media_id: int, db: Session = Depends(get_db)):
    media = db.scalar(
        select(MediaFile)
        .options(selectinload(MediaFile.library_root))
        .where(MediaFile.id == media_id)
    )
    if not media:
        raise AppError("NOT_FOUND", "媒体文件不存在", status_code=404)
    path = _resolve_media_stream_path(media)
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(
        path,
        media_type=media_type,
        filename=path.name,
        content_disposition_type="inline",
    )


@app.get("/api/sources/getchu/{source_id}/cover")
async def getchu_cover(source_id: int, db: Session = Depends(get_db)):
    proxy = next(
        (
            row.value
            for row in [db.get(AppSetting, "proxy_url")]
            if row and isinstance(row.value, str) and row.value
        ),
        None,
    )
    url = f"https://www.getchu.com/brandnew/{source_id}/c{source_id}package.jpg"
    try:
        async with httpx.AsyncClient(
            proxy=proxy,
            timeout=30,
            follow_redirects=True,
            headers={
                "User-Agent": "AnimeManager/0.1 (local metadata client)",
                "Referer": "https://www.getchu.com/",
            },
            cookies={"getchu_adalt_flag": "getchu.com"},
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise AppError(
            "GETCHU_COVER_FAILED",
            "Getchu 封面获取失败",
            details=type(exc).__name__,
            retryable=True,
            status_code=502,
        ) from exc
    mime = response.headers.get("content-type", "").split(";")[0]
    if not mime.startswith("image/"):
        raise AppError("INVALID_ARTWORK", "Getchu 封面响应不是图片", status_code=502)
    return Response(
        content=response.content,
        media_type=mime,
        headers={"Cache-Control": "public, max-age=3600"},
    )


def _group_query():
    return select(MatchGroup).options(
        selectinload(MatchGroup.files), selectinload(MatchGroup.candidates)
    )


@app.get("/api/match-groups")
def list_groups(
    status: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    query = _group_query()
    count_query = select(func.count(MatchGroup.id))
    if status:
        query = query.where(MatchGroup.status == status)
        count_query = count_query.where(MatchGroup.status == status)
    if status == "pending":
        query = query.where(MatchGroup.files.any())
        count_query = count_query.where(MatchGroup.files.any())
    total = db.scalar(count_query) or 0
    items = db.scalars(
        query.order_by(MatchGroup.updated_at.desc()).offset((page - 1) * page_size).limit(page_size)
    ).all()
    return {
        "items": [MatchGroupOut.model_validate(item).model_dump(mode="json") for item in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


async def _run_bulk_search_confirm(
    task_id: int, sources: list[str], session_factory=None
) -> None:
    factory = session_factory or SessionLocal
    with factory() as db:
        task = db.get(TaskRecord, task_id)
        if not task:
            return
        try:
            enabled = enabled_scraper_names(db)
            groups = db.scalars(
                _group_query()
                .where(MatchGroup.status == "pending", MatchGroup.files.any())
                .order_by(MatchGroup.updated_at.desc())
            ).all()
            total = len(groups)
            items: list[dict[str, Any]] = []
            confirmed = 0
            task.status = "running"
            task.message = f"准备处理 0/{total}"
            task.result = {
                "searched": 0,
                "total": total,
                "confirmed": 0,
                "skipped": 0,
                "failed": 0,
                "items": [],
            }
            db.commit()
            await _publish_task_event(task)

            for index, group in enumerate(groups, start=1):
                task.message = f"正在匹配 {index}/{total}：{group.display_title}"
                task.progress = (index - 1) / max(total, 1)
                db.commit()
                await _publish_task_event(task)
                try:
                    candidates, search_errors = await search_group(
                        db, group, group.search_keyword, sources
                    )
                    exact_by_source: dict[str, ScrapeCandidate] = {}
                    for candidate in candidates:
                        if candidate.score >= 1:
                            current = exact_by_source.get(candidate.source)
                            if current is None or candidate.score > current.score:
                                exact_by_source[candidate.source] = candidate
                    if not exact_by_source:
                        items.append(
                            {
                                "group_id": group.id,
                                "title": group.display_title,
                                "status": "skipped",
                                "reason": "没有 100% 匹配候选",
                                "errors": search_errors,
                            }
                        )
                    else:
                        save_selections(
                            db,
                            group,
                            {
                                source: (
                                    exact_by_source[source].id
                                    if source in exact_by_source
                                    else None
                                )
                                for source in enabled
                            },
                        )
                        anime = await confirm_group(db, group, enabled)
                        confirmed += 1
                        items.append(
                            {
                                "group_id": group.id,
                                "title": group.display_title,
                                "status": "confirmed",
                                "anime_id": anime.id,
                                "anime_title": anime.title,
                                "sources": sorted(exact_by_source),
                                "errors": search_errors,
                            }
                        )
                except AppError as exc:
                    db.rollback()
                    items.append(
                        {
                            "group_id": group.id,
                            "title": group.display_title,
                            "status": "failed",
                            "reason": exc.message,
                            "code": exc.code,
                        }
                    )

                task = db.get(TaskRecord, task_id)
                task.progress = index / max(total, 1)
                task.message = f"已处理 {index}/{total}"
                task.result = {
                    "searched": index,
                    "total": total,
                    "confirmed": confirmed,
                    "skipped": sum(item["status"] == "skipped" for item in items),
                    "failed": sum(item["status"] == "failed" for item in items),
                    "items": items,
                }
                db.commit()
                await _publish_task_event(task)

            task.status = "completed"
            task.progress = 1
            task.message = f"批量匹配完成，共处理 {total} 个"
            db.commit()
            await _publish_task_event(task)
        except Exception as exc:
            db.rollback()
            task = db.get(TaskRecord, task_id)
            if task:
                task.status = "failed"
                task.message = "批量匹配失败"
                task.error = {
                    "code": getattr(exc, "code", "BULK_MATCH_FAILED"),
                    "message": str(exc),
                    "retryable": True,
                }
                db.commit()
                await _publish_task_event(task)


@app.post(
    "/api/match-groups/bulk-search-confirm",
    response_model=TaskOut,
    status_code=202,
)
def start_bulk_search_confirm(
    payload: BulkSearchConfirmRequest,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
):
    enabled = enabled_scraper_names(db)
    requested = payload.sources if payload.sources is not None else enabled
    sources = [source for source in requested if source in enabled]
    if not sources:
        raise AppError("NO_ENABLED_SOURCE", "没有可用于批量搜索的数据源")
    running = db.scalar(
        select(TaskRecord)
        .where(
            TaskRecord.kind == "bulk_search_confirm",
            TaskRecord.status.in_(["pending", "running"]),
        )
        .order_by(TaskRecord.id.desc())
    )
    if running:
        return running
    task = TaskRecord(
        kind="bulk_search_confirm",
        message="等待批量匹配",
        result={"searched": 0, "confirmed": 0, "skipped": 0, "failed": 0},
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    background.add_task(_run_bulk_search_confirm, task.id, sources)
    return task


@app.get("/api/match-groups/{group_id}", response_model=MatchGroupOut)
def get_group(group_id: int, db: Session = Depends(get_db)):
    group = db.scalar(_group_query().where(MatchGroup.id == group_id))
    if not group:
        raise AppError("NOT_FOUND", "匹配分组不存在", status_code=404)
    return group


@app.patch("/api/match-groups/{group_id}", response_model=MatchGroupOut)
def patch_group(group_id: int, payload: MatchGroupPatch, db: Session = Depends(get_db)):
    group = db.scalar(_group_query().where(MatchGroup.id == group_id))
    if not group:
        raise AppError("NOT_FOUND", "匹配分组不存在", status_code=404)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(group, key, value)
    db.commit()
    db.refresh(group)
    return group


@app.post("/api/match-groups/{group_id}/search")
async def run_search(group_id: int, payload: SearchRequest, db: Session = Depends(get_db)):
    group = db.get(MatchGroup, group_id)
    if not group:
        raise AppError("NOT_FOUND", "匹配分组不存在", status_code=404)
    enabled = enabled_scraper_names(db)
    requested = payload.sources if payload.sources is not None else enabled
    sources = [source for source in requested if source in enabled]
    results, errors = await search_group(db, group, payload.keyword or group.search_keyword, sources)
    errors.extend(
        {
            "source": source,
            "code": "UNKNOWN_SOURCE",
            "message": "未知数据源",
        }
        for source in requested
        if source not in SCRAPERS
    )
    errors.extend(
        {
            "source": source,
            "code": "SOURCE_DISABLED",
            "message": "该数据源已在设置中停用",
        }
        for source in requested
        if source in SCRAPERS and source not in enabled
    )
    return {
        "items": [
            {
                "id": item.id,
                "source": item.source,
                "source_id": item.source_id,
                "title": item.title,
                "year": item.year,
                "episode_count": item.episode_count,
                "cover_url": item.cover_url,
                "score": item.score,
                "selected": item.selected,
                "is_mock": item.is_mock,
            }
            for item in results
        ],
        "errors": errors,
    }


@app.put("/api/match-groups/{group_id}/selections", response_model=MatchGroupOut)
def update_selections(group_id: int, payload: SelectionRequest, db: Session = Depends(get_db)):
    group = db.get(MatchGroup, group_id)
    if not group:
        raise AppError("NOT_FOUND", "匹配分组不存在", status_code=404)
    save_selections(db, group, payload.selections)
    return db.scalar(_group_query().where(MatchGroup.id == group_id))


@app.post("/api/match-groups/{group_id}/confirm", response_model=AnimeOut)
async def confirm(group_id: int, db: Session = Depends(get_db)):
    group = db.scalar(
        select(MatchGroup)
        .options(selectinload(MatchGroup.files))
        .where(MatchGroup.id == group_id)
    )
    if not group:
        raise AppError("NOT_FOUND", "匹配分组不存在", status_code=404)
    anime = await confirm_group(db, group, enabled_scraper_names(db))
    return db.scalar(
        select(Anime)
        .options(
            selectinload(Anime.files).selectinload(MediaFile.library_root),
            selectinload(Anime.mappings),
        )
        .where(Anime.id == anime.id)
    )


@app.post("/api/match-groups/{group_id}/bind-existing", response_model=AnimeOut)
def bind_existing(group_id: int, payload: BindExistingRequest, db: Session = Depends(get_db)):
    group = db.scalar(_group_query().where(MatchGroup.id == group_id))
    if not group:
        raise AppError("NOT_FOUND", "匹配分组不存在", status_code=404)
    anime = db.get(Anime, payload.anime_id)
    if not anime:
        raise AppError("NOT_FOUND", "目标作品不存在", status_code=404)
    bind_group_to_anime(db, group, anime)
    return db.scalar(_anime_query().where(Anime.id == anime.id))


def _anime_query():
    return select(Anime).options(
        selectinload(Anime.files).selectinload(MediaFile.library_root),
        selectinload(Anime.mappings),
    )


@app.get("/api/anime")
def list_anime(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    total = db.scalar(select(func.count(Anime.id))) or 0
    items = db.scalars(
        _anime_query().order_by(Anime.updated_at.desc()).offset((page - 1) * page_size).limit(page_size)
    ).all()
    return {
        "items": [AnimeOut.model_validate(item).model_dump(mode="json") for item in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.get("/api/anime/{anime_id}", response_model=AnimeOut)
def get_anime(anime_id: int, db: Session = Depends(get_db)):
    anime = db.scalar(_anime_query().where(Anime.id == anime_id))
    if not anime:
        raise AppError("NOT_FOUND", "作品不存在", status_code=404)
    return anime


@app.patch("/api/anime/{anime_id}", response_model=AnimeOut)
def patch_anime(anime_id: int, payload: AnimePatch, db: Session = Depends(get_db)):
    anime = db.scalar(_anime_query().where(Anime.id == anime_id))
    if not anime:
        raise AppError("NOT_FOUND", "作品不存在", status_code=404)
    manual = set(anime.manual_fields or [])
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(anime, key, value)
        manual.add(key)
    anime.manual_fields = sorted(manual)
    provenance = dict(anime.field_provenance or {})
    for key in manual:
        provenance[key] = "manual"
    anime.field_provenance = provenance
    db.commit()
    db.refresh(anime)
    return anime


@app.post("/api/anime/{anime_id}/refresh", response_model=AnimeOut)
async def refresh(anime_id: int, db: Session = Depends(get_db)):
    anime = db.scalar(_anime_query().where(Anime.id == anime_id))
    if not anime:
        raise AppError("NOT_FOUND", "作品不存在", status_code=404)
    return await refresh_anime(db, anime, enabled_scraper_names(db))


@app.post("/api/anime/{anime_id}/rename-preview")
def rename_preview(anime_id: int, payload: RenameRequest, db: Session = Depends(get_db)):
    anime = db.scalar(_anime_query().where(Anime.id == anime_id))
    if not anime:
        raise AppError("NOT_FOUND", "作品不存在", status_code=404)
    return build_rename_plan(anime, payload.season)


@app.post("/api/anime/{anime_id}/rename")
def rename_files(anime_id: int, payload: RenameRequest, db: Session = Depends(get_db)):
    anime = db.scalar(_anime_query().where(Anime.id == anime_id))
    if not anime:
        raise AppError("NOT_FOUND", "作品不存在", status_code=404)
    return execute_rename_plan(db, anime, payload.season)


@app.post("/api/anime/rename-preview")
def rename_all_preview(payload: RenameRequest, db: Session = Depends(get_db)):
    animes = db.scalars(_anime_query().order_by(Anime.title)).unique().all()
    return build_bulk_rename_plan(list(animes), payload.season)


@app.post("/api/anime/rename")
def rename_all_files(payload: RenameRequest, db: Session = Depends(get_db)):
    animes = db.scalars(_anime_query().order_by(Anime.title)).unique().all()
    return execute_bulk_rename_plan(db, list(animes), payload.season)


@app.post("/api/anime/artifacts-preview")
def bulk_artifacts_preview(db: Session = Depends(get_db)):
    animes = db.scalars(_anime_query().order_by(Anime.title)).unique().all()
    return build_bulk_artifact_plan(list(animes))


def _run_bulk_artifacts(task_id: int) -> None:
    with SessionLocal() as db:
        task = db.get(TaskRecord, task_id)
        if not task:
            return
        animes = db.scalars(_anime_query().order_by(Anime.title)).unique().all()
        execute_bulk_artifact_plan(db, list(animes), task)


@app.post("/api/anime/artifacts", response_model=TaskOut, status_code=202)
def start_bulk_artifacts(
    background: BackgroundTasks,
    db: Session = Depends(get_db),
):
    task = TaskRecord(kind="bulk_artifacts", message="等待批量写入")
    db.add(task)
    db.commit()
    db.refresh(task)
    background.add_task(_run_bulk_artifacts, task.id)
    return task


@app.get("/api/anime/{anime_id}/export-preview")
def export_preview(anime_id: int, db: Session = Depends(get_db)):
    anime = db.scalar(_anime_query().where(Anime.id == anime_id))
    if not anime:
        raise AppError("NOT_FOUND", "作品不存在", status_code=404)
    return public_export_plan(build_export_plan(anime))


@app.post("/api/anime/{anime_id}/export")
async def run_export(anime_id: int, payload: ExportRequest, db: Session = Depends(get_db)):
    anime = db.scalar(_anime_query().where(Anime.id == anime_id))
    if not anime:
        raise AppError("NOT_FOUND", "作品不存在", status_code=404)
    return await export_anime(db, anime, payload.overwrite)


DEFAULT_SETTINGS: dict[str, Any] = {
    "enabled_scrapers": list(SCRAPER_NAMES),
    "anidb_client": "",
    "anidb_clientver": 1,
    "dmm_api_id": "",
    "dmm_affiliate_id": "",
    "proxy_url": "",
    "request_interval_seconds": 2.1,
    "scheduled_refresh": False,
}


@app.get("/api/settings")
def get_settings(db: Session = Depends(get_db)):
    result = dict(DEFAULT_SETTINGS)
    for row in db.scalars(select(AppSetting)).all():
        result[row.key] = row.value
    return result


@app.patch("/api/settings")
def patch_settings(payload: SettingsPatch, db: Session = Depends(get_db)):
    for key, value in payload.model_dump(exclude_unset=True).items():
        row = db.get(AppSetting, key)
        if row:
            row.value = value
        else:
            db.add(AppSetting(key=key, value=value))
    db.commit()
    return get_settings(db)


@app.post("/api/sources/anidb/titles/refresh")
async def refresh_titles(db: Session = Depends(get_db)):
    if "anidb" not in enabled_scraper_names(db):
        raise AppError("SOURCE_DISABLED", "AniDB 爬虫已在设置中停用", status_code=409)
    scraper = SCRAPERS["anidb"]
    assert isinstance(scraper, AniDBScraper)
    return await scraper.refresh_titles(db)
