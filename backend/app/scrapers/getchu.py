from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any
from urllib.parse import urlencode, urljoin

import httpx
from lxml import etree
from rapidfuzz import fuzz
from sqlalchemy.orm import Session

from ..errors import AppError
from ..parser import normalize_title
from .base import (
    Candidate,
    Scraper,
    SourceMetadata,
    cached_source_metadata,
    get_setting,
    http_error_detail,
    store_source_metadata,
)

GETCHU_SEARCH_URL = "https://www.getchu.com/php/nsearch.phtml"
GETCHU_ITEM_URL = "https://www.getchu.com/item/{source_id}/"
GETCHU_REQUEST_TIMEOUT_SECONDS = 25

_request_lock: asyncio.Lock | None = None
_request_loop: asyncio.AbstractEventLoop | None = None
_last_request_started = 0.0


def _limiter_lock() -> asyncio.Lock:
    """每个事件循环使用独立锁，兼容应用重载和隔离事件循环的测试。"""
    global _request_lock, _request_loop
    loop = asyncio.get_running_loop()
    if _request_lock is None or _request_loop is not loop:
        _request_lock = asyncio.Lock()
        _request_loop = loop
    return _request_lock


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
        global _last_request_started
        proxy = get_setting(db, "proxy_url")
        interval = max(float(get_setting(db, "request_interval_seconds", 2.1) or 2.1), 0)
        last_error: Exception | None = None
        async with _limiter_lock():
            for attempt in range(2):
                wait_seconds = interval - (time.monotonic() - _last_request_started)
                if wait_seconds > 0:
                    await asyncio.sleep(wait_seconds)
                _last_request_started = time.monotonic()
                try:
                    async with httpx.AsyncClient(
                        proxy=proxy or None,
                        timeout=GETCHU_REQUEST_TIMEOUT_SECONDS,
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
                    last_error = exc
                    if attempt == 0:
                        continue
        assert last_error is not None
        raise AppError(
            "GETCHU_REQUEST_FAILED",
            "Getchu 页面请求失败",
            details=http_error_detail(last_error),
            retryable=True,
            status_code=502,
        ) from last_error

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
            errors="ignore",
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
        cached, metadata = cached_source_metadata(db, self.source, source_id)
        if metadata:
            return metadata
        try:
            response, root = await self._get(db, GETCHU_ITEM_URL.format(source_id=source_id))
        except AppError as exc:
            # An expired snapshot is still preferable to blocking confirmation
            # when Getchu is temporarily slow or unavailable.
            if cached is not None and exc.code == "GETCHU_REQUEST_FAILED":
                return SourceMetadata(**cached.normalized_payload)
            raise
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
        store_source_metadata(
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
