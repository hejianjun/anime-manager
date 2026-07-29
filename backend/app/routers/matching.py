from fastapi import APIRouter, BackgroundTasks, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..database import get_db
from ..errors import AppError
from ..matching import confirm_group, save_selections, search_group
from ..media_ops import bind_group_to_anime
from ..models import Anime, MatchGroup, MediaFile, TaskRecord
from ..queries import anime_query, group_query
from ..schemas import (
    AnimeOut,
    BindExistingRequest,
    BulkSearchConfirmRequest,
    MatchGroupOut,
    MatchGroupPatch,
    SearchRequest,
    SelectionRequest,
    TaskOut,
)
from ..scrapers import SCRAPERS
from ..services.bulk_matching import run_bulk_search_confirm
from ..source_settings import enabled_scraper_names

router = APIRouter(prefix="/api/match-groups", tags=["matching"])


@router.get("")
def list_groups(
    status: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    query = group_query()
    count_query = select(func.count(MatchGroup.id))
    if status:
        query = query.where(MatchGroup.status == status)
        count_query = count_query.where(MatchGroup.status == status)
    if status == "pending":
        query = query.where(MatchGroup.files.any())
        count_query = count_query.where(MatchGroup.files.any())
    total = db.scalar(count_query) or 0
    items = db.scalars(
        query.order_by(MatchGroup.updated_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return {
        "items": [
            MatchGroupOut.model_validate(item).model_dump(mode="json")
            for item in items
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.post(
    "/bulk-search-confirm",
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
    background.add_task(run_bulk_search_confirm, task.id, sources)
    return task


@router.get("/{group_id}", response_model=MatchGroupOut)
def get_group(group_id: int, db: Session = Depends(get_db)):
    group = db.scalar(group_query().where(MatchGroup.id == group_id))
    if not group:
        raise AppError("NOT_FOUND", "匹配分组不存在", status_code=404)
    return group


@router.patch("/{group_id}", response_model=MatchGroupOut)
def patch_group(
    group_id: int,
    payload: MatchGroupPatch,
    db: Session = Depends(get_db),
):
    group = db.scalar(group_query().where(MatchGroup.id == group_id))
    if not group:
        raise AppError("NOT_FOUND", "匹配分组不存在", status_code=404)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(group, key, value)
    db.commit()
    db.refresh(group)
    return group


@router.post("/{group_id}/search")
async def run_search(
    group_id: int,
    payload: SearchRequest,
    db: Session = Depends(get_db),
):
    group = db.get(MatchGroup, group_id)
    if not group:
        raise AppError("NOT_FOUND", "匹配分组不存在", status_code=404)
    enabled = enabled_scraper_names(db)
    requested = payload.sources if payload.sources is not None else enabled
    sources = [source for source in requested if source in enabled]
    results, errors = await search_group(
        db,
        group,
        payload.keyword or group.search_keyword,
        sources,
    )
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


@router.put("/{group_id}/selections", response_model=MatchGroupOut)
def update_selections(
    group_id: int,
    payload: SelectionRequest,
    db: Session = Depends(get_db),
):
    group = db.get(MatchGroup, group_id)
    if not group:
        raise AppError("NOT_FOUND", "匹配分组不存在", status_code=404)
    save_selections(db, group, payload.selections)
    return db.scalar(group_query().where(MatchGroup.id == group_id))


@router.post("/{group_id}/confirm", response_model=AnimeOut)
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


@router.post("/{group_id}/bind-existing", response_model=AnimeOut)
def bind_existing(
    group_id: int,
    payload: BindExistingRequest,
    db: Session = Depends(get_db),
):
    group = db.scalar(group_query().where(MatchGroup.id == group_id))
    if not group:
        raise AppError("NOT_FOUND", "匹配分组不存在", status_code=404)
    anime = db.get(Anime, payload.anime_id)
    if not anime:
        raise AppError("NOT_FOUND", "目标作品不存在", status_code=404)
    bind_group_to_anime(db, group, anime)
    return db.scalar(anime_query().where(Anime.id == anime.id))
