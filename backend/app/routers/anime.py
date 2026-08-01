from fastapi import APIRouter, BackgroundTasks, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..bulk_artifacts import build_bulk_artifact_plan, execute_bulk_artifact_plan
from ..database import SessionLocal, get_db
from ..description_translation import (
    apply_description_translation,
    auto_translation_enabled,
    description_needs_translation,
    translate_description_text,
)
from ..errors import AppError
from ..exporter import build_export_plan, export_anime, public_export_plan
from ..matching import (
    fill_missing_description,
    refresh_anime,
    search_description_candidates,
)
from ..media_ops import build_rename_plan, execute_rename_plan, unbind_media_from_anime
from ..models import Anime, MediaFile, TaskRecord
from ..queries import anime_query
from ..schemas import (
    AnimeOut,
    AnimePatch,
    BulkRenameExecuteRequest,
    DescriptionCandidateOut,
    DescriptionFillRequest,
    ExportRequest,
    GetchuDescriptionCancelRequest,
    GetchuDescriptionPreviewRequest,
    GetchuDescriptionTaskRequest,
    RenameRequest,
    TaskOut,
)
from ..services.bulk_rename import (
    claim_preview_for_execution,
    run_bulk_rename_execute,
    run_bulk_rename_preview,
)
from ..services.getchu_descriptions import (
    PREVIEW_KIND,
    WRITE_KIND,
    build_initial_rows,
    cancel_preview_item,
    get_candidate_detail,
    run_getchu_description_preview,
    run_getchu_description_write,
    validate_preview_candidate,
)
from ..source_settings import enabled_scraper_names

router = APIRouter(prefix="/api/anime", tags=["anime"])


@router.get("")
def list_anime(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    total = db.scalar(select(func.count(Anime.id))) or 0
    items = db.scalars(
        anime_query()
        .order_by(Anime.updated_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return {
        "items": [
            AnimeOut.model_validate(item).model_dump(mode="json") for item in items
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/{anime_id}", response_model=AnimeOut)
def get_anime(anime_id: int, db: Session = Depends(get_db)):
    anime = db.scalar(anime_query().where(Anime.id == anime_id))
    if not anime:
        raise AppError("NOT_FOUND", "作品不存在", status_code=404)
    return anime


@router.patch("/{anime_id}", response_model=AnimeOut)
def patch_anime(
    anime_id: int,
    payload: AnimePatch,
    db: Session = Depends(get_db),
):
    anime = db.scalar(anime_query().where(Anime.id == anime_id))
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


@router.post("/getchu-description-preview", response_model=TaskOut, status_code=202)
def start_getchu_description_preview(
    payload: GetchuDescriptionPreviewRequest,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
):
    if "getchu" not in enabled_scraper_names(db):
        raise AppError("SOURCE_DISABLED", "Getchu 已在设置中停用", status_code=409)
    requested = list(dict.fromkeys(payload.anime_ids))
    found = db.scalars(anime_query().where(Anime.id.in_(requested))).unique().all()
    by_id = {anime.id: anime for anime in found}
    missing = [anime_id for anime_id in requested if anime_id not in by_id]
    if missing:
        raise AppError(
            "NOT_FOUND",
            "部分作品不存在",
            details={"anime_ids": missing},
            status_code=404,
        )
    animes = [by_id[anime_id] for anime_id in requested]
    rows = build_initial_rows(animes)
    task = TaskRecord(
        kind=PREVIEW_KIND,
        message="等待搜索 Getchu 简介",
        result={"processed": 0, "total": len(rows), "items": rows},
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    background.add_task(run_getchu_description_preview, task.id)
    return task


@router.post("/{anime_id}/getchu-description-detail")
async def getchu_description_detail(
    anime_id: int,
    payload: GetchuDescriptionTaskRequest,
):
    return await get_candidate_detail(
        payload.preview_task_id,
        anime_id,
        payload.source_id,
    )


@router.post("/{anime_id}/getchu-description-cancel")
async def cancel_getchu_description(
    anime_id: int,
    payload: GetchuDescriptionCancelRequest,
):
    return await cancel_preview_item(payload.preview_task_id, anime_id)


@router.post(
    "/{anime_id}/getchu-description-confirm",
    response_model=TaskOut,
    status_code=202,
)
def confirm_getchu_description(
    anime_id: int,
    payload: GetchuDescriptionTaskRequest,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
):
    preview = db.get(TaskRecord, payload.preview_task_id)
    if not preview:
        raise AppError("NOT_FOUND", "Getchu 简介预览不存在", status_code=404)
    validate_preview_candidate(preview, anime_id, payload.source_id)
    children = db.scalars(
        select(TaskRecord)
        .where(
            TaskRecord.parent_task_id == preview.id,
            TaskRecord.kind == WRITE_KIND,
        )
        .order_by(TaskRecord.id.desc())
    ).all()
    existing = next(
        (
            task
            for task in children
            if (task.result or {}).get("anime_id") == anime_id
            and task.status in {"pending", "running", "completed"}
        ),
        None,
    )
    if existing:
        return existing
    task = TaskRecord(
        parent_task_id=preview.id,
        kind=WRITE_KIND,
        message="等待写入 Getchu 简介和 NFO",
        result={
            "preview_task_id": preview.id,
            "anime_id": anime_id,
            "source_id": payload.source_id,
        },
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    background.add_task(run_getchu_description_write, task.id)
    return task


@router.post("/{anime_id}/refresh", response_model=AnimeOut)
async def refresh(anime_id: int, db: Session = Depends(get_db)):
    anime = db.scalar(anime_query().where(Anime.id == anime_id))
    if not anime:
        raise AppError("NOT_FOUND", "作品不存在", status_code=404)
    return await refresh_anime(db, anime, enabled_scraper_names(db))


@router.post("/{anime_id}/description-candidates")
async def description_candidates(
    anime_id: int,
    keyword: str | None = Query(default=None, min_length=1, max_length=500),
    db: Session = Depends(get_db),
):
    anime = db.scalar(anime_query().where(Anime.id == anime_id))
    if not anime:
        raise AppError("NOT_FOUND", "作品不存在", status_code=404)
    if (anime.description or "").strip():
        raise AppError("DESCRIPTION_EXISTS", "当前作品已有简介", status_code=409)
    results, errors = await search_description_candidates(
        db,
        anime,
        (keyword or anime.original_title or anime.title).strip(),
        enabled_scraper_names(db),
    )
    return {
        "items": [
            DescriptionCandidateOut(
                source=item.source,
                source_id=item.source_id,
                title=item.title,
                year=item.year,
                cover_url=item.cover_url,
                score=item.score,
            ).model_dump(mode="json")
            for item in results
        ],
        "errors": errors,
    }


@router.post("/{anime_id}/fill-description", response_model=AnimeOut)
async def fill_description(
    anime_id: int,
    payload: DescriptionFillRequest,
    db: Session = Depends(get_db),
):
    anime = db.scalar(anime_query().where(Anime.id == anime_id))
    if not anime:
        raise AppError("NOT_FOUND", "作品不存在", status_code=404)
    return await fill_missing_description(
        db,
        anime,
        payload.source,
        payload.source_id,
        enabled_scraper_names(db),
    )


@router.post("/{anime_id}/translate-description", response_model=AnimeOut)
async def translate_description(anime_id: int, db: Session = Depends(get_db)):
    anime = db.scalar(anime_query().where(Anime.id == anime_id))
    if not anime:
        raise AppError("NOT_FOUND", "作品不存在", status_code=404)
    source = (anime.description or "").strip()
    if not source:
        raise AppError("DESCRIPTION_EMPTY", "当前作品没有可翻译的简介")

    translated = await translate_description_text(db, source)
    apply_description_translation(anime, translated)
    db.commit()
    db.refresh(anime)
    return anime


@router.delete("/{anime_id}/media-files/{media_id}", response_model=dict)
def remove_media_file(
    anime_id: int,
    media_id: int,
    db: Session = Depends(get_db),
):
    anime = db.get(Anime, anime_id)
    if not anime:
        raise AppError("NOT_FOUND", "作品不存在", status_code=404)
    media = db.scalar(
        select(MediaFile)
        .options(selectinload(MediaFile.library_root))
        .where(MediaFile.id == media_id)
    )
    if not media:
        raise AppError("NOT_FOUND", "媒体文件不存在", status_code=404)
    unbind_media_from_anime(db, anime, media)
    return {
        "id": media.id,
        "anime_id": None,
        "match_group_id": media.match_group_id,
        "physical_file_deleted": False,
    }


@router.post("/{anime_id}/rename-preview")
def rename_preview(
    anime_id: int,
    payload: RenameRequest,
    db: Session = Depends(get_db),
):
    anime = db.scalar(anime_query().where(Anime.id == anime_id))
    if not anime:
        raise AppError("NOT_FOUND", "作品不存在", status_code=404)
    return build_rename_plan(anime, payload.season)


@router.post("/{anime_id}/rename")
def rename_files(
    anime_id: int,
    payload: RenameRequest,
    db: Session = Depends(get_db),
):
    anime = db.scalar(anime_query().where(Anime.id == anime_id))
    if not anime:
        raise AppError("NOT_FOUND", "作品不存在", status_code=404)
    return execute_rename_plan(db, anime, payload.season)


@router.post("/rename-preview", response_model=TaskOut, status_code=202)
def rename_all_preview(
    payload: RenameRequest,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
):
    task = TaskRecord(
        kind="bulk_rename_preview",
        message="等待生成重命名预览",
        result={"season": payload.season},
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    background.add_task(run_bulk_rename_preview, task.id, payload.season)
    return task


@router.post("/rename", response_model=TaskOut, status_code=202)
def rename_all_files(
    payload: BulkRenameExecuteRequest,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
):
    task = TaskRecord(
        kind="bulk_rename_execute",
        message="等待执行重命名",
        result={"preview_task_id": payload.preview_task_id},
    )
    task = claim_preview_for_execution(db, payload.preview_task_id, task)
    background.add_task(run_bulk_rename_execute, task.id, payload.preview_task_id)
    return task


@router.post("/artifacts-preview")
def bulk_artifacts_preview(db: Session = Depends(get_db)):
    animes = db.scalars(anime_query().order_by(Anime.title)).unique().all()
    plan = build_bulk_artifact_plan(list(animes))
    candidates = {
        anime.id
        for anime in animes
        if anime.id in plan["_nfo_anime_ids"] and description_needs_translation(anime)
    }
    plan["auto_translate_description"] = auto_translation_enabled(db)
    plan["translation_candidate_count"] = len(candidates)
    return plan


async def run_bulk_artifacts(task_id: int) -> None:
    with SessionLocal() as db:
        task = db.get(TaskRecord, task_id)
        if not task:
            return
        animes = db.scalars(anime_query().order_by(Anime.title)).unique().all()
        await execute_bulk_artifact_plan(db, list(animes), task)


@router.post("/artifacts", response_model=TaskOut, status_code=202)
def start_bulk_artifacts(
    background: BackgroundTasks,
    db: Session = Depends(get_db),
):
    task = TaskRecord(kind="bulk_artifacts", message="等待批量写入")
    db.add(task)
    db.commit()
    db.refresh(task)
    background.add_task(run_bulk_artifacts, task.id)
    return task


@router.get("/{anime_id}/export-preview")
def export_preview(anime_id: int, db: Session = Depends(get_db)):
    anime = db.scalar(anime_query().where(Anime.id == anime_id))
    if not anime:
        raise AppError("NOT_FOUND", "作品不存在", status_code=404)
    return public_export_plan(build_export_plan(anime))


@router.post("/{anime_id}/export")
async def run_export(
    anime_id: int,
    payload: ExportRequest,
    db: Session = Depends(get_db),
):
    anime = db.scalar(anime_query().where(Anime.id == anime_id))
    if not anime:
        raise AppError("NOT_FOUND", "作品不存在", status_code=404)
    return await export_anime(db, anime, payload.overwrite)
