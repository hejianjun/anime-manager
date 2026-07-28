from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.media_ops import bind_group_to_anime, build_rename_plan, execute_rename_plan
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
        assert plan["files"][0]["target"].endswith("作品名 - S01E03 - 集标题.mp4")

        result = execute_rename_plan(db, anime, 1)

        target = Path(result["moved"][0])
        assert target.exists()
        assert not source.exists()
        assert media.path == str(target)
        assert media.relative_path == str(target.relative_to(tmp_path))
