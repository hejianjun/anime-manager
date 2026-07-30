from __future__ import annotations

from dataclasses import asdict

from sqlalchemy import delete, select, tuple_
from sqlalchemy.orm import Session

from .description_translation import auto_translate_anime_description
from .errors import AppError
from .models import (
    Anime,
    MatchGroup,
    MediaFile,
    ScrapeCandidate,
    ScrapeHistory,
    SourceMapping,
    SourceSnapshot,
)
from .scrapers import SCRAPERS, Candidate, SourceMetadata

DESCRIPTION_SOURCES = {"anidb", "getchu"}


def _selected_group_files(
    group: MatchGroup, file_ids: list[int] | None
) -> list[MediaFile]:
    """返回本次要绑定的组内文件，并拒绝空选择或跨组文件 ID。"""
    files = list(group.files)
    if file_ids is None:
        return files
    requested = set(file_ids)
    selected = [media for media in files if media.id in requested]
    if not requested or len(selected) != len(requested):
        raise AppError("INVALID_MEDIA_SELECTION", "所选视频不属于当前待确认分组")
    return selected


async def search_group(
    db: Session, group: MatchGroup, keyword: str, sources: list[str]
) -> tuple[list[ScrapeCandidate], list[dict]]:
    group.search_keyword = keyword
    errors: list[dict] = []
    results: list[ScrapeCandidate] = []
    for source in sources:
        scraper = SCRAPERS.get(source)
        if not scraper:
            errors.append({"source": source, "code": "UNKNOWN_SOURCE", "message": "未知数据源"})
            continue
        try:
            found = await scraper.search(db, keyword)
        except AppError as exc:
            errors.append(
                {"source": source, "code": exc.code, "message": exc.message, "details": exc.details}
            )
            continue
        db.execute(
            delete(ScrapeCandidate).where(
                ScrapeCandidate.match_group_id == group.id,
                ScrapeCandidate.source == source,
            )
        )
        for item in found:
            row = ScrapeCandidate(
                match_group_id=group.id,
                source=item.source,
                source_id=item.source_id,
                title=item.title,
                year=item.year,
                episode_count=item.episode_count,
                cover_url=item.cover_url,
                score=item.score,
                is_mock=item.is_mock,
                payload=item.payload or {},
            )
            db.add(row)
            results.append(row)
    db.commit()
    return results, errors


def save_selections(db: Session, group: MatchGroup, selections: dict[str, int | None]) -> None:
    allowed = {"anidb", "dmm", "getchu"}
    for source, candidate_id in selections.items():
        if source not in allowed:
            raise AppError("UNKNOWN_SOURCE", f"未知数据源: {source}")
        db.query(ScrapeCandidate).filter_by(match_group_id=group.id, source=source).update(
            {"selected": False}
        )
        if candidate_id is not None:
            candidate = db.get(ScrapeCandidate, candidate_id)
            if not candidate or candidate.match_group_id != group.id or candidate.source != source:
                raise AppError("INVALID_CANDIDATE", "候选不属于当前分组或数据源")
            candidate.selected = True
    db.commit()


def _apply_metadata(anime: Anime, metadata: SourceMetadata) -> None:
    """按人工字段和来源优先级合并元数据，并记录每个字段的来源。"""
    if metadata.is_mock:
        return
    values = asdict(metadata)
    field_map = {
        "title": "title",
        "original_title": "original_title",
        "description": "description",
        "year": "year",
        "media_type": "media_type",
        "episode_count": "episode_count",
        "studio": "studio",
        "cover_url": "cover_url",
        "episode_titles": "episode_titles",
        "genres": "genres",
        "tags": "tags",
        "cast": "cast",
        "staff": "staff",
    }
    provenance = dict(anime.field_provenance or {})
    for source_field, target_field in field_map.items():
        if target_field in (anime.manual_fields or []):
            continue
        value = values.get(source_field)
        if value not in (None, "", [], {}):
            # Getchu 的条目是 DVD、Blu-ray 或 BOX 等商品，不是稳定的作品记录。
            # 已由 AniDB 提供的作品级字段保持不变，Getchu 只补充 AniDB 缺失的内容。
            if metadata.source == "getchu" and provenance.get(target_field) == "anidb":
                continue
            setattr(anime, target_field, value)
            provenance[target_field] = metadata.source
    anime.field_provenance = provenance


def _selected_candidates(
    db: Session,
    group: MatchGroup,
    enabled_sources: list[str] | None,
) -> list[ScrapeCandidate]:
    """读取人工勾选且当前允许使用的来源候选。"""
    selected = list(
        db.scalars(
            select(ScrapeCandidate).where(
                ScrapeCandidate.match_group_id == group.id,
                ScrapeCandidate.selected.is_(True),
            )
        ).all()
    )
    if enabled_sources is not None:
        selected = [item for item in selected if item.source in enabled_sources]
    if not selected:
        raise AppError("NO_SELECTION", "请至少选择一个候选结果")
    return selected


def _resolve_target_anime(
    db: Session,
    group: MatchGroup,
    selected: list[ScrapeCandidate],
) -> Anime:
    """复用分组或来源映射指向的作品；没有既有作品时再创建新作品。"""
    anime = db.get(Anime, group.anime_id) if group.anime_id else None
    mapped_anime_ids = set(
        db.scalars(
            select(SourceMapping.anime_id).where(
                tuple_(SourceMapping.source, SourceMapping.source_id).in_(
                    [(item.source, item.source_id) for item in selected]
                )
            )
        ).all()
    )
    if anime:
        mapped_anime_ids.discard(anime.id)
    if mapped_anime_ids:
        if anime or len(mapped_anime_ids) > 1:
            raise AppError(
                "SOURCES_BOUND_TO_DIFFERENT_ANIME",
                "选择的来源候选已绑定到不同作品，请调整选择",
                status_code=409,
            )
        anime = db.get(Anime, mapped_anime_ids.pop())
    if anime:
        return anime

    # AniDB 通常提供更规范的主标题；未选择 AniDB 时使用第一个候选标题。
    title_candidate = next(
        (item for item in selected if item.source == "anidb"),
        selected[0],
    )
    anime = Anime(title=title_candidate.title)
    db.add(anime)
    db.flush()
    return anime


def _upsert_source_mapping(
    db: Session,
    anime: Anime,
    candidate: ScrapeCandidate,
) -> None:
    """同一作品每个来源只保留一个稳定映射，重新选择时更新来源 ID。"""
    mapping = db.scalar(
        select(SourceMapping).where(
            SourceMapping.anime_id == anime.id,
            SourceMapping.source == candidate.source,
        )
    )
    if mapping:
        mapping.source_id = candidate.source_id
        mapping.is_mock = candidate.is_mock
        return
    db.add(
        SourceMapping(
            anime_id=anime.id,
            source=candidate.source,
            source_id=candidate.source_id,
            is_mock=candidate.is_mock,
        )
    )


async def _bind_candidate_sources(
    db: Session,
    anime: Anime,
    selected: list[ScrapeCandidate],
) -> None:
    """保存来源映射、抓取详情，并为每个成功来源留下刷新历史。"""
    for candidate in selected:
        _upsert_source_mapping(db, anime, candidate)
        metadata = await SCRAPERS[candidate.source].detail(db, candidate.source_id)
        _apply_metadata(anime, metadata)
        db.add(
            ScrapeHistory(
                anime_id=anime.id,
                source=candidate.source,
                success=True,
                message="确认绑定并获取元数据",
            )
        )


def _bind_group_files(
    group: MatchGroup,
    anime: Anime,
    files: list[MediaFile],
) -> None:
    """绑定所选视频；部分绑定时保留剩余视频所在的待确认分组。"""
    partial = len(files) < len(group.files)
    group.anime_id = None if partial else anime.id
    group.status = "pending" if partial else "confirmed"
    for media in files:
        media.anime_id = anime.id
        if partial:
            media.match_group_id = None


async def _record_auto_translation(db: Session, anime: Anime) -> None:
    result = await auto_translate_anime_description(db, anime)
    if result["status"] not in {"translated", "failed"}:
        return
    db.add(
        ScrapeHistory(
            anime_id=anime.id,
            source="translation",
            success=result["status"] == "translated",
            message=(
                "简介已自动翻译为简体中文"
                if result["status"] == "translated"
                else f"{result.get('code')}: {result.get('message')}"
            ),
        )
    )


async def confirm_group(
    db: Session,
    group: MatchGroup,
    enabled_sources: list[str] | None = None,
    file_ids: list[int] | None = None,
) -> Anime:
    """确认人工选择，并在同一事务中完成作品、来源和视频绑定。"""
    selected = _selected_candidates(db, group, enabled_sources)
    files = _selected_group_files(group, file_ids)
    if not files:
        raise AppError("NO_MEDIA_SELECTION", "请至少勾选一个视频")
    anime = _resolve_target_anime(db, group, selected)
    try:
        await _bind_candidate_sources(db, anime, selected)
        _bind_group_files(group, anime, files)
        await _record_auto_translation(db, anime)
        db.commit()
        db.refresh(anime)
        return anime
    except Exception:
        db.rollback()
        raise


async def refresh_anime(
    db: Session, anime: Anime, enabled_sources: list[str] | None = None
) -> Anime:
    for mapping in anime.mappings:
        if enabled_sources is not None and mapping.source not in enabled_sources:
            continue
        try:
            metadata = await SCRAPERS[mapping.source].detail(db, mapping.source_id)
            _apply_metadata(anime, metadata)
            db.add(
                ScrapeHistory(
                    anime_id=anime.id,
                    source=mapping.source,
                    success=True,
                    message="元数据刷新成功",
                )
            )
        except AppError as exc:
            db.add(
                ScrapeHistory(
                    anime_id=anime.id,
                    source=mapping.source,
                    success=False,
                    message=f"{exc.code}: {exc.message}",
                )
            )
    await _record_auto_translation(db, anime)
    db.commit()
    db.refresh(anime)
    return anime


async def search_description_candidates(
    db: Session,
    anime: Anime,
    keyword: str,
    enabled_sources: list[str],
) -> tuple[list[Candidate], list[dict]]:
    """搜索尚未绑定且能够提供简介的数据源，结果交由用户确认。"""
    bound_sources = {mapping.source for mapping in anime.mappings}
    sources = [
        source
        for source in enabled_sources
        if source in DESCRIPTION_SOURCES and source not in bound_sources
    ]
    results: list[Candidate] = []
    errors: list[dict] = []
    for source in sources:
        try:
            results.extend(await SCRAPERS[source].search(db, keyword))
        except AppError as exc:
            errors.append(
                {
                    "source": source,
                    "code": exc.code,
                    "message": exc.message,
                    "details": exc.details,
                }
            )
    return results, errors


async def fill_missing_description(
    db: Session,
    anime: Anime,
    source: str,
    source_id: str,
    enabled_sources: list[str],
) -> Anime:
    """由人工确认的其他来源仅补写缺失简介，并保存来源映射供后续刷新。"""
    if (anime.description or "").strip():
        raise AppError("DESCRIPTION_EXISTS", "当前作品已有简介，不会自动覆盖", status_code=409)
    if source not in SCRAPERS or source not in DESCRIPTION_SOURCES:
        raise AppError("UNKNOWN_SOURCE", f"该数据源不支持补抓简介: {source}")
    if source not in enabled_sources:
        raise AppError("SOURCE_DISABLED", "该数据源已在设置中停用", status_code=409)
    if any(mapping.source == source for mapping in anime.mappings):
        raise AppError("SOURCE_ALREADY_BOUND", "该数据源已绑定，请直接刷新元数据", status_code=409)

    existing = db.scalar(
        select(SourceMapping).where(
            SourceMapping.source == source,
            SourceMapping.source_id == source_id,
        )
    )
    if existing and existing.anime_id != anime.id:
        raise AppError(
            "SOURCE_ALREADY_USED",
            "选择的来源条目已绑定到其他作品",
            status_code=409,
        )

    try:
        metadata = await SCRAPERS[source].detail(db, source_id)
        description = (metadata.description or "").strip()
        if metadata.is_mock or not description:
            raise AppError(
                "SOURCE_DESCRIPTION_EMPTY",
                "所选来源条目没有可用简介，请选择其他候选",
                status_code=409,
            )
        db.add(
            SourceMapping(
                anime_id=anime.id,
                source=source,
                source_id=source_id,
                is_mock=False,
            )
        )
        anime.description = description
        provenance = dict(anime.field_provenance or {})
        provenance["description"] = source
        anime.field_provenance = provenance
        db.add(
            ScrapeHistory(
                anime_id=anime.id,
                source=source,
                success=True,
                message="人工确认候选并补充简介",
            )
        )
        await _record_auto_translation(db, anime)
        db.commit()
        db.refresh(anime)
        return anime
    except Exception:
        db.rollback()
        raise
