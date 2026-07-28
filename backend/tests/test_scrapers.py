import json

import httpx
from lxml import etree
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.errors import AppError
from app.models import AniDBTitle
from app.parser import normalize_title
from app.scrapers import (
    AniDBScraper,
    DMMScraper,
    GetchuScraper,
    _preferred_detail_titles,
    _preferred_stored_titles,
)


def title(aid: int, text: str, language: str, title_type: str) -> AniDBTitle:
    return AniDBTitle(
        aid=aid,
        title=text,
        normalized_title=normalize_title(text),
        language=language,
        title_type=title_type,
    )


async def test_search_matches_alias_but_displays_japanese_title() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all(
            [
                title(1, "English Name", "en", "official"),
                title(1, "日本語名", "ja", "official"),
                title(1, "Nihongo Name", "x-jat", "main"),
            ]
        )
        db.commit()

        candidates = await AniDBScraper().search(db, "English Name")

    assert candidates[0].title == "日本語名"
    assert candidates[0].score == 1
    assert candidates[0].payload == {
        "language": "ja",
        "title_type": "official",
        "matched_title": "English Name",
    }


def test_stored_title_prefers_japanese_for_cached_details() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all(
            [
                title(1, "English Name", "en", "official"),
                title(1, "Japanese Name", "ja", "official"),
                title(1, "Nihongo Name", "x-jat", "main"),
            ]
        )
        db.commit()

        assert _preferred_stored_titles(db, 1) == ("Japanese Name", "Japanese Name")


def test_detail_title_prefers_japanese_then_main() -> None:
    titles = etree.fromstring(
        b"""<titles>
        <title xml:lang="x-jat" type="main">Nihongo Name</title>
        <title xml:lang="en" type="official">English Name</title>
        <title xml:lang="ja" type="official">Japanese Name</title>
        </titles>"""
    ).findall("title")

    assert _preferred_detail_titles(titles) == ("Japanese Name", "Japanese Name")


def test_detail_title_falls_back_to_main_without_japanese() -> None:
    titles = etree.fromstring(
        b"""<titles>
        <title xml:lang="x-jat" type="main">Nihongo Name</title>
        <title xml:lang="en" type="official">English Name</title>
        </titles>"""
    ).findall("title")

    assert _preferred_detail_titles(titles) == ("Nihongo Name", "Nihongo Name")


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


class StubGetchuScraper(GetchuScraper):
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.requested_urls: list[str] = []

    async def _get(self, _db: Session, url: str):
        self.requested_urls.append(url)
        html = self.pages["detail" if "/item/" in url else "search"]
        response = httpx.Response(200, request=httpx.Request("GET", url))
        return response, etree.HTML(html)


async def test_getchu_search_and_detail_parse_product_pages() -> None:
    product = {
        "@graph": [
            {
                "@type": "Product",
                "name": "OVA 作品名 ＃1",
                "image": ["https://www.getchu.com/brandnew/123/c123package.jpg"],
                "brand": {"@type": "Brand", "name": "制作会社"},
                "sku": "123",
            }
        ]
    }
    pages = {
        "search": """
            <html><body><ul class="display"><li>
              <img data-original="https://www.getchu.com/brandnew/123/c123package_ss.jpg">
              <a href="../soft.phtml?id=123">OVA 作品名 ＃1</a>
              <div>発売日：2024/05/06 ブランド名： 制作会社 メディア： DVD-VIDEO 定価：￥1</div>
            </li></ul></body></html>
        """,
        "detail": f"""
            <html><body>
              <script type="application/ld+json">{json.dumps(product, ensure_ascii=False)}</script>
              <div>発売日：2024/05/06 メディア： DVD-VIDEO JANコード：1
                サブジャンル： アダルトアニメ、同人原作アニメ [一覧]</div>
              <h3>商品紹介</h3><div>作品の紹介文です。</div><h3>スタッフ</h3>
            </body></html>
        """,
    }
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    scraper = StubGetchuScraper(pages)
    with Session(engine) as db:
        candidates = await scraper.search(db, "作品名")
        metadata = await scraper.detail(db, "123")

    assert candidates[0].source_id == "123"
    assert candidates[0].year == 2024
    assert candidates[0].is_mock is False
    assert "%BA%EE%C9%CA%CC%BE" in scraper.requested_urls[0]
    assert metadata.title == "OVA 作品名 ＃1"
    assert metadata.description == "作品の紹介文です。"
    assert metadata.studio == "制作会社"
    assert metadata.genres == ["アダルトアニメ", "同人原作アニメ"]
