from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class LibraryRoot(Base):
    __tablename__ = "library_root"
    id: Mapped[int] = mapped_column(primary_key=True)
    path: Mapped[str] = mapped_column(String(2048), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_scan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    files: Mapped[list[MediaFile]] = relationship(back_populates="library_root")


class Anime(Base):
    __tablename__ = "anime"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(500))
    original_title: Mapped[str | None] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text)
    year: Mapped[int | None] = mapped_column(Integer)
    media_type: Mapped[str | None] = mapped_column(String(80))
    episode_count: Mapped[int | None] = mapped_column(Integer)
    studio: Mapped[str | None] = mapped_column(String(500))
    cover_url: Mapped[str | None] = mapped_column(String(2048))
    episode_titles: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    genres: Mapped[list[str]] = mapped_column(JSON, default=list)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    cast: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    staff: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    field_provenance: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    manual_fields: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    files: Mapped[list[MediaFile]] = relationship(back_populates="anime")
    mappings: Mapped[list[SourceMapping]] = relationship(
        back_populates="anime", cascade="all, delete-orphan"
    )


class MatchGroup(Base):
    __tablename__ = "match_group"
    __table_args__ = (UniqueConstraint("library_root_id", "group_key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    library_root_id: Mapped[int] = mapped_column(ForeignKey("library_root.id"))
    group_key: Mapped[str] = mapped_column(String(2048))
    display_title: Mapped[str] = mapped_column(String(500))
    search_keyword: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(40), default="pending")
    anime_id: Mapped[int | None] = mapped_column(ForeignKey("anime.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    files: Mapped[list[MediaFile]] = relationship(back_populates="match_group")
    candidates: Mapped[list[ScrapeCandidate]] = relationship(
        back_populates="match_group", cascade="all, delete-orphan"
    )


class MediaFile(Base):
    __tablename__ = "media_file"
    __table_args__ = (UniqueConstraint("library_root_id", "path"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    library_root_id: Mapped[int] = mapped_column(ForeignKey("library_root.id"))
    path: Mapped[str] = mapped_column(String(2048))
    relative_path: Mapped[str] = mapped_column(String(2048))
    size: Mapped[int] = mapped_column(Integer)
    modified_ns: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    hash_algorithm: Mapped[str | None] = mapped_column(String(40))
    parsed_title: Mapped[str] = mapped_column(String(500))
    episode: Mapped[int | None] = mapped_column(Integer)
    duration: Mapped[float | None] = mapped_column(Float)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    video_codec: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(40), default="present")
    match_group_id: Mapped[int | None] = mapped_column(ForeignKey("match_group.id"))
    anime_id: Mapped[int | None] = mapped_column(ForeignKey("anime.id"))
    scanned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    library_root: Mapped[LibraryRoot] = relationship(back_populates="files")
    match_group: Mapped[MatchGroup | None] = relationship(back_populates="files")
    anime: Mapped[Anime | None] = relationship(back_populates="files")


class SourceMapping(Base):
    __tablename__ = "source_mapping"
    __table_args__ = (
        UniqueConstraint("anime_id", "source"),
        UniqueConstraint("source", "source_id"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    anime_id: Mapped[int] = mapped_column(ForeignKey("anime.id"))
    source: Mapped[str] = mapped_column(String(40))
    source_id: Mapped[str] = mapped_column(String(200))
    is_mock: Mapped[bool] = mapped_column(Boolean, default=False)
    anime: Mapped[Anime] = relationship(back_populates="mappings")


class ScrapeCandidate(Base):
    __tablename__ = "scrape_candidate"
    __table_args__ = (UniqueConstraint("match_group_id", "source", "source_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    match_group_id: Mapped[int] = mapped_column(ForeignKey("match_group.id"))
    source: Mapped[str] = mapped_column(String(40))
    source_id: Mapped[str] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(String(500))
    year: Mapped[int | None] = mapped_column(Integer)
    episode_count: Mapped[int | None] = mapped_column(Integer)
    cover_url: Mapped[str | None] = mapped_column(String(2048))
    score: Mapped[float] = mapped_column(Float)
    selected: Mapped[bool] = mapped_column(Boolean, default=False)
    is_mock: Mapped[bool] = mapped_column(Boolean, default=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    match_group: Mapped[MatchGroup] = relationship(back_populates="candidates")


class SourceSnapshot(Base):
    __tablename__ = "source_snapshot"
    __table_args__ = (UniqueConstraint("source", "source_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(40))
    source_id: Mapped[str] = mapped_column(String(200))
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    normalized_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ScrapeHistory(Base):
    __tablename__ = "scrape_history"
    id: Mapped[int] = mapped_column(primary_key=True)
    anime_id: Mapped[int | None] = mapped_column(ForeignKey("anime.id"))
    source: Mapped[str] = mapped_column(String(40))
    success: Mapped[bool] = mapped_column(Boolean)
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ExportHistory(Base):
    __tablename__ = "export_history"
    id: Mapped[int] = mapped_column(primary_key=True)
    anime_id: Mapped[int] = mapped_column(ForeignKey("anime.id"))
    success: Mapped[bool] = mapped_column(Boolean)
    files: Mapped[list[str]] = mapped_column(JSON, default=list)
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TaskRecord(Base):
    __tablename__ = "task_record"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(40), default="pending")
    progress: Mapped[float] = mapped_column(Float, default=0)
    message: Mapped[str] = mapped_column(Text, default="")
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class AppSetting(Base):
    __tablename__ = "app_setting"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class AniDBTitle(Base):
    __tablename__ = "anidb_title"
    id: Mapped[int] = mapped_column(primary_key=True)
    aid: Mapped[int] = mapped_column(Integer, index=True)
    title: Mapped[str] = mapped_column(String(500))
    normalized_title: Mapped[str] = mapped_column(String(500), index=True)
    language: Mapped[str] = mapped_column(String(30))
    title_type: Mapped[str] = mapped_column(String(30))
