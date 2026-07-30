from __future__ import annotations

import asyncio
import gzip
import io
import re
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from lxml import etree
from rapidfuzz import fuzz
from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from ..config import settings
from ..errors import AppError
from ..models import AniDBTitle, AppSetting, SourceSnapshot
from ..parser import normalize_title
from .base import Candidate, Scraper, SourceMetadata, get_setting

TITLE_DUMP_URL = "https://anidb.net/api/anime-titles.xml.gz"
EPISODE_PARSER_VERSION = 2
DETAIL_URL = "http://api.anidb.net:9001/httpapi"
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"


def _title_display_priority(title: AniDBTitle) -> tuple[int, int]:
    language_priority = {"ja": 0, "x-jat": 1, "en": 2}.get(title.language, 3)
    type_priority = {"official": 0, "main": 1}.get(title.title_type, 2)
    return language_priority, type_priority


def _preferred_stored_titles(db: Session, aid: int) -> tuple[str | None, str | None]:
    titles = db.scalars(select(AniDBTitle).where(AniDBTitle.aid == aid)).all()

    def find(*, language: str | None = None, title_type: str | None = None) -> str | None:
        return next(
            (
                item.title
                for item in titles
                if (language is None or item.language == language)
                and (title_type is None or item.title_type == title_type)
            ),
            None,
        )

    japanese = find(language="ja", title_type="official") or find(language="ja")
    main = find(title_type="main")
    english = find(language="en", title_type="official") or find(language="en")
    return japanese or main or english, japanese or main


def _preferred_detail_titles(titles: list[etree._Element]) -> tuple[str | None, str | None]:
    def find(*, language: str | None = None, title_type: str | None = None) -> str | None:
        return next(
            (
                item.text
                for item in titles
                if (language is None or item.attrib.get(XML_LANG) == language)
                and (title_type is None or item.attrib.get("type") == title_type)
            ),
            None,
        )

    japanese = find(language="ja", title_type="official") or find(language="ja")
    main = find(title_type="main")
    english = find(language="en", title_type="official") or find(language="en")
    return japanese or main or english, japanese or main


def _episode_titles(root: etree._Element) -> dict[str, str]:
    result: dict[str, str] = {}
    for episode in root.findall("./episodes/episode"):
        epno = episode.find("epno")
        if epno is None:
            continue
        number = (epno.text or "").strip().upper()
        if not number.isalnum():
            continue
        titles = episode.findall("title")
        preferred = next(
            (
                (item.text or "").strip()
                for language in ("ja", "x-jat", "en")
                for item in titles
                if item.attrib.get(XML_LANG) == language and (item.text or "").strip()
            ),
            None,
        )
        if not preferred:
            preferred = next(
                ((item.text or "").strip() for item in titles if (item.text or "").strip()),
                None,
            )
        if preferred:
            result[str(int(number)) if number.isdigit() else number] = preferred
    return result


class AniDBScraper(Scraper):
    source = "anidb"
    _request_lock = asyncio.Lock()
    _last_request_at: datetime | None = None

    async def refresh_titles(self, db: Session, *, force: bool = False) -> dict[str, Any]:
        last_value = get_setting(db, "anidb_titles_refreshed_at")
        if last_value:
            last = datetime.fromisoformat(last_value)
            if datetime.now(timezone.utc) - last < timedelta(hours=24):
                return {"refreshed": False, "reason": "24h_limit", "last_refreshed_at": last_value}
        settings.cache_dir.mkdir(parents=True, exist_ok=True)
        proxy = get_setting(db, "proxy_url")
        try:
            async with httpx.AsyncClient(proxy=proxy or None, timeout=60, follow_redirects=True) as client:
                response = await client.get(TITLE_DUMP_URL)
                response.raise_for_status()
            raw = gzip.decompress(response.content)
            root = etree.parse(io.BytesIO(raw)).getroot()
            rows: list[AniDBTitle] = []
            for anime in root.findall("anime"):
                aid = int(anime.attrib["aid"])
                for title in anime.findall("title"):
                    text = (title.text or "").strip()
                    if text:
                        rows.append(
                            AniDBTitle(
                                aid=aid,
                                title=text,
                                normalized_title=normalize_title(text),
                                language=title.attrib.get(XML_LANG, ""),
                                title_type=title.attrib.get("type", ""),
                            )
                        )
            db.execute(delete(AniDBTitle))
            for offset in range(0, len(rows), 5000):
                db.bulk_save_objects(rows[offset : offset + 5000])
            now = datetime.now(timezone.utc).isoformat()
            setting = db.get(AppSetting, "anidb_titles_refreshed_at")
            if setting:
                setting.value = now
            else:
                db.add(AppSetting(key="anidb_titles_refreshed_at", value=now))
            dump_path = settings.cache_dir / "anime-titles.xml.gz"
            dump_path.write_bytes(response.content)
            db.commit()
            return {"refreshed": True, "titles": len(rows), "last_refreshed_at": now}
        except (httpx.HTTPError, OSError, etree.XMLSyntaxError) as exc:
            db.rollback()
            raise AppError(
                "ANIDB_TITLE_REFRESH_FAILED",
                "AniDB 标题库刷新失败",
                details=str(exc),
                retryable=True,
                status_code=502,
            ) from exc

    async def search(self, db: Session, keyword: str) -> list[Candidate]:
        count = db.query(AniDBTitle).count()
        if count == 0:
            await self.refresh_titles(db)
        normalized = normalize_title(keyword)
        tokens = sorted(
            {token for token in normalized.split() if len(token) >= 2},
            key=len,
            reverse=True,
        )
        clauses = [AniDBTitle.normalized_title.contains(normalized)]
        clauses.extend(AniDBTitle.normalized_title.contains(token) for token in tokens[:3])
        candidates = db.scalars(
            select(AniDBTitle).where(or_(*clauses)).limit(5000)
        ).all()
        if not candidates:
            candidates = db.scalars(select(AniDBTitle).limit(5000)).all()
        best: dict[int, tuple[AniDBTitle, float]] = {}
        for title in candidates:
            score = fuzz.WRatio(normalized, title.normalized_title) / 100
            current = best.get(title.aid)
            if current is None or score > current[1]:
                best[title.aid] = (title, score)
        display_titles: dict[int, AniDBTitle] = {}
        if best:
            for title in db.scalars(select(AniDBTitle).where(AniDBTitle.aid.in_(best))).all():
                current = display_titles.get(title.aid)
                if current is None or _title_display_priority(title) < _title_display_priority(current):
                    display_titles[title.aid] = title
        return [
            Candidate(
                source=self.source,
                source_id=str(item.aid),
                title=display_titles.get(item.aid, item).title,
                score=round(score, 4),
                payload={
                    "language": display_titles.get(item.aid, item).language,
                    "title_type": display_titles.get(item.aid, item).title_type,
                    "matched_title": item.title,
                },
            )
            for item, score in sorted(best.values(), key=lambda pair: pair[1], reverse=True)[:20]
            if score >= 0.35
        ]

    async def detail(self, db: Session, source_id: str) -> SourceMetadata:
        cached = db.scalar(
            select(SourceSnapshot).where(
                SourceSnapshot.source == self.source,
                SourceSnapshot.source_id == source_id,
            )
        )
        if cached:
            fetched_at = cached.fetched_at
            if fetched_at.tzinfo is None:
                fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        else:
            fetched_at = None
        cache_has_episode_titles = cached and "episode_titles" in cached.normalized_payload
        cache_has_current_episode_parser = (
            cached
            and cached.raw_payload.get("episode_parser_version")
            == EPISODE_PARSER_VERSION
        )
        if (
            cached
            and cache_has_episode_titles
            and cache_has_current_episode_parser
            and fetched_at
            and datetime.now(timezone.utc) - fetched_at < timedelta(days=1)
        ):
            metadata = SourceMetadata(**cached.normalized_payload)
            title, original_title = _preferred_stored_titles(db, int(source_id))
            if title:
                metadata.title = title
                metadata.original_title = original_title
                cached.normalized_payload = asdict(metadata)
            return metadata
        client_name = get_setting(db, "anidb_client")
        client_ver = get_setting(db, "anidb_clientver")
        if not client_name or not client_ver:
            raise AppError(
                "ANIDB_CLIENT_NOT_CONFIGURED",
                "请先在设置中填写 AniDB 注册的 client 和 clientver",
                status_code=409,
            )
        interval = float(get_setting(db, "request_interval_seconds", 2.1))
        proxy = get_setting(db, "proxy_url")
        async with self._request_lock:
            if self._last_request_at:
                remaining = interval - (datetime.now(timezone.utc) - self._last_request_at).total_seconds()
                if remaining > 0:
                    await asyncio.sleep(remaining)
            try:
                async with httpx.AsyncClient(proxy=proxy or None, timeout=45) as client:
                    response = await client.get(
                        DETAIL_URL,
                        params={
                            "request": "anime",
                            "client": str(client_name).lower(),
                            "clientver": int(client_ver),
                            "protover": 1,
                            "aid": int(source_id),
                        },
                    )
                    response.raise_for_status()
                self._last_request_at = datetime.now(timezone.utc)
                root = etree.fromstring(response.content)
            except (httpx.HTTPError, ValueError, etree.XMLSyntaxError) as exc:
                raise AppError(
                    "ANIDB_DETAIL_FAILED",
                    "AniDB 详情获取失败",
                    details=str(exc),
                    retryable=True,
                    status_code=502,
                ) from exc
        if root.tag == "error":
            raise AppError(
                "ANIDB_API_ERROR",
                root.text or "AniDB 返回错误",
                retryable=(root.text or "").lower() not in {"banned"},
                status_code=502,
            )
        title, original_title = _preferred_detail_titles(root.findall("./titles/title"))
        start_date = root.findtext("startdate")
        picture = root.findtext("picture")
        metadata = SourceMetadata(
            source=self.source,
            source_id=source_id,
            title=title or f"AniDB {source_id}",
            original_title=original_title,
            description=(root.findtext("description") or "").strip() or None,
            year=int(start_date[:4]) if start_date and re.match(r"^\d{4}", start_date) else None,
            media_type=root.findtext("type"),
            episode_count=int(root.findtext("episodecount")) if (root.findtext("episodecount") or "").isdigit() else None,
            cover_url=f"https://cdn.anidb.net/images/main/{picture}" if picture else None,
            episode_titles=_episode_titles(root),
            tags=[
                item.findtext("name")
                for item in root.findall("./tags/tag")
                if item.findtext("name") and item.attrib.get("localspoiler") != "true"
            ],
            genres=[],
            cast=[],
            staff=[],
        )
        normalized = asdict(metadata)
        raw = {
            "id": source_id,
            "type": root.findtext("type"),
            "startdate": start_date,
            "picture": picture,
            "episode_titles": metadata.episode_titles,
            "episode_parser_version": EPISODE_PARSER_VERSION,
        }
        if cached:
            cached.raw_payload = raw
            cached.normalized_payload = normalized
            cached.fetched_at = datetime.now(timezone.utc)
        else:
            db.add(
                SourceSnapshot(
                    source=self.source,
                    source_id=source_id,
                    raw_payload=raw,
                    normalized_payload=normalized,
                )
            )
        db.flush()
        return metadata
