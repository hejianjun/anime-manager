from __future__ import annotations

import asyncio
import gzip
import io
import json
import re
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urljoin

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
DMM_ITEM_LIST_URL = "https://api.dmm.com/affiliate/v3/ItemList"
GETCHU_SEARCH_URL = "https://www.getchu.com/php/nsearch.phtml"
GETCHU_ITEM_URL = "https://www.getchu.com/item/{source_id}/"
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
SOURCE_CACHE_AGE = timedelta(days=1)


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


def _cached_source_metadata(
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


def _store_source_metadata(
    db: Session,
    source: str,
    source_id: str,
    raw_payload: dict[str, Any],
    metadata: SourceMetadata,
    cached: SourceSnapshot | None,
) -> None:
    normalized = asdict(metadata)
    if cached:
        cached.raw_payload = raw_payload
        cached.normalized_payload = normalized
        cached.fetched_at = datetime.now(timezone.utc)
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


def _http_error_detail(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return type(exc).__name__


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
        if cached and fetched_at and datetime.now(timezone.utc) - fetched_at < timedelta(days=1):
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


def _dmm_iteminfo_names(item: dict[str, Any], key: str) -> list[str]:
    value = (item.get("iteminfo") or {}).get(key) or []
    if isinstance(value, dict):
        value = value.get("item") or [value]
    if not isinstance(value, list):
        return []
    return [
        str(entry.get("name")).strip()
        for entry in value
        if isinstance(entry, dict) and entry.get("name")
    ]


def _dmm_cover_url(item: dict[str, Any]) -> str | None:
    images = item.get("imageURL") or {}
    return images.get("large") or images.get("list") or images.get("small")


class DMMScraper(Scraper):
    source = "dmm"

    def _credentials(self, db: Session) -> tuple[str, str]:
        api_id = str(get_setting(db, "dmm_api_id", "") or "").strip()
        affiliate_id = str(get_setting(db, "dmm_affiliate_id", "") or "").strip()
        if not api_id or not affiliate_id:
            raise AppError(
                "DMM_NOT_CONFIGURED",
                "请先在设置中填写 DMM API ID 和 API 专用 Affiliate ID",
                status_code=409,
            )
        return api_id, affiliate_id

    async def _items(
        self, db: Session, *, keyword: str | None = None, cid: str | None = None
    ) -> list[dict[str, Any]]:
        api_id, affiliate_id = self._credentials(db)
        params: dict[str, Any] = {
            "api_id": api_id,
            "affiliate_id": affiliate_id,
            "site": "FANZA",
            "service": "digital",
            "floor": "anime",
            "hits": 30 if keyword else 1,
            "sort": "match",
            "output": "json",
        }
        if keyword:
            params["keyword"] = keyword
        if cid:
            params["cid"] = cid
        proxy = get_setting(db, "proxy_url")
        try:
            async with httpx.AsyncClient(
                proxy=proxy or None,
                timeout=45,
                follow_redirects=True,
                headers={"User-Agent": "AnimeManager/0.1 (local metadata client)"},
            ) as client:
                response = await client.get(DMM_ITEM_LIST_URL, params=params)
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise AppError(
                "DMM_REQUEST_FAILED",
                "DMM/FANZA 商品接口请求失败",
                details=_http_error_detail(exc),
                retryable=True,
                status_code=502,
            ) from exc
        result = payload.get("result") or {}
        try:
            status = int(result.get("status", 0))
        except (TypeError, ValueError):
            status = 0
        if status != 200:
            raise AppError(
                "DMM_API_ERROR",
                str(result.get("message") or "DMM/FANZA 商品接口返回错误"),
                details={"status": status},
                retryable=status >= 500,
                status_code=502,
            )
        items = result.get("items") or []
        return items if isinstance(items, list) else []

    async def search(self, db: Session, keyword: str) -> list[Candidate]:
        items = await self._items(db, keyword=keyword)
        normalized = normalize_title(keyword)
        candidates: list[Candidate] = []
        for item in items:
            source_id = str(item.get("content_id") or item.get("product_id") or "").strip()
            title = str(item.get("title") or "").strip()
            if not source_id or not title:
                continue
            score = fuzz.WRatio(normalized, normalize_title(title)) / 100
            date = str(item.get("date") or "")
            candidates.append(
                Candidate(
                    source=self.source,
                    source_id=source_id,
                    title=title,
                    score=round(score, 4),
                    year=int(date[:4]) if re.match(r"^\d{4}", date) else None,
                    cover_url=_dmm_cover_url(item),
                    payload={
                        "floor": item.get("floor_code"),
                        "url": item.get("URL"),
                        "maker": (_dmm_iteminfo_names(item, "maker") or [None])[0],
                    },
                )
            )
        return sorted(candidates, key=lambda item: item.score, reverse=True)[:20]

    async def detail(self, db: Session, source_id: str) -> SourceMetadata:
        cached, metadata = _cached_source_metadata(db, self.source, source_id)
        if metadata:
            return metadata
        items = await self._items(db, cid=source_id)
        if not items:
            raise AppError("DMM_NOT_FOUND", "DMM/FANZA 商品不存在", status_code=404)
        item = items[0]
        date = str(item.get("date") or "")
        makers = _dmm_iteminfo_names(item, "maker")
        genres = _dmm_iteminfo_names(item, "genre")
        series = _dmm_iteminfo_names(item, "series")
        metadata = SourceMetadata(
            source=self.source,
            source_id=source_id,
            title=str(item.get("title") or f"DMM {source_id}").strip(),
            original_title=str(item.get("title") or "").strip() or None,
            year=int(date[:4]) if re.match(r"^\d{4}", date) else None,
            media_type=str(item.get("floor_name") or "アニメ"),
            studio=makers[0] if makers else None,
            cover_url=_dmm_cover_url(item),
            genres=genres,
            tags=series,
            cast=[],
            staff=[],
        )
        _store_source_metadata(db, self.source, source_id, item, metadata, cached)
        return metadata


def _getchu_product(root: etree._Element) -> dict[str, Any]:
    for raw in root.xpath('//script[@type="application/ld+json"]/text()'):
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            continue
        entries = payload.get("@graph", []) if isinstance(payload, dict) else []
        for entry in entries:
            if isinstance(entry, dict) and entry.get("@type") == "Product":
                return entry
    return {}


def _getchu_section_text(root: etree._Element, heading_text: str) -> str | None:
    heading = next(
        (
            item
            for item in root.xpath("//h2|//h3|//h4")
            if heading_text in " ".join("".join(item.itertext()).split())
        ),
        None,
    )
    if heading is None:
        return None
    parts: list[str] = []
    for sibling in heading.itersiblings():
        if sibling.tag in {"h2", "h3", "h4"}:
            break
        if sibling.tag in {"script", "style"}:
            continue
        text = " ".join("".join(sibling.itertext()).split())
        if text:
            parts.append(text)
    result = "\n".join(parts).strip()
    return result[:10000] or None


class GetchuScraper(Scraper):
    source = "getchu"

    async def _get(self, db: Session, url: str) -> tuple[httpx.Response, etree._Element]:
        proxy = get_setting(db, "proxy_url")
        try:
            async with httpx.AsyncClient(
                proxy=proxy or None,
                timeout=45,
                follow_redirects=True,
                cookies={"getchu_adalt_flag": "getchu.com"},
                headers={
                    "User-Agent": "AnimeManager/0.1 (local metadata client)",
                    "Accept-Language": "ja-JP,ja;q=0.9",
                },
            ) as client:
                response = await client.get(url)
                response.raise_for_status()
            response.encoding = "euc-jp"
            return response, etree.HTML(response.text)
        except (httpx.HTTPError, etree.XMLSyntaxError) as exc:
            raise AppError(
                "GETCHU_REQUEST_FAILED",
                "Getchu 页面请求失败",
                details=_http_error_detail(exc),
                retryable=True,
                status_code=502,
            ) from exc

    async def search(self, db: Session, keyword: str) -> list[Candidate]:
        query = urlencode(
            {
                "search_title": keyword,
                "genre": "anime_dvd",
                "list_count": "30",
                "list_type": "detail",
                "search": "search",
            },
            encoding="euc_jp",
        )
        response, root = await self._get(db, f"{GETCHU_SEARCH_URL}?{query}")
        normalized = normalize_title(keyword)
        candidates: dict[str, Candidate] = {}
        for anchor in root.xpath('//a[contains(@href,"soft.phtml?id=")]'):
            match = re.search(r"soft\.phtml\?id=(\d+)", anchor.attrib.get("href", ""))
            title = " ".join("".join(anchor.itertext()).split())
            if not match or not title:
                continue
            source_id = match.group(1)
            container = next(iter(anchor.xpath("ancestor::li[1]")), None)
            if container is None:
                continue
            text = " ".join("".join(container.itertext()).split())
            date_match = re.search(r"発売日：\s*(\d{4})/\d{2}/\d{2}", text)
            brand_match = re.search(r"ブランド名：\s*(.+?)(?:\s+メディア：|\s+定価：)", text)
            cover = next(
                (
                    value
                    for value in container.xpath(".//img/@data-original | .//img/@src")
                    if f"/{source_id}/" in value and "package" in value and "r18.jpg" not in value
                ),
                None,
            )
            cover_url = (
                urljoin(str(response.url), cover)
                if cover
                else f"https://www.getchu.com/brandnew/{source_id}/c{source_id}package.jpg"
            )
            candidates[source_id] = Candidate(
                source=self.source,
                source_id=source_id,
                title=title,
                score=round(fuzz.WRatio(normalized, normalize_title(title)) / 100, 4),
                year=int(date_match.group(1)) if date_match else None,
                cover_url=cover_url,
                payload={
                    "url": urljoin(str(response.url), anchor.attrib.get("href", "")),
                    "brand": brand_match.group(1).strip() if brand_match else None,
                },
            )
        return sorted(candidates.values(), key=lambda item: item.score, reverse=True)[:20]

    async def detail(self, db: Session, source_id: str) -> SourceMetadata:
        if not re.fullmatch(r"\d+", source_id):
            raise AppError("GETCHU_INVALID_ID", "Getchu 商品 ID 无效", status_code=400)
        cached, metadata = _cached_source_metadata(db, self.source, source_id)
        if metadata:
            return metadata
        response, root = await self._get(db, GETCHU_ITEM_URL.format(source_id=source_id))
        product = _getchu_product(root)
        title = str(product.get("name") or "").strip()
        if not title:
            raise AppError("GETCHU_NOT_FOUND", "Getchu 商品不存在", status_code=404)
        page_text = " ".join(root.xpath("string(//body)").split())
        date_match = re.search(r"発売日：\s*(\d{4})/\d{2}/\d{2}", page_text)
        media_match = re.search(r"メディア：\s*(.+?)(?:\s+JANコード：|\s+品番：|\s+予約締切)", page_text)
        subgenre_match = re.search(r"サブジャンル：\s*(.+?)\s*\[一覧\]", page_text)
        image = product.get("image")
        if isinstance(image, list):
            image = next((item for item in image if item), None)
        brand = product.get("brand") or {}
        studio = brand.get("name") if isinstance(brand, dict) else None
        genres = (
            [item.strip() for item in re.split(r"[、,]", subgenre_match.group(1)) if item.strip()]
            if subgenre_match
            else []
        )
        metadata = SourceMetadata(
            source=self.source,
            source_id=source_id,
            title=title,
            original_title=title,
            description=_getchu_section_text(root, "商品紹介"),
            year=int(date_match.group(1)) if date_match else None,
            media_type=media_match.group(1).strip() if media_match else "アニメDVD",
            studio=str(studio).strip() if studio else None,
            cover_url=urljoin(str(response.url), str(image)) if image else None,
            genres=genres,
            tags=[],
            cast=[],
            staff=[],
        )
        _store_source_metadata(
            db,
            self.source,
            source_id,
            {
                "url": str(response.url),
                "product": product,
                "description": metadata.description,
            },
            metadata,
            cached,
        )
        return metadata


SCRAPERS: dict[str, Scraper] = {
    "anidb": AniDBScraper(),
    "dmm": DMMScraper(),
    "getchu": GetchuScraper(),
}
