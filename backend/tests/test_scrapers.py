from lxml import etree
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AniDBTitle
from app.parser import normalize_title
from app.scrapers import AniDBScraper, _preferred_detail_titles, _preferred_stored_titles


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
