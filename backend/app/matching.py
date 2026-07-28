from __future__ import annotations

from dataclasses import asdict

from sqlalchemy import delete, select, tuple_
from sqlalchemy.orm import Session

from .errors import AppError
from .models import (
    Anime,
    MatchGroup,
    ScrapeCandidate,
    ScrapeHistory,
    SourceMapping,
    SourceSnapshot,
)
from .scrapers import SCRAPERS, Candidate, SourceMetadata


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
            setattr(anime, target_field, value)
            provenance[target_field] = metadata.source
    anime.field_provenance = provenance


async def confirm_group(
    db: Session, group: MatchGroup, enabled_sources: list[str] | None = None
) -> Anime:
    selected = db.scalars(
        select(ScrapeCandidate).where(
            ScrapeCandidate.match_group_id == group.id,
            ScrapeCandidate.selected.is_(True),
        )
    ).all()
    if enabled_sources is not None:
        selected = [item for item in selected if item.source in enabled_sources]
    if not selected:
        raise AppError("NO_SELECTION", "请至少选择一个候选结果")
    anidb = next((item for item in selected if item.source == "anidb"), None)
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
    if not anime:
        anime = Anime(title=(anidb or selected[0]).title)
        db.add(anime)
        db.flush()
    try:
        for candidate in selected:
            mapping = db.scalar(
                select(SourceMapping).where(
                    SourceMapping.anime_id == anime.id,
                    SourceMapping.source == candidate.source,
                )
            )
            if mapping:
                mapping.source_id = candidate.source_id
                mapping.is_mock = candidate.is_mock
            else:
                db.add(
                    SourceMapping(
                        anime_id=anime.id,
                        source=candidate.source,
                        source_id=candidate.source_id,
                        is_mock=candidate.is_mock,
                    )
                )
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
        group.anime_id = anime.id
        group.status = "confirmed"
        for media in group.files:
            media.anime_id = anime.id
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
    db.commit()
    db.refresh(anime)
    return anime
