from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel):
    items: list[Any]
    total: int
    page: int
    page_size: int


class LibraryRootCreate(BaseModel):
    path: str
    enabled: bool = True


class LibraryRootPatch(BaseModel):
    path: str | None = None
    enabled: bool | None = None


class LibraryRootOut(ORMModel):
    id: int
    path: str
    enabled: bool
    last_scan_at: datetime | None
    created_at: datetime


class TaskOut(ORMModel):
    id: int
    kind: str
    status: str
    progress: float
    message: str
    result: dict[str, Any]
    error: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime


class MediaFileOut(ORMModel):
    id: int
    path: str
    relative_path: str
    size: int
    parsed_title: str
    episode: int | None
    duration: float | None
    width: int | None
    height: int | None
    video_codec: str | None
    status: str


class MediaFilePatch(BaseModel):
    episode: int | None = Field(default=None, ge=0, le=9999)
    parsed_title: str | None = Field(default=None, min_length=1, max_length=500)


class CandidateOut(ORMModel):
    id: int
    source: str
    source_id: str
    title: str
    year: int | None
    episode_count: int | None
    cover_url: str | None
    score: float
    selected: bool
    is_mock: bool


class MatchGroupOut(ORMModel):
    id: int
    display_title: str
    search_keyword: str
    status: str
    anime_id: int | None
    files: list[MediaFileOut] = []
    candidates: list[CandidateOut] = []


class MatchGroupPatch(BaseModel):
    search_keyword: str | None = Field(default=None, min_length=1, max_length=500)
    display_title: str | None = Field(default=None, min_length=1, max_length=500)


class SearchRequest(BaseModel):
    keyword: str | None = Field(default=None, min_length=1, max_length=500)
    sources: list[str] | None = None


class BulkSearchConfirmRequest(BaseModel):
    sources: list[str] | None = None


class SelectionRequest(BaseModel):
    selections: dict[str, int | None]


class BindExistingRequest(BaseModel):
    anime_id: int


class RenameRequest(BaseModel):
    season: int = Field(default=1, ge=0, le=99)


class MappingOut(ORMModel):
    source: str
    source_id: str
    is_mock: bool


class AnimeOut(ORMModel):
    id: int
    title: str
    original_title: str | None
    description: str | None
    year: int | None
    media_type: str | None
    episode_count: int | None
    studio: str | None
    cover_url: str | None
    episode_titles: dict[str, str]
    genres: list[str]
    tags: list[str]
    cast: list[dict[str, Any]]
    staff: list[dict[str, Any]]
    field_provenance: dict[str, str]
    mappings: list[MappingOut] = []
    files: list[MediaFileOut] = []
    updated_at: datetime


class AnimePatch(BaseModel):
    title: str | None = None
    original_title: str | None = None
    description: str | None = None
    year: int | None = None
    media_type: str | None = None
    episode_count: int | None = None
    studio: str | None = None
    genres: list[str] | None = None
    tags: list[str] | None = None


class SettingsPatch(BaseModel):
    enabled_scrapers: list[Literal["anidb", "dmm", "getchu"]] | None = None
    anidb_client: str | None = None
    anidb_clientver: int | None = None
    dmm_api_id: str | None = None
    dmm_affiliate_id: str | None = None
    proxy_url: str | None = None
    request_interval_seconds: float | None = Field(default=None, ge=2)
    scheduled_refresh: bool | None = None


class ExportRequest(BaseModel):
    overwrite: bool = False
