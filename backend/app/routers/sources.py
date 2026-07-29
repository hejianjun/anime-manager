import httpx
from fastapi import APIRouter, Depends
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ..database import get_db
from ..errors import AppError
from ..models import AppSetting
from ..scrapers import SCRAPERS, AniDBScraper
from ..source_settings import enabled_scraper_names

router = APIRouter(prefix="/api/sources", tags=["sources"])


@router.get("/getchu/{source_id}/cover")
async def getchu_cover(source_id: int, db: Session = Depends(get_db)):
    proxy = next(
        (
            row.value
            for row in [db.get(AppSetting, "proxy_url")]
            if row and isinstance(row.value, str) and row.value
        ),
        None,
    )
    url = f"https://www.getchu.com/brandnew/{source_id}/c{source_id}package.jpg"
    try:
        async with httpx.AsyncClient(
            proxy=proxy,
            timeout=30,
            follow_redirects=True,
            headers={
                "User-Agent": "AnimeManager/0.1 (local metadata client)",
                "Referer": "https://www.getchu.com/",
            },
            cookies={"getchu_adalt_flag": "getchu.com"},
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise AppError(
            "GETCHU_COVER_FAILED",
            "Getchu 封面获取失败",
            details=type(exc).__name__,
            retryable=True,
            status_code=502,
        ) from exc
    mime = response.headers.get("content-type", "").split(";")[0]
    if not mime.startswith("image/"):
        raise AppError("INVALID_ARTWORK", "Getchu 封面响应不是图片", status_code=502)
    return Response(
        content=response.content,
        media_type=mime,
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.post("/anidb/titles/refresh")
async def refresh_titles(db: Session = Depends(get_db)):
    if "anidb" not in enabled_scraper_names(db):
        raise AppError(
            "SOURCE_DISABLED",
            "AniDB 爬虫已在设置中停用",
            status_code=409,
        )
    scraper = SCRAPERS["anidb"]
    assert isinstance(scraper, AniDBScraper)
    return await scraper.refresh_titles(db)
