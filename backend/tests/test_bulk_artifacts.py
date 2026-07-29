from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.bulk_artifacts import build_bulk_artifact_plan, execute_bulk_artifact_plan
from app.database import Base
from app.models import Anime, LibraryRoot, MediaFile, TaskRecord


def add_media(
    db: Session,
    root: LibraryRoot,
    anime: Anime,
    path: Path,
    episode: int | None,
) -> MediaFile:
    path.write_bytes(b"video")
    media = MediaFile(
        library_root=root,
        library_root_id=root.id,
        anime=anime,
        anime_id=anime.id,
        path=str(path),
        relative_path=path.name,
        size=5,
        modified_ns=1,
        parsed_title=anime.title,
        episode=episode,
        duration=100,
        status="present",
        has_nfo=False,
        has_episode_image=False,
    )
    db.add(media)
    db.flush()
    return media


def test_bulk_artifact_plan_only_contains_missing_outputs(tmp_path: Path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        root = LibraryRoot(path=str(tmp_path))
        anime = Anime(
            title="Example",
            media_type="OVA",
            has_show_nfo=False,
            cover_url="https://images.example/poster.jpg",
        )
        db.add_all([root, anime])
        db.flush()
        media = add_media(db, root, anime, tmp_path / "Example.mkv", 1)
        db.commit()

        plan = build_bulk_artifact_plan([anime])

        assert plan["anime_count"] == 1
        assert plan["nfo_count"] == 2
        assert plan["poster_count"] == 1
        assert plan["episode_image_count"] == 1
        assert [item["kind"] for item in plan["files"]] == [
            "poster",
            "tvshow_nfo",
            "episode_nfo",
            "episode_image",
        ]
        assert plan["files"][-1]["path"].endswith("Example-thumb.jpg")
        assert media.has_nfo is False


def test_bulk_artifact_execution_writes_and_updates_health(
    tmp_path: Path,
    monkeypatch,
) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        root = LibraryRoot(path=str(tmp_path))
        anime = Anime(
            title="Example",
            media_type="OVA",
            has_show_nfo=False,
            cover_url="https://images.example/poster.jpg",
        )
        task = TaskRecord(kind="bulk_artifacts")
        db.add_all([root, anime, task])
        db.flush()
        media = add_media(db, root, anime, tmp_path / "Example.mkv", 1)
        db.commit()

        def fake_extract(_source: Path, target: Path, _seek: float) -> None:
            target.write_bytes(b"jpg")

        def fake_download(_source: str, target: Path) -> None:
            target.write_bytes(b"poster")

        monkeypatch.setattr("app.bulk_artifacts._extract_episode_image", fake_extract)
        monkeypatch.setattr("app.bulk_artifacts._download_poster", fake_download)
        monkeypatch.setattr("app.bulk_artifacts.shutil.which", lambda _name: "ffmpeg")

        execute_bulk_artifact_plan(db, [anime], task)

        assert task.status == "completed"
        assert len(task.result["written"]) == 4
        assert (tmp_path / "poster.jpg").read_bytes() == b"poster"
        assert (tmp_path / "tvshow.nfo").exists()
        assert (tmp_path / "Example.nfo").exists()
        assert (tmp_path / "Example-thumb.jpg").exists()
        assert anime.has_show_nfo is True
        assert media.has_nfo is True
        assert media.has_episode_image is True


def test_bulk_artifact_plan_does_not_overwrite_existing_poster(tmp_path: Path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        root = LibraryRoot(path=str(tmp_path))
        anime = Anime(
            title="Example",
            media_type="OVA",
            has_show_nfo=True,
            cover_url="https://images.example/poster.jpg",
        )
        db.add_all([root, anime])
        db.flush()
        media = add_media(db, root, anime, tmp_path / "Example.mkv", 1)
        media.has_nfo = True
        media.has_episode_image = True
        (tmp_path / "poster.jpg").write_bytes(b"existing")
        db.commit()

        plan = build_bulk_artifact_plan([anime])

        assert plan["poster_count"] == 0
        assert plan["files"] == []
        assert (tmp_path / "poster.jpg").read_bytes() == b"existing"
