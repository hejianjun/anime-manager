from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.media_ops as media_ops
from app.database import Base
from app.errors import AppError
from app.media_ops import (
    bind_group_to_anime,
    build_bulk_rename_plan,
    build_rename_plan,
    execute_cached_bulk_rename_plan,
    execute_bulk_rename_plan,
    execute_rename_plan,
    unbind_media_from_anime,
)
from app.models import Anime, LibraryRoot, MatchGroup, MediaFile, ScrapeHistory, TaskRecord
from app.scanner import scan_library


def add_media(
    db: Session,
    root: LibraryRoot,
    path: Path,
    episode: str | int,
    group: MatchGroup | None = None,
) -> MediaFile:
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


def test_unbind_media_keeps_file_and_returns_it_to_pending(tmp_path: Path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        series = tmp_path / "Example"
        series.mkdir()
        first_path = series / "Example E01.mp4"
        second_path = series / "Example E02.mp4"
        first_path.write_bytes(b"first")
        second_path.write_bytes(b"second")
        root = LibraryRoot(path=str(tmp_path))
        anime = Anime(title="Example")
        db.add_all([root, anime])
        db.flush()
        confirmed_group = MatchGroup(
            library_root_id=root.id,
            group_key=f"directory::{str(series.resolve()).casefold()}",
            display_title="Example",
            search_keyword="Example",
            status="confirmed",
            anime_id=anime.id,
        )
        db.add(confirmed_group)
        db.flush()
        first = add_media(db, root, first_path, 1, confirmed_group)
        second = add_media(db, root, second_path, 2, confirmed_group)
        first.anime_id = anime.id
        second.anime_id = anime.id
        db.commit()

        unbind_media_from_anime(db, anime, first)

        assert first.anime_id is None
        assert first.match_group.status == "pending"
        assert first.match_group.anime_id is None
        assert first.match_group_id != confirmed_group.id
        assert second.anime_id == anime.id
        assert second.match_group_id == confirmed_group.id
        assert first_path.read_bytes() == b"first"
        history = db.query(ScrapeHistory).one()
        assert history.anime_id == anime.id
        assert "Example E01.mp4" in history.message
        pending_group_id = first.match_group_id
        task = TaskRecord(kind="scan_library")
        db.add(task)
        db.commit()

        scan_library(db, root.id, task.id)

        assert first.anime_id is None
        assert first.match_group_id == pending_group_id
        assert first.match_group.status == "pending"
        assert second.anime_id == anime.id


def test_bind_group_can_add_only_selected_files_to_existing_anime(tmp_path: Path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        root = LibraryRoot(path=str(tmp_path))
        anime = Anime(title="既存作品")
        db.add_all([root, anime])
        db.flush()
        group = MatchGroup(
            library_root_id=root.id,
            group_key="group",
            display_title="Mixed",
            search_keyword="Mixed",
        )
        db.add(group)
        db.flush()
        first_path = tmp_path / "First.mp4"
        second_path = tmp_path / "Second.mp4"
        first_path.write_bytes(b"first")
        second_path.write_bytes(b"second")
        first = add_media(db, root, first_path, 1, group)
        second = add_media(db, root, second_path, 2, group)
        db.commit()

        bind_group_to_anime(db, group, anime, [first.id])

        assert first.anime_id == anime.id
        assert first.match_group_id is None
        assert second.anime_id is None
        assert second.match_group_id == group.id
        assert group.status == "pending"
        assert group.anime_id is None


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
        assert plan["nfo_create_count"] == 2
        assert plan["cleanup_dirs"] == [
            {
                "source": str(source.parent.resolve()),
                "target": str((tmp_path / ".delete" / "incoming").resolve()),
                "changed": True,
            }
        ]
        assert plan["files"][0]["target"].endswith("作品名 - S01E03 - 公式集标题.mp4")

        result = execute_rename_plan(db, anime, 1)

        target = Path(result["moved"][0])
        assert target.exists()
        assert not source.exists()
        assert media.path == str(target)
        assert media.relative_path == str(target.relative_to(tmp_path))
        assert media.has_nfo is True
        assert anime.has_show_nfo is True
        assert len(result["written_nfos"]) == 2
        assert (target.parent / "tvshow.nfo").exists()
        assert target.with_suffix(".nfo").exists()


def test_rename_plan_accepts_alphanumeric_episode_identifier(tmp_path: Path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        root = LibraryRoot(path=str(tmp_path))
        anime = Anime(title="作品名", episode_titles={"S1": "特别篇"})
        db.add_all([root, anime])
        db.flush()
        source = tmp_path / "incoming" / "Example Special.mkv"
        source.parent.mkdir()
        source.write_bytes(b"video")
        media = add_media(db, root, source, "S1")
        media.anime_id = anime.id
        db.commit()
        db.refresh(anime)

        plan = build_rename_plan(anime, 1)

        assert plan["blockers"] == []
        assert Path(plan["files"][0]["target"]).name == "作品名 - S01ES1 - 特别篇.mkv"


def test_rename_plan_strips_pending_suffix_from_recorded_directory(
    tmp_path: Path,
) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        source_dir = tmp_path / "incoming"
        source_dir.mkdir()
        source = source_dir / "Example E01.mkv"
        source.write_bytes(b"video")
        root = LibraryRoot(path=str(tmp_path))
        anime = Anime(title="Example")
        db.add_all([root, anime])
        db.flush()
        group = MatchGroup(
            library_root_id=root.id,
            group_key=f"directory::{str(source_dir.resolve()).casefold()}::pending",
            display_title="Example",
            search_keyword="Example",
            status="confirmed",
            anime_id=anime.id,
        )
        db.add(group)
        db.flush()
        media = add_media(db, root, source, "1", group)
        media.anime_id = anime.id
        db.commit()
        db.refresh(anime)

        plan = build_rename_plan(anime, 1)

        assert plan["blockers"] == []
        assert Path(plan["files"][0]["target"]).name == "Example - S01E01.mkv"
        assert plan["cleanup_dirs"][0]["source"] == str(source_dir.resolve())


def test_rename_preserves_shared_directory_and_other_anime_files(
    tmp_path: Path,
) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        root = LibraryRoot(path=str(tmp_path))
        first_anime = Anime(title="作品一")
        second_anime = Anime(title="作品二")
        db.add_all([root, first_anime, second_anime])
        db.flush()
        shared = tmp_path / "CHS"
        shared.mkdir()
        first_source = shared / "First E01.mkv"
        second_source = shared / "Second E01.mkv"
        first_source.write_bytes(b"first")
        second_source.write_bytes(b"second")
        target_dir = tmp_path / "作品一"
        target_dir.mkdir()
        (target_dir / "tvshow.nfo").write_text("<tvshow />", encoding="utf-8")
        first_media = add_media(db, root, first_source, 1)
        second_media = add_media(db, root, second_source, 1)
        first_media.anime_id = first_anime.id
        second_media.anime_id = second_anime.id
        db.commit()
        db.refresh(first_anime)

        plan = build_rename_plan(first_anime, 1)

        assert plan["blockers"] == []
        assert [Path(item["source"]) for item in plan["files"]] == [
            first_source,
            shared / "tvshow.nfo",
            first_source.with_suffix(".nfo"),
        ]
        assert plan["cleanup_dirs"] == []
        assert plan["preserved_dirs"] == [
            {
                "source": str(shared),
                "reason": "目录包含本作品计划外的文件，按共享目录保留",
                "examples": [second_source.name],
            }
        ]

        result = execute_rename_plan(db, first_anime, 1)

        assert result["archived_dirs"] == []
        assert shared.is_dir()
        assert second_source.exists()
        assert not first_source.exists()
        assert (target_dir / "作品一 - S01E01.mkv").exists()
        assert (target_dir / "作品一 - S01E01.nfo").exists()


def test_rename_moves_and_renames_video_sidecars(tmp_path: Path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        root = LibraryRoot(path=str(tmp_path))
        anime = Anime(title="作品名")
        db.add_all([root, anime])
        db.flush()
        source = tmp_path / "incoming" / "Example E01.MKV"
        source.parent.mkdir()
        source.write_bytes(b"video")
        sidecars = {
            source.with_suffix(".nfo"): b"nfo",
            source.with_suffix(".srt"): b"subtitle",
            source.with_suffix(".jpg"): b"image",
            source.with_name(f"{source.stem}.ja.srt"): b"japanese subtitle",
            source.with_suffix(".ass"): b"ass subtitle",
            source.with_name(f"{source.stem}.zh.ass"): b"chinese ass subtitle",
            source.with_name(f"{source.stem}-thumb.jpg"): b"thumbnail",
        }
        for path, content in sidecars.items():
            path.write_bytes(content)
        directory_sidecars = {
            source.parent / "poster.jpg": b"poster",
            source.parent / "tvshow.nfo": b"tvshow",
            source.parent / "metadata" / "artwork.jpg": b"metadata artwork",
        }
        for path, content in directory_sidecars.items():
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(content)
        media = add_media(db, root, source, 1)
        media.anime_id = anime.id
        db.commit()
        db.refresh(anime)

        plan = build_rename_plan(anime, 1)

        assert plan["blockers"] == []
        assert sorted(item["kind"] for item in plan["files"]) == [
            "image",
            "image",
            "image",
            "image",
            "nfo",
            "nfo",
            "subtitle",
            "subtitle",
            "subtitle",
            "subtitle",
            "video",
        ]
        assert {Path(item["target"]).name for item in plan["files"]} == {
            "作品名 - S01E01.mkv",
            "作品名 - S01E01.jpg",
            "作品名 - S01E01.srt",
            "作品名 - S01E01.ja.srt",
            "作品名 - S01E01.ass",
            "作品名 - S01E01.zh.ass",
            "作品名 - S01E01.nfo",
            "作品名 - S01E01-thumb.jpg",
            "poster.jpg",
            "tvshow.nfo",
            "artwork.jpg",
        }

        result = execute_rename_plan(db, anime, 1)

        assert len(result["moved"]) == 11
        assert all(Path(path).exists() for path in result["moved"])
        assert all(not path.exists() for path in [source, *sidecars])
        assert all(not path.exists() for path in directory_sidecars)
        assert (tmp_path / "作品名" / "metadata" / "artwork.jpg").exists()
        assert result["archived_dirs"] == [str(tmp_path / ".delete" / "incoming")]
        assert (tmp_path / ".delete" / "incoming").is_dir()
        assert not source.parent.exists()
        assert media.path.endswith("作品名 - S01E01.mkv")


def test_rename_sidecar_target_conflict_blocks_move(tmp_path: Path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        root = LibraryRoot(path=str(tmp_path))
        anime = Anime(title="作品名")
        db.add_all([root, anime])
        db.flush()
        source = tmp_path / "incoming" / "Example E01.mkv"
        source.parent.mkdir()
        source.write_bytes(b"video")
        source.with_suffix(".srt").write_bytes(b"subtitle")
        target_subtitle = tmp_path / "作品名" / "作品名 - S01E01.srt"
        target_subtitle.parent.mkdir()
        target_subtitle.write_bytes(b"existing")
        media = add_media(db, root, source, 1)
        media.anime_id = anime.id
        db.commit()
        db.refresh(anime)

        plan = build_rename_plan(anime, 1)

        assert plan["blockers"] == [f"目标文件已存在: {target_subtitle}"]


def test_rename_blocks_existing_old_folder_in_deletion_directory(tmp_path: Path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        root = LibraryRoot(path=str(tmp_path))
        anime = Anime(title="作品名")
        db.add_all([root, anime])
        db.flush()
        source = tmp_path / "incoming" / "Example E01.mkv"
        source.parent.mkdir()
        source.write_bytes(b"video")
        (tmp_path / ".delete" / "incoming").mkdir(parents=True)
        media = add_media(db, root, source, 1)
        media.anime_id = anime.id
        db.commit()
        db.refresh(anime)

        plan = build_rename_plan(anime, 1)

        assert plan["blockers"] == [
            f".delete 目录中已存在同名文件夹: {tmp_path / '.delete' / 'incoming'}"
        ]


def test_bulk_rename_preview_and_execute_all_anime(tmp_path: Path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        root = LibraryRoot(path=str(tmp_path))
        first = Anime(title="作品一")
        second = Anime(title="作品二")
        db.add_all([root, first, second])
        db.flush()
        first_source = tmp_path / "incoming-first" / "First E01.mkv"
        second_source = tmp_path / "incoming-second" / "Second E02.mkv"
        first_source.parent.mkdir()
        second_source.parent.mkdir()
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
        assert plan["changed_count"] == 6
        assert plan["nfo_create_count"] == 4
        assert plan["blockers"] == []
        assert {item["anime_title"] for item in plan["files"]} == {"作品一", "作品二"}

        result = execute_bulk_rename_plan(db, [first, second], 1)

        assert result["anime_count"] == 2
        assert len(result["moved"]) == 6
        assert all(Path(path).exists() for path in result["moved"])
        assert len(result["archived_dirs"]) == 2
        assert len(result["written_nfos"]) == 4
        assert not first_source.exists()
        assert not second_source.exists()
        assert not first_source.parent.exists()
        assert not second_source.parent.exists()
        assert plan["preserved_dirs"] == []


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


def test_bulk_rename_skips_anime_whose_directory_name_already_matches(
    tmp_path: Path,
) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        root = LibraryRoot(path=str(tmp_path))
        anime = Anime(title="作品名")
        db.add_all([root, anime])
        db.flush()
        directory = tmp_path / "作品名"
        directory.mkdir()
        source = directory / "旧文件名 E01.mkv"
        source.write_bytes(b"episode")
        media = add_media(db, root, source, 1)
        media.anime_id = anime.id
        db.commit()
        db.refresh(anime)

        plan = build_bulk_rename_plan([anime], 1)

        assert plan["anime_count"] == 0
        assert plan["files"] == []
        assert plan["skipped"] == [
            {"anime_id": anime.id, "title": "作品名", "reason": "目录名已一致"}
        ]


def test_bulk_rename_skips_anime_when_library_is_unavailable(tmp_path: Path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    missing_root = tmp_path / "disconnected"
    with Session(engine) as db:
        root = LibraryRoot(path=str(missing_root))
        anime = Anime(title="离线作品")
        db.add_all([root, anime])
        db.flush()
        media = MediaFile(
            library_root_id=root.id,
            anime_id=anime.id,
            path=str(missing_root / "incoming" / "Episode 01.mkv"),
            relative_path="incoming/Episode 01.mkv",
            size=1,
            modified_ns=1,
            parsed_title="离线作品",
            episode=1,
            status="present",
        )
        db.add(media)
        db.commit()
        db.refresh(anime)

        plan = build_bulk_rename_plan([anime], 1)

        assert plan["anime_count"] == 0
        assert plan["blockers"] == []
        assert plan["skipped"] == [
            {
                "anime_id": anime.id,
                "title": "离线作品",
                "reason": f"媒体库不可访问: {missing_root}",
            }
        ]


def test_cached_bulk_rename_revalidates_target_before_moving(tmp_path: Path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        root = LibraryRoot(path=str(tmp_path))
        anime = Anime(title="作品名")
        db.add_all([root, anime])
        db.flush()
        source = tmp_path / "incoming" / "Example E01.mkv"
        source.parent.mkdir()
        source.write_bytes(b"source")
        media = add_media(db, root, source, 1)
        media.anime_id = anime.id
        db.commit()
        db.refresh(anime)
        plan = build_bulk_rename_plan([anime], 1)
        target = Path(plan["files"][0]["target"])
        target.parent.mkdir()
        target.write_bytes(b"new conflict")

        with pytest.raises(AppError) as caught:
            execute_cached_bulk_rename_plan(db, plan)

        assert caught.value.code == "RENAME_PLAN_STALE"
        assert source.exists()
        assert target.read_bytes() == b"new conflict"


def test_cached_bulk_rename_rejects_new_file_in_cleanup_directory(
    tmp_path: Path,
) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        root = LibraryRoot(path=str(tmp_path))
        anime = Anime(title="作品名")
        db.add_all([root, anime])
        db.flush()
        source = tmp_path / "incoming" / "Example E01.mkv"
        source.parent.mkdir()
        source.write_bytes(b"source")
        media = add_media(db, root, source, 1)
        media.anime_id = anime.id
        db.commit()
        db.refresh(anime)
        plan = build_bulk_rename_plan([anime], 1)
        unexpected = source.parent / "Late E02.mkv"
        unexpected.write_bytes(b"late")

        with pytest.raises(AppError) as caught:
            execute_cached_bulk_rename_plan(db, plan)

        assert caught.value.code == "RENAME_PLAN_STALE"
        assert source.exists()
        assert unexpected.exists()
        assert not Path(plan["files"][0]["target"]).exists()


def test_cached_bulk_rename_rolls_back_partial_file_moves(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        root = LibraryRoot(path=str(tmp_path))
        anime = Anime(title="作品名")
        db.add_all([root, anime])
        db.flush()
        first = tmp_path / "incoming" / "Example E01.mkv"
        second = tmp_path / "incoming" / "Example E02.mkv"
        first.parent.mkdir()
        first.write_bytes(b"first")
        second.write_bytes(b"second")
        first_media = add_media(db, root, first, 1)
        second_media = add_media(db, root, second, 2)
        first_media.anime_id = anime.id
        second_media.anime_id = anime.id
        db.commit()
        db.refresh(anime)
        plan = build_bulk_rename_plan([anime], 1)

        from app import media_ops

        original_move = media_ops.shutil.move
        calls = 0

        def fail_second_move(source: str, target: str):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated move failure")
            return original_move(source, target)

        monkeypatch.setattr(media_ops.shutil, "move", fail_second_move)

        with pytest.raises(OSError, match="simulated move failure"):
            execute_cached_bulk_rename_plan(db, plan)

        assert first.exists()
        assert second.exists()
        assert not any(
            Path(item["target"]).exists()
            for item in plan["files"]
            if item["changed"]
        )


def test_bulk_rename_moves_scan_directory_media_into_main_directory(
    tmp_path: Path,
) -> None:
    main_dir = tmp_path / "library"
    scan_dir = tmp_path / "incoming"
    source_dir = scan_dir / "Example"
    main_dir.mkdir()
    source_dir.mkdir(parents=True)
    source = source_dir / "Example E01.mkv"
    source.write_bytes(b"episode")

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        root = LibraryRoot(path=str(main_dir), scan_path=str(scan_dir))
        anime = Anime(title="Example")
        db.add_all([root, anime])
        db.flush()
        stat = source.stat()
        media = MediaFile(
            library_root_id=root.id,
            anime_id=anime.id,
            path=str(source.resolve()),
            relative_path=str(source.relative_to(scan_dir)),
            size=stat.st_size,
            modified_ns=stat.st_mtime_ns,
            parsed_title="Example",
            episode=1,
        )
        db.add(media)
        db.commit()

        plan = build_bulk_rename_plan([anime], 1)
        target = main_dir / "Example" / "Example - S01E01.mkv"

        assert plan["anime_count"] == 1
        assert plan["blockers"] == []
        assert plan["files"][0]["source"] == str(source.resolve())
        assert plan["files"][0]["target"] == str(target.absolute())

        root.scan_path = None
        db.flush()
        with pytest.raises(AppError) as stale:
            execute_cached_bulk_rename_plan(db, plan)
        assert stale.value.code == "RENAME_PLAN_STALE"
        root.scan_path = str(scan_dir)
        db.flush()

        result = execute_cached_bulk_rename_plan(db, plan)

        assert target.read_bytes() == b"episode"
        assert media.path == str(target.absolute())
        assert media.relative_path == str(target.relative_to(main_dir))
        assert result["moved"][0] == str(target.absolute())
        assert set(result["moved"][1:]) == {
            str((main_dir / "Example" / "tvshow.nfo").absolute()),
            str(target.with_suffix(".nfo").absolute()),
        }
        assert len(result["written_nfos"]) == 2
        assert (main_dir / ".delete" / "Example").is_dir()


def test_movie_rename_generates_only_movie_nfo(tmp_path: Path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        root = LibraryRoot(path=str(tmp_path))
        anime = Anime(title="电影作品", media_type="movie")
        db.add_all([root, anime])
        db.flush()
        source = tmp_path / "incoming" / "Movie.mkv"
        source.parent.mkdir()
        source.write_bytes(b"movie")
        media = add_media(db, root, source, 1)
        media.anime_id = anime.id
        db.commit()
        db.refresh(anime)

        plan = build_rename_plan(anime, 1)
        assert plan["nfo_create_count"] == 1
        generated = next(item for item in plan["files"] if item.get("generated"))
        assert generated["nfo_scope"] == "movie"

        result = execute_rename_plan(db, anime, 1)

        target = tmp_path / "电影作品" / "电影作品 - S01E01.mkv"
        assert target.exists()
        assert target.with_suffix(".nfo").exists()
        assert "<movie>" in target.with_suffix(".nfo").read_text(encoding="utf-8")
        assert not (target.parent / "tvshow.nfo").exists()
        assert result["written_nfos"] == [str(target.with_suffix(".nfo"))]
        assert media.has_nfo is True


def test_rename_moves_nfo_before_video(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        root = LibraryRoot(path=str(tmp_path))
        anime = Anime(title="顺序测试")
        db.add_all([root, anime])
        db.flush()
        source = tmp_path / "incoming" / "Episode E01.mkv"
        source.parent.mkdir()
        source.write_bytes(b"video")
        media = add_media(db, root, source, 1)
        media.anime_id = anime.id
        db.commit()
        db.refresh(anime)

        original_move = media_ops.shutil.move

        def checked_move(source_path: str, target_path: str):
            target = Path(target_path)
            if target.suffix == ".mkv":
                assert (target.parent / "tvshow.nfo").exists()
                assert target.with_suffix(".nfo").exists()
            return original_move(source_path, target_path)

        monkeypatch.setattr(media_ops.shutil, "move", checked_move)

        execute_rename_plan(db, anime, 1)


def test_existing_nfos_are_preserved_without_overwrite(tmp_path: Path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        root = LibraryRoot(path=str(tmp_path))
        anime = Anime(title="保留 NFO")
        db.add_all([root, anime])
        db.flush()
        source = tmp_path / "incoming" / "Episode E01.mkv"
        source.parent.mkdir()
        source.write_bytes(b"video")
        source.with_suffix(".nfo").write_text("existing episode", encoding="utf-8")
        (source.parent / "tvshow.nfo").write_text("existing show", encoding="utf-8")
        media = add_media(db, root, source, 1)
        media.anime_id = anime.id
        db.commit()
        db.refresh(anime)

        plan = build_rename_plan(anime, 1)
        assert plan["nfo_create_count"] == 0

        execute_rename_plan(db, anime, 1)

        target_dir = tmp_path / "保留 NFO"
        assert (target_dir / "tvshow.nfo").read_text(encoding="utf-8") == "existing show"
        assert (
            target_dir / "保留 NFO - S01E01.nfo"
        ).read_text(encoding="utf-8") == "existing episode"


def test_bulk_rename_accepts_nfo_created_after_preview(tmp_path: Path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        root = LibraryRoot(path=str(tmp_path))
        anime = Anime(title="竞态测试")
        db.add_all([root, anime])
        db.flush()
        source = tmp_path / "incoming" / "Episode E01.mkv"
        source.parent.mkdir()
        source.write_bytes(b"video")
        media = add_media(db, root, source, 1)
        media.anime_id = anime.id
        db.commit()
        db.refresh(anime)

        plan = build_bulk_rename_plan([anime], 1)
        target_dir = tmp_path / "竞态测试"
        target_dir.mkdir()
        (target_dir / "tvshow.nfo").write_text("nas show", encoding="utf-8")
        target_episode = target_dir / "竞态测试 - S01E01.nfo"
        target_episode.write_text("nas episode", encoding="utf-8")

        result = execute_cached_bulk_rename_plan(db, plan)

        assert (target_dir / "tvshow.nfo").read_text(encoding="utf-8") == "nas show"
        assert target_episode.read_text(encoding="utf-8") == "nas episode"
        assert result["written_nfos"] == []
        assert (target_dir / "竞态测试 - S01E01.mkv").exists()


def test_bulk_rename_regenerates_target_nfo_removed_after_preview(
    tmp_path: Path,
) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        root = LibraryRoot(path=str(tmp_path))
        anime = Anime(title="目标删除竞态")
        db.add_all([root, anime])
        db.flush()
        source = tmp_path / "incoming" / "Episode E01.mkv"
        source.parent.mkdir()
        source.write_bytes(b"video")
        media = add_media(db, root, source, 1)
        media.anime_id = anime.id
        target_dir = tmp_path / "目标删除竞态"
        target_dir.mkdir()
        target_show = target_dir / "tvshow.nfo"
        target_episode = target_dir / "目标删除竞态 - S01E01.nfo"
        target_show.write_text("temporary show", encoding="utf-8")
        target_episode.write_text("temporary episode", encoding="utf-8")
        db.commit()
        db.refresh(anime)

        plan = build_bulk_rename_plan([anime], 1)
        assert plan["nfo_create_count"] == 0
        target_show.unlink()
        target_episode.unlink()

        result = execute_cached_bulk_rename_plan(db, plan)

        assert len(result["written_nfos"]) == 2
        assert target_show.exists()
        assert target_episode.exists()
        assert (target_dir / "目标删除竞态 - S01E01.mkv").exists()


def test_bulk_nfo_failure_skips_only_affected_anime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        root = LibraryRoot(path=str(tmp_path))
        failed = Anime(title="失败作品")
        succeeded = Anime(title="成功作品")
        db.add_all([root, failed, succeeded])
        db.flush()
        failed_source = tmp_path / "failed-source" / "Episode E01.mkv"
        succeeded_source = tmp_path / "succeeded-source" / "Episode E01.mkv"
        failed_source.parent.mkdir()
        succeeded_source.parent.mkdir()
        failed_source.write_bytes(b"failed")
        succeeded_source.write_bytes(b"succeeded")
        failed_media = add_media(db, root, failed_source, 1)
        succeeded_media = add_media(db, root, succeeded_source, 1)
        failed_media.anime_id = failed.id
        succeeded_media.anime_id = succeeded.id
        db.commit()
        db.refresh(failed)
        db.refresh(succeeded)

        plan = build_bulk_rename_plan([failed, succeeded], 1)
        original_write = media_ops._write_missing_nfo

        def fail_episode_nfo(path: Path, content: str) -> bool:
            if path.parent == failed_source.parent and path.name != "tvshow.nfo":
                raise OSError("simulated NFO failure")
            return original_write(path, content)

        monkeypatch.setattr(media_ops, "_write_missing_nfo", fail_episode_nfo)

        result = execute_cached_bulk_rename_plan(db, plan)

        assert result["anime_count"] == 1
        assert any(item["anime_id"] == failed.id for item in result["skipped"])
        assert failed_source.exists()
        assert (failed_source.parent / "tvshow.nfo").exists()
        assert not (tmp_path / "失败作品" / "失败作品 - S01E01.mkv").exists()
        assert not succeeded_source.exists()
        assert (tmp_path / "成功作品" / "成功作品 - S01E01.mkv").exists()


def test_shared_tv_directory_without_target_nfo_is_blocked(tmp_path: Path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        root = LibraryRoot(path=str(tmp_path))
        anime = Anime(title="共享目录作品")
        db.add_all([root, anime])
        db.flush()
        shared = tmp_path / "shared"
        shared.mkdir()
        source = shared / "Episode E01.mkv"
        other = shared / "Other E01.mkv"
        source.write_bytes(b"video")
        other.write_bytes(b"other")
        media = add_media(db, root, source, 1)
        media.anime_id = anime.id
        db.commit()
        db.refresh(anime)

        plan = build_rename_plan(anime, 1)

        assert any("共享" in blocker and "tvshow.nfo" in blocker for blocker in plan["blockers"])
        assert not (shared / "tvshow.nfo").exists()
