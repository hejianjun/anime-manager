from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from ..models import AppSetting, SourceSnapshot

# Getchu product metadata changes rarely. Keep successful detail responses long
# enough for repeated batch sessions to avoid unnecessary site requests.
SOURCE_CACHE_AGE = timedelta(days=30)


@dataclass
class Candidate:
    source: str
    source_id: str
    title: str
    score: float
    year: int | None = None
    episode_count: int | None = None
    cover_url: str | None = None
    is_mock: bool = False
    payload: dict[str, Any] | None = None


@dataclass
class SourceMetadata:
    source: str
    source_id: str
    title: str
    original_title: str | None = None
    description: str | None = None
    year: int | None = None
    media_type: str | None = None
    episode_count: int | None = None
    studio: str | None = None
    cover_url: str | None = None
    episode_titles: dict[str, str] | None = None
    genres: list[str] | None = None
    tags: list[str] | None = None
    cast: list[dict[str, Any]] | None = None
    staff: list[dict[str, Any]] | None = None
    is_mock: bool = False


class Scraper(ABC):
    source: str

    @abstractmethod
    async def search(self, db: Session, keyword: str) -> list[Candidate]:
        raise NotImplementedError

    @abstractmethod
    async def detail(self, db: Session, source_id: str) -> SourceMetadata:
        raise NotImplementedError

    async def health(self, db: Session) -> dict[str, Any]:
        return {"source": self.source, "status": "ok"}


def get_setting(db: Session, key: str, default: Any = None) -> Any:
    row = db.get(AppSetting, key)
    return row.value if row else default


def cached_source_metadata(
    db: Session, source: str, source_id: str
) -> tuple[SourceSnapshot | None, SourceMetadata | None]:
    cached = db.scalar(
        select(SourceSnapshot).where(
            SourceSnapshot.source == source,
            SourceSnapshot.source_id == source_id,
        )
    )
    if not cached:
        return None, None
    fetched_at = cached.fetched_at
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - fetched_at < SOURCE_CACHE_AGE:
        return cached, SourceMetadata(**cached.normalized_payload)
    return cached, None


def store_source_metadata(
    db: Session,
    source: str,
    source_id: str,
    raw_payload: dict[str, Any],
    metadata: SourceMetadata,
    cached: SourceSnapshot | None,
) -> None:
    normalized = asdict(metadata)
    fetched_at = datetime.now(timezone.utc)
    if db.get_bind().dialect.name == "sqlite":
        statement = sqlite_insert(SourceSnapshot).values(
            source=source,
            source_id=source_id,
            raw_payload=raw_payload,
            normalized_payload=normalized,
            fetched_at=fetched_at,
        )
        statement = statement.on_conflict_do_update(
            index_elements=[SourceSnapshot.source, SourceSnapshot.source_id],
            set_={
                "raw_payload": raw_payload,
                "normalized_payload": normalized,
                "fetched_at": fetched_at,
            },
        )
        db.execute(statement)
        db.flush()
        return
    if cached:
        cached.raw_payload = raw_payload
        cached.normalized_payload = normalized
        cached.fetched_at = fetched_at
    else:
        db.add(
            SourceSnapshot(
                source=source,
                source_id=source_id,
                raw_payload=raw_payload,
                normalized_payload=normalized,
            )
        )
    db.flush()


def http_error_detail(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return type(exc).__name__
