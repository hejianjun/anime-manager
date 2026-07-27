from __future__ import annotations

import asyncio
import gzip
import hashlib
import io
import re
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from lxml import etree
from rapidfuzz import fuzz
from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from .config import settings
from .errors import AppError
from .models import AniDBTitle, AppSetting, SourceSnapshot
from .parser import normalize_title

TITLE_DUMP_URL = "https://anidb.net/api/anime-titles.xml.gz"
DETAIL_URL = "http://api.anidb.net:9001/httpapi"


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
                                language=title.attrib.get("{http://www.w3.org/XML/1998/namespace}lang", ""),
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
            select(AniDBTitle)
            .where(or_(*clauses))
            .limit(5000)
        ).all()
        if not candidates:
            candidates = db.scalars(select(AniDBTitle).limit(5000)).all()
        best: dict[int, tuple[AniDBTitle, float]] = {}
        for title in candidates:
            score = fuzz.WRatio(normalized, title.normalized_title) / 100
            current = best.get(title.aid)
            if current is None or score > current[1]:
                best[title.aid] = (title, score)
        return [
            Candidate(
                source=self.source,
                source_id=str(item.aid),
                title=item.title,
                score=round(score, 4),
                payload={"language": item.language, "title_type": item.title_type},
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
        if cached and fetched_at and datetime.now(timezone.utc) - fetched_at < timedelta(days=1):
            return SourceMetadata(**cached.normalized_payload)
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
        titles = root.findall("./titles/title")
        main = next((item.text for item in titles if item.attrib.get("type") == "main"), None)
        english = next(
            (
                item.text
                for item in titles
                if item.attrib.get("type") == "official"
                and item.attrib.get("{http://www.w3.org/XML/1998/namespace}lang") == "en"
            ),
            None,
        )
        japanese = next(
            (
                item.text
                for item in titles
                if item.attrib.get("type") == "official"
                and item.attrib.get("{http://www.w3.org/XML/1998/namespace}lang") == "ja"
            ),
            None,
        )
        start_date = root.findtext("startdate")
        picture = root.findtext("picture")
        metadata = SourceMetadata(
            source=self.source,
            source_id=source_id,
            title=english or main or japanese or f"AniDB {source_id}",
            original_title=japanese or main,
            description=(root.findtext("description") or "").strip() or None,
            year=int(start_date[:4]) if start_date and re.match(r"^\d{4}", start_date) else None,
            media_type=root.findtext("type"),
            episode_count=int(root.findtext("episodecount")) if (root.findtext("episodecount") or "").isdigit() else None,
            cover_url=f"https://cdn.anidb.net/images/main/{picture}" if picture else None,
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


class DemoScraper(Scraper):
    def __init__(self, source: str) -> None:
        self.source = source

    async def search(self, db: Session, keyword: str) -> list[Candidate]:
        if not get_setting(db, "demo_scrapers", settings.demo_scrapers):
            return []
        digest = hashlib.sha1(f"{self.source}:{keyword}".encode()).hexdigest()[:8].upper()
        suffix = "The Animation" if self.source == "dmm" else "OVA"
        return [
            Candidate(
                source=self.source,
                source_id=f"DEMO-{digest}",
                title=f"{keyword} {suffix}",
                score=0.82 if self.source == "dmm" else 0.78,
                is_mock=True,
                payload={"fixture": True},
            )
        ]

    async def detail(self, db: Session, source_id: str) -> SourceMetadata:
        return SourceMetadata(
            source=self.source,
            source_id=source_id,
            title=f"{self.source.upper()} 演示元数据",
            is_mock=True,
        )


SCRAPERS: dict[str, Scraper] = {
    "anidb": AniDBScraper(),
    "dmm": DemoScraper("dmm"),
    "getchu": DemoScraper("getchu"),
}
