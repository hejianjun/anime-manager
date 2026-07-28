from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.errors import AppError
from app.matching import confirm_group
from app.models import Anime, LibraryRoot, MatchGroup, MediaFile, ScrapeCandidate, SourceMapping
from app.scrapers import SCRAPERS, SourceMetadata


class StubScraper:
    async def detail(self, _db: Session, source_id: str) -> SourceMetadata:
        return SourceMetadata(
            source="anidb",
            source_id=source_id,
            title="日本語名",
            episode_titles={"1": "第一話", "2": "第二話"},
        )


async def test_confirm_reuses_anime_with_existing_source_mapping(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setitem(SCRAPERS, "anidb", StubScraper())

    with Session(engine) as db:
        root = LibraryRoot(path="library")
        anime = Anime(title="Existing")
        db.add_all([root, anime])
        db.flush()
        db.add(SourceMapping(anime_id=anime.id, source="anidb", source_id="123"))
        group = MatchGroup(
            library_root_id=root.id,
            group_key="new-episode",
            display_title="Example",
            search_keyword="Example",
        )
        db.add(group)
        db.flush()
        media = MediaFile(
            library_root_id=root.id,
            path="new.mkv",
            relative_path="new.mkv",
            size=1,
            modified_ns=1,
            parsed_title="Example",
            episode=2,
            match_group_id=group.id,
        )
        candidate = ScrapeCandidate(
            match_group_id=group.id,
            source="anidb",
            source_id="123",
            title="Example",
            score=1,
            selected=True,
        )
        db.add_all([media, candidate])
        db.commit()

        result = await confirm_group(db, group)

        assert result.id == anime.id
        assert media.anime_id == anime.id
        assert group.anime_id == anime.id
        assert group.status == "confirmed"
        assert result.episode_titles == {"1": "第一話", "2": "第二話"}
        assert db.query(Anime).count() == 1


async def test_confirm_ignores_candidates_from_disabled_scrapers() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        root = LibraryRoot(path="library")
        db.add(root)
        db.flush()
        group = MatchGroup(
            library_root_id=root.id,
            group_key="disabled-source",
            display_title="Example",
            search_keyword="Example",
        )
        db.add(group)
        db.flush()
        db.add(
            ScrapeCandidate(
                match_group_id=group.id,
                source="getchu",
                source_id="123",
                title="Example",
                score=1,
                selected=True,
            )
        )
        db.commit()

        try:
            await confirm_group(db, group, ["anidb"])
        except AppError as exc:
            assert exc.code == "NO_SELECTION"
        else:
            raise AssertionError("disabled candidate should not be confirmed")
