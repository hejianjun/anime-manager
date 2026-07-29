from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from sqlalchemy import select

from .config import settings
from .database import Base, SessionLocal, engine
from .errors import AppError
from .matching import refresh_anime
from .models import Anime, AppSetting, ScrapeCandidate, SourceMapping, TaskRecord
from .queries import anime_query
from .scanner import migrate_pending_folder_search_keywords
from .scrapers import SCRAPERS
from .source_settings import enabled_scraper_names

scheduler = AsyncIOScheduler(timezone="UTC")


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
            anime = db.scalar(anime_query().where(Anime.id == anime_id))
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
