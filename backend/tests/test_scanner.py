from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Anime, LibraryRoot, MatchGroup, MediaFile, TaskRecord
from app.scanner import (
    content_hash,
    folder_search_keyword,
    migrate_pending_folder_search_keywords,
    scan_library,
)


def test_content_hash_is_stable_after_rename(tmp_path: Path) -> None:
    first = tmp_path / "first.mkv"
    first.write_bytes(b"same media bytes")
    digest = content_hash(first)
    second = tmp_path / "renamed.mkv"
    first.rename(second)
    assert content_hash(second) == digest


def test_folder_search_keyword_removes_year_but_keeps_title() -> None:
    assert folder_search_keyword("葬送のフリーレン (2023)") == "葬送のフリーレン"
    assert folder_search_keyword("[2024] ダンダダン") == "ダンダダン"
    assert folder_search_keyword("86―エイティシックス―") == "86―エイティシックス―"


def test_migrate_pending_folder_keywords_preserves_manual_edits(
    tmp_path: Path,
) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        root = LibraryRoot(path=str(tmp_path))
        db.add(root)
        db.flush()
        default_group = MatchGroup(
            library_root_id=root.id,
            group_key=f"directory::{str(tmp_path / 'Title (2024)').casefold()}",
            display_title="Title (2024)",
            search_keyword="Title (2024)",
        )
        manual_group = MatchGroup(
            library_root_id=root.id,
            group_key=f"directory::{str(tmp_path / 'Other (2023)').casefold()}",
            display_title="Other (2023)",
            search_keyword="手工搜索词 2023",
        )
        db.add_all([default_group, manual_group])
        db.flush()

        assert migrate_pending_folder_search_keywords(db) == 1
        assert default_group.search_keyword == "Title"
        assert manual_group.search_keyword == "手工搜索词 2023"


def test_scan_merges_pending_files_in_same_directory(tmp_path: Path, monkeypatch) -> None:
    series = tmp_path / "OVAスペエルフ探訪記"
    series.mkdir()
    first = series / "OVAスペエルフ探訪記 #1.mp4"
    second = series / "OVAスペエルフ探訪記 #2.mp4"
    first.write_bytes(b"first")
    second.write_bytes(b"second")

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    probed: list[Path] = []
    monkeypatch.setattr(
        "app.scanner.probe_media",
        lambda path: (probed.append(path) or {}, None),
    )

    with Session(engine) as db:
        root = LibraryRoot(path=str(tmp_path))
        db.add(root)
        db.flush()
        old_groups = [
            MatchGroup(
                library_root_id=root.id,
                group_key=f"legacy::{number}",
                display_title=f"OVAスペエルフ探訪記 #{number}",
                search_keyword=f"OVAスペエルフ探訪記 #{number}",
            )
            for number in (1, 2)
        ]
        db.add_all(old_groups)
        db.flush()
        for path, group in zip((first, second), old_groups, strict=True):
            stat = path.stat()
            db.add(
                MediaFile(
                    library_root_id=root.id,
                    path=str(path.resolve()),
                    relative_path=str(path.relative_to(tmp_path)),
                    size=stat.st_size,
                    modified_ns=stat.st_mtime_ns,
                    content_hash=content_hash(path),
                    parsed_title=path.stem,
                    match_group_id=group.id,
                )
            )
        task = TaskRecord(kind="scan_library")
        db.add(task)
        db.commit()

        scan_library(db, root.id, task.id)

        groups = db.query(MatchGroup).filter_by(status="pending").all()
        assert len(groups) == 1
        assert groups[0].display_title == series.name
        assert groups[0].search_keyword == series.name
        assert {item.path for item in groups[0].files} == {
            str(first.resolve()),
            str(second.resolve()),
        }
        assert {item.episode for item in groups[0].files} == {"1", "2"}
        assert db.get(TaskRecord, task.id).result["merged_groups"] == 1
        assert probed == []


def test_scan_removes_folder_year_from_default_search_keyword(
    tmp_path: Path, monkeypatch
) -> None:
    series = tmp_path / "葬送のフリーレン [2023]"
    series.mkdir()
    media_path = series / "葬送のフリーレン - 01.mp4"
    media_path.write_bytes(b"episode")

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr("app.scanner.probe_media", lambda _path: ({}, None))

    with Session(engine) as db:
        root = LibraryRoot(path=str(tmp_path))
        task = TaskRecord(kind="scan_library")
        db.add_all([root, task])
        db.commit()

        scan_library(db, root.id, task.id)

        group = db.query(MatchGroup).one()
        assert group.display_title == "葬送のフリーレン [2023]"
        assert group.search_keyword == "葬送のフリーレン"


def test_scan_supports_rmvb_files_case_insensitively(
    tmp_path: Path, monkeypatch
) -> None:
    media_path = tmp_path / "Example - 01.RMVB"
    media_path.write_bytes(b"rmvb episode")

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr("app.scanner.probe_media", lambda _path: ({}, None))

    with Session(engine) as db:
        root = LibraryRoot(path=str(tmp_path))
        task = TaskRecord(kind="scan_library")
        db.add_all([root, task])
        db.commit()

        scan_library(db, root.id, task.id)

        scanned = db.query(MediaFile).one()
        assert scanned.path == str(media_path.resolve())
        assert scanned.relative_path == media_path.name
        assert db.get(TaskRecord, task.id).result["found"] == 1


def test_scan_ignores_deletion_directory(tmp_path: Path, monkeypatch) -> None:
    active = tmp_path / "active"
    deleted = tmp_path / ".delete" / "old-series"
    active.mkdir()
    deleted.mkdir(parents=True)
    active_video = active / "Active E01.mp4"
    deleted_video = deleted / "Deleted E01.mp4"
    active_video.write_bytes(b"active")
    deleted_video.write_bytes(b"deleted")

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr("app.scanner.probe_media", lambda _path: ({}, None))

    with Session(engine) as db:
        root = LibraryRoot(path=str(tmp_path))
        task = TaskRecord(kind="scan_library")
        db.add_all([root, task])
        db.commit()

        scan_library(db, root.id, task.id)

        files = db.query(MediaFile).all()
        assert [item.path for item in files] == [str(active_video.resolve())]


def test_scan_records_nfo_and_episode_image_health(tmp_path: Path, monkeypatch) -> None:
    series = tmp_path / "Example"
    series.mkdir()
    video = series / "Example - S01E01.mkv"
    video.write_bytes(b"episode")
    video.with_suffix(".nfo").write_text("<episodedetails />", encoding="utf-8")
    (series / "Example - S01E01-thumb.jpg").write_bytes(b"image")
    (series / "tvshow.nfo").write_text("<tvshow />", encoding="utf-8")

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr("app.scanner.probe_media", lambda _path: ({}, None))

    with Session(engine) as db:
        root = LibraryRoot(path=str(tmp_path))
        anime = Anime(title="Example", media_type="OVA")
        task = TaskRecord(kind="scan_library")
        db.add_all([root, anime, task])
        db.flush()
        stat = video.stat()
        db.add(
            MediaFile(
                library_root_id=root.id,
                anime_id=anime.id,
                path=str(video.resolve()),
                relative_path=str(video.relative_to(tmp_path)),
                size=stat.st_size,
                modified_ns=stat.st_mtime_ns,
                content_hash=content_hash(video),
                parsed_title="Example",
                episode=1,
            )
        )
        db.commit()

        scan_library(db, root.id, task.id)

        scanned = db.query(MediaFile).one()
        assert scanned.has_nfo is True
        assert scanned.has_episode_image is True
        assert db.get(Anime, anime.id).has_show_nfo is True


def test_main_and_scan_directories_are_scanned_independently(
    tmp_path: Path, monkeypatch
) -> None:
    main_dir = tmp_path / "library"
    scan_dir = tmp_path / "incoming"
    main_dir.mkdir()
    scan_dir.mkdir()
    main_video = main_dir / "Main E01.mp4"
    scan_video = scan_dir / "Incoming E01.mp4"
    main_video.write_bytes(b"main")
    scan_video.write_bytes(b"incoming")

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr("app.scanner.probe_media", lambda _path: ({}, None))

    with Session(engine) as db:
        root = LibraryRoot(path=str(main_dir), scan_path=str(scan_dir))
        main_task = TaskRecord(kind="scan_library")
        scan_task = TaskRecord(kind="scan_library")
        db.add_all([root, main_task, scan_task])
        db.commit()

        scan_library(db, root.id, main_task.id, "main")
        assert {item.path for item in db.query(MediaFile).all()} == {
            str(main_video.resolve())
        }

        scan_library(db, root.id, scan_task.id, "scan")
        files = db.query(MediaFile).all()
        assert {item.path for item in files if item.status == "present"} == {
            str(main_video.resolve()),
            str(scan_video.resolve()),
        }
        assert root.last_scan_at is not None
        assert root.scan_last_scan_at is not None
        assert db.get(TaskRecord, scan_task.id).result["source"] == "scan"

        scan_video.unlink()
        rescan_task = TaskRecord(kind="scan_library")
        db.add(rescan_task)
        db.commit()
        scan_library(db, root.id, rescan_task.id, "scan")

        statuses = {Path(item.path).name: item.status for item in db.query(MediaFile)}
        assert statuses == {
            main_video.name: "present",
            scan_video.name: "missing",
        }
