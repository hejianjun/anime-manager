from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.errors import AppError
from app.main import _run_bulk_search_confirm, _task_event_stream
from app.matching import confirm_group
from app.models import (
    Anime,
    LibraryRoot,
    MatchGroup,
    MediaFile,
    ScrapeCandidate,
    SourceMapping,
    TaskRecord,
)
from app.schemas import BulkSearchConfirmRequest
from app.scrapers import SCRAPERS, Candidate, SourceMetadata


class StubScraper:
    async def detail(self, _db: Session, source_id: str) -> SourceMetadata:
        return SourceMetadata(
            source="anidb",
            source_id=source_id,
            title="日本語名",
            episode_titles={"1": "第一話", "2": "第二話"},
        )


class ExactSearchStub(StubScraper):
    def __init__(self) -> None:
        self.progress_updates: list[tuple[float, str]] = []

    async def search(self, db: Session, keyword: str) -> list[Candidate]:
        task = db.query(TaskRecord).filter_by(kind="bulk_search_confirm").one()
        self.progress_updates.append((task.progress, task.message))
        score = 1 if keyword == "Exact" else 0.99
        return [
            Candidate(
                source="anidb",
                source_id=keyword,
                title=keyword,
                score=score,
            )
        ]


async def test_bulk_search_confirms_only_exact_matches(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    scraper = ExactSearchStub()
    monkeypatch.setitem(SCRAPERS, "anidb", scraper)

    with Session(engine) as db:
        root = LibraryRoot(path="library")
        db.add(root)
        db.flush()
        for index, title in enumerate(("Exact", "Near"), start=1):
            group = MatchGroup(
                library_root_id=root.id,
                group_key=title,
                display_title=title,
                search_keyword=title,
            )
            db.add(group)
            db.flush()
            db.add(
                MediaFile(
                    library_root_id=root.id,
                    path=f"{title}.mkv",
                    relative_path=f"{title}.mkv",
                    size=1,
                    modified_ns=index,
                    parsed_title=title,
                    episode=index,
                    match_group_id=group.id,
                )
            )
        db.commit()

        task = TaskRecord(kind="bulk_search_confirm")
        db.add(task)
        db.commit()
        task_id = task.id

    await _run_bulk_search_confirm(
        task_id,
        BulkSearchConfirmRequest(sources=["anidb"]).sources,
        lambda: Session(engine),
    )

    with Session(engine) as db:
        task = db.get(TaskRecord, task_id)
        assert task.status == "completed"
        assert task.progress == 1
        assert task.result["searched"] == 2
        assert task.result["confirmed"] == 1
        assert task.result["skipped"] == 1
        assert [item[0] for item in scraper.progress_updates] == [0, 0.5]
        assert scraper.progress_updates[0][1].startswith("正在匹配 1/2：")
        assert scraper.progress_updates[1][1].startswith("正在匹配 2/2：")
        assert {
            item[1].split("：", maxsplit=1)[1] for item in scraper.progress_updates
        } == {"Exact", "Near"}
        assert db.query(MatchGroup).filter_by(status="confirmed").one().display_title == "Exact"
        assert db.query(MatchGroup).filter_by(status="pending").one().display_title == "Near"


async def test_task_event_stream_sends_terminal_snapshot() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        task = TaskRecord(
            kind="bulk_search_confirm",
            status="completed",
            progress=1,
            message="批量匹配完成，共处理 2 个",
            result={"searched": 2},
        )
        db.add(task)
        db.commit()
        task_id = task.id

    stream = _task_event_stream(task_id, lambda: Session(engine))
    event = await anext(stream)

    assert event.startswith("data: ")
    assert '"status": "completed"' in event
    assert '"progress": 1.0' in event
    try:
        await anext(stream)
    except StopAsyncIteration:
        pass
    else:
        raise AssertionError("terminal SSE stream should close after its snapshot")


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


async def test_confirm_can_bind_only_selected_files(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setitem(SCRAPERS, "anidb", StubScraper())

    with Session(engine) as db:
        root = LibraryRoot(path="library")
        db.add(root)
        db.flush()
        group = MatchGroup(
            library_root_id=root.id,
            group_key="mixed-folder",
            display_title="Mixed",
            search_keyword="Selected title",
        )
        db.add(group)
        db.flush()
        selected_file = MediaFile(
            library_root_id=root.id,
            path="selected.mkv",
            relative_path="selected.mkv",
            size=1,
            modified_ns=1,
            parsed_title="Selected title",
            episode=1,
            match_group_id=group.id,
        )
        remaining_file = MediaFile(
            library_root_id=root.id,
            path="remaining.mkv",
            relative_path="remaining.mkv",
            size=1,
            modified_ns=1,
            parsed_title="Other title",
            episode=1,
            match_group_id=group.id,
        )
        candidate = ScrapeCandidate(
            match_group_id=group.id,
            source="anidb",
            source_id="123",
            title="Selected title",
            score=1,
            selected=True,
        )
        db.add_all([selected_file, remaining_file, candidate])
        db.commit()

        anime = await confirm_group(db, group, file_ids=[selected_file.id])

        assert selected_file.anime_id == anime.id
        assert selected_file.match_group_id is None
        assert remaining_file.anime_id is None
        assert remaining_file.match_group_id == group.id
        assert group.status == "pending"
        assert group.anime_id is None


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
