from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .episode_numbers import normalize_episode_identifier


# 所有数据库实体响应模型的共同基类，允许直接从 SQLAlchemy 对象读取字段。
class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# 通用分页响应；具体资源列表可以复用相同的分页元数据结构。
class Page(BaseModel):
    items: list[Any]
    total: int
    page: int
    page_size: int


# 媒体库根目录的新增、局部更新和返回结构。
class LibraryRootCreate(BaseModel):
    path: str
    scan_path: str | None = None
    enabled: bool = True


class LibraryRootPatch(BaseModel):
    path: str | None = None
    scan_path: str | None = None
    enabled: bool | None = None


class LibraryRootOut(ORMModel):
    id: int
    path: str
    scan_path: str | None
    enabled: bool
    # 尚未执行过扫描时为空。
    last_scan_at: datetime | None
    scan_last_scan_at: datetime | None
    created_at: datetime


# 扫描、批量匹配和批量输出等后台任务的统一状态。
class TaskOut(ORMModel):
    id: int
    kind: str
    status: str
    # 进度约定为 0 到 1；具体统计和失败详情分别放在 result、error 中。
    progress: float
    message: str
    result: dict[str, Any]
    error: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime


# 已扫描媒体文件的展示结构，不暴露内部哈希等维护字段。
class MediaFileOut(ORMModel):
    id: int
    path: str
    relative_path: str
    size: int
    parsed_title: str
    episode: str | None
    duration: float | None
    width: int | None
    height: int | None
    video_codec: str | None
    status: str


# 当前仅允许人工修正集号和解析出的作品标题。
class MediaFilePatch(BaseModel):
    episode: str | None = Field(default=None, max_length=16)
    parsed_title: str | None = Field(default=None, min_length=1, max_length=500)

    @field_validator("episode", mode="before")
    @classmethod
    def validate_episode(cls, value: object | None) -> str | None:
        return normalize_episode_identifier(value)


# 作品目录与 Jellyfin 输出文件的一致性检查结果。
class CatalogHealthOut(BaseModel):
    directory_name_mismatch: bool
    missing_nfo_count: int
    missing_episode_image_count: int


# 单个外部来源搜索候选；source_id 是后续刷新时使用的稳定来源标识。
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


# 扫描生成的待匹配分组，包含组内文件和所有来源候选。
class MatchGroupOut(ORMModel):
    id: int
    display_title: str
    search_keyword: str
    status: str
    anime_id: int | None
    files: list[MediaFileOut] = []
    candidates: list[CandidateOut] = []


# 允许人工修正匹配时使用的搜索词和界面展示标题。
class MatchGroupPatch(BaseModel):
    search_keyword: str | None = Field(default=None, min_length=1, max_length=500)
    display_title: str | None = Field(default=None, min_length=1, max_length=500)


# sources 为空时由接口使用当前设置中已启用的数据源。
class SearchRequest(BaseModel):
    keyword: str | None = Field(default=None, min_length=1, max_length=500)
    sources: list[str] | None = None


# 对全部待匹配分组执行搜索，并仅自动确认完全匹配的候选。
class BulkSearchConfirmRequest(BaseModel):
    sources: list[str] | None = None


# 每个来源对应一个候选 ID；值为 None 表示清除该来源的选择。
class SelectionRequest(BaseModel):
    selections: dict[str, int | None]


# 确认候选时可只绑定组内勾选的视频；不传时保持原有的整组绑定行为。
class ConfirmMatchRequest(BaseModel):
    file_ids: list[int] | None = Field(default=None, min_length=1)


# 将待匹配分组直接绑定到已经存在的作品，可限定为组内勾选的视频。
class BindExistingRequest(BaseModel):
    anime_id: int
    file_ids: list[int] | None = Field(default=None, min_length=1)


# 重命名预览和执行共用的季号参数。
class RenameRequest(BaseModel):
    season: int = Field(default=1, ge=0, le=99)


class BulkRenameExecuteRequest(BaseModel):
    preview_task_id: int = Field(gt=0)


# 已确认作品与外部元数据来源之间的持久映射。
class MappingOut(ORMModel):
    source: str
    source_id: str
    is_mock: bool


# 作品详情聚合响应，包含元数据、字段来源、外部映射和媒体文件。
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
    # 记录每个作品字段来自人工编辑还是某个外部数据源。
    field_provenance: dict[str, str]
    catalog_health: CatalogHealthOut
    mappings: list[MappingOut] = []
    files: list[MediaFileOut] = []
    updated_at: datetime


# 作品元数据的人工局部更新；被修改字段会在业务层标记为 manual。
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


# 可由前端修改的运行设置；未提供的字段保持原值。
class SettingsPatch(BaseModel):
    enabled_scrapers: list[Literal["anidb", "dmm", "getchu"]] | None = None
    anidb_client: str | None = None
    anidb_clientver: int | None = None
    dmm_api_id: str | None = None
    dmm_affiliate_id: str | None = None
    proxy_url: str | None = None
    request_interval_seconds: float | None = Field(default=None, ge=2)
    scheduled_refresh: bool | None = None
    auto_translate_description: bool | None = None
    translation_provider: Literal["openai", "tmt"] | None = None
    translation_base_url: str | None = None
    translation_api_key: str | None = None
    translation_model: str | None = None
    translation_timeout_seconds: float | None = Field(default=None, ge=1, le=300)
    tmt_secret_id: str | None = None
    tmt_secret_key: str | None = None
    tmt_region: str | None = None


# overwrite 为真时允许覆盖已有输出，但业务层仍会先创建备份。
class ExportRequest(BaseModel):
    overwrite: bool = False
