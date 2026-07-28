from __future__ import annotations

import re
from typing import Any

import httpx
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

DMM_ITEM_LIST_URL = "https://api.dmm.com/affiliate/v3/ItemList"


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
                details=http_error_detail(exc),
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
        cached, metadata = cached_source_metadata(db, self.source, source_id)
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
        store_source_metadata(db, self.source, source_id, item, metadata, cached)
        return metadata
