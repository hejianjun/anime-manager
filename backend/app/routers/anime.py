from fastapi import APIRouter, BackgroundTasks, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..bulk_artifacts import build_bulk_artifact_plan, execute_bulk_artifact_plan
from ..database import SessionLocal, get_db
from ..errors import AppError
from ..exporter import build_export_plan, export_anime, public_export_plan
from ..matching import refresh_anime
from ..media_ops import (
    build_bulk_rename_plan,
    build_rename_plan,
    execute_bulk_rename_plan,
    execute_rename_plan,
)
from ..models import Anime, TaskRecord
from ..queries import anime_query
from ..schemas import AnimeOut, AnimePatch, ExportRequest, RenameRequest, TaskOut
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


@router.post("/{anime_id}/refresh", response_model=AnimeOut)
async def refresh(anime_id: int, db: Session = Depends(get_db)):
    anime = db.scalar(anime_query().where(Anime.id == anime_id))
    if not anime:
        raise AppError("NOT_FOUND", "作品不存在", status_code=404)
    return await refresh_anime(db, anime, enabled_scraper_names(db))


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


@router.post("/rename-preview")
def rename_all_preview(
    payload: RenameRequest,
    db: Session = Depends(get_db),
):
    animes = db.scalars(anime_query().order_by(Anime.title)).unique().all()
    return build_bulk_rename_plan(list(animes), payload.season)


@router.post("/rename")
def rename_all_files(
    payload: RenameRequest,
    db: Session = Depends(get_db),
):
    animes = db.scalars(anime_query().order_by(Anime.title)).unique().all()
    return execute_bulk_rename_plan(db, list(animes), payload.season)


@router.post("/artifacts-preview")
def bulk_artifacts_preview(db: Session = Depends(get_db)):
    animes = db.scalars(anime_query().order_by(Anime.title)).unique().all()
    return build_bulk_artifact_plan(list(animes))


def run_bulk_artifacts(task_id: int) -> None:
    with SessionLocal() as db:
        task = db.get(TaskRecord, task_id)
        if not task:
            return
        animes = db.scalars(anime_query().order_by(Anime.title)).unique().all()
        execute_bulk_artifact_plan(db, list(animes), task)


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
