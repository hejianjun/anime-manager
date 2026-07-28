from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.media_ops import (
    bind_group_to_anime,
    build_bulk_rename_plan,
    build_rename_plan,
    execute_bulk_rename_plan,
    execute_rename_plan,
)
from app.models import Anime, LibraryRoot, MatchGroup, MediaFile


def add_media(db: Session, root: LibraryRoot, path: Path, episode: int, group: MatchGroup | None = None) -> MediaFile:
    media = MediaFile(
        library_root_id=root.id,
        path=str(path.resolve()),
        relative_path=str(path.relative_to(Path(root.path))),
        size=path.stat().st_size,
        modified_ns=path.stat().st_mtime_ns,
        parsed_title="Example",
        episode=episode,
        match_group_id=group.id if group else None,
    )
    db.add(media)
    return media


def test_bind_group_adds_files_to_existing_anime(tmp_path: Path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        root = LibraryRoot(path=str(tmp_path))
        anime = Anime(title="既存作品")
        db.add_all([root, anime])
        db.flush()
        group = MatchGroup(library_root_id=root.id, group_key="group", display_title="Example", search_keyword="Example")
        db.add(group)
        db.flush()
        path = tmp_path / "Example E02.mp4"
        path.write_bytes(b"video")
        media = add_media(db, root, path, 2, group)
        db.commit()

        bind_group_to_anime(db, group, anime)

        assert media.anime_id == anime.id
        assert group.status == "confirmed"
        assert db.query(Anime).count() == 1


def test_rename_plan_and_execute_move_files(tmp_path: Path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        root = LibraryRoot(path=str(tmp_path))
        anime = Anime(title="作品名")
        anime.episode_titles = {"3": "公式集标题"}
        db.add_all([root, anime])
        db.flush()
        source = tmp_path / "incoming" / "Example - S01E03 - 集标题.MP4"
        source.parent.mkdir()
        source.write_bytes(b"video")
        media = add_media(db, root, source, 3)
        media.anime_id = anime.id
        db.commit()
        db.refresh(anime)

        plan = build_rename_plan(anime, 1)
        assert plan["blockers"] == []
        assert plan["files"][0]["target"].endswith("作品名 - S01E03 - 公式集标题.mp4")

        result = execute_rename_plan(db, anime, 1)

        target = Path(result["moved"][0])
        assert target.exists()
        assert not source.exists()
        assert media.path == str(target)
        assert media.relative_path == str(target.relative_to(tmp_path))


def test_bulk_rename_preview_and_execute_all_anime(tmp_path: Path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        root = LibraryRoot(path=str(tmp_path))
        first = Anime(title="作品一")
        second = Anime(title="作品二")
        db.add_all([root, first, second])
        db.flush()
        first_source = tmp_path / "incoming" / "First E01.mkv"
        second_source = tmp_path / "incoming" / "Second E02.mkv"
        first_source.parent.mkdir()
        first_source.write_bytes(b"first")
        second_source.write_bytes(b"second")
        first_media = add_media(db, root, first_source, 1)
        second_media = add_media(db, root, second_source, 2)
        first_media.anime_id = first.id
        second_media.anime_id = second.id
        db.commit()
        db.refresh(first)
        db.refresh(second)

        plan = build_bulk_rename_plan([first, second], 1)
        assert plan["anime_count"] == 2
        assert plan["changed_count"] == 2
        assert plan["blockers"] == []
        assert {item["anime_title"] for item in plan["files"]} == {"作品一", "作品二"}

        result = execute_bulk_rename_plan(db, [first, second], 1)

        assert result["anime_count"] == 2
        assert len(result["moved"]) == 2
        assert all(Path(path).exists() for path in result["moved"])
        assert not first_source.exists()
        assert not second_source.exists()


def test_bulk_rename_skips_anime_without_present_files(tmp_path: Path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        anime = Anime(title="没有文件")
        db.add(anime)
        db.commit()
        db.refresh(anime)

        plan = build_bulk_rename_plan([anime], 1)

        assert plan["anime_count"] == 0
        assert plan["files"] == []
        assert plan["blockers"] == []
        assert plan["skipped"] == [
            {"anime_id": anime.id, "title": "没有文件", "reason": "没有可用媒体文件"}
        ]
