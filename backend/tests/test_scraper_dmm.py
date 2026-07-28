import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.errors import AppError
from app.scrapers import DMMScraper


class StubDMMScraper(DMMScraper):
    async def _items(self, _db: Session, **_kwargs):
        return [
            {
                "content_id": "anime001",
                "title": "作品名 The Animation",
                "date": "2025-04-01 00:00:00",
                "floor_name": "アニメ",
                "imageURL": {"large": "https://pics.example/large.jpg"},
                "iteminfo": {
                    "maker": [{"id": 1, "name": "制作会社"}],
                    "genre": [{"id": 2, "name": "ジャンル"}],
                    "series": [{"id": 3, "name": "シリーズ"}],
                },
            }
        ]


async def test_dmm_search_and_detail_use_real_item_fields() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    scraper = StubDMMScraper()
    with Session(engine) as db:
        candidates = await scraper.search(db, "作品名")
        metadata = await scraper.detail(db, "anime001")

    assert candidates[0].source_id == "anime001"
    assert candidates[0].cover_url == "https://pics.example/large.jpg"
    assert candidates[0].is_mock is False
    assert metadata.year == 2025
    assert metadata.studio == "制作会社"
    assert metadata.genres == ["ジャンル"]
    assert metadata.tags == ["シリーズ"]


async def test_dmm_requires_api_credentials() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        with pytest.raises(AppError) as exc:
            await DMMScraper().search(db, "作品名")

    assert exc.value.code == "DMM_NOT_CONFIGURED"
