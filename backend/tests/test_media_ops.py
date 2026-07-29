from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.errors import AppError
from app.media_ops import (
    bind_group_to_anime,
    build_bulk_rename_plan,
    build_rename_plan,
    execute_cached_bulk_rename_plan,
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
        first_media = add_media(db, root, first_source, 1)
        second_media = add_media(db, root, second_source, 1)
        first_media.anime_id = first_anime.id
        second_media.anime_id = second_anime.id
        db.commit()
        db.refresh(first_anime)

        plan = build_rename_plan(first_anime, 1)

        assert [Path(item["source"]) for item in plan["files"]] == [first_source]
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
        assert (tmp_path / "作品一" / "作品一 - S01E01.mkv").exists()


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
        assert result["archived_dirs"] == []
        assert not first_source.exists()
        assert not second_source.exists()
        assert first_source.parent.is_dir()
        assert plan["preserved_dirs"] == [
            {
                "source": str(first_source.parent),
                "reason": "目录包含本作品计划外的文件，按共享目录保留",
                "examples": [second_source.name],
                "anime_id": first.id,
                "anime_title": first.title,
                "library_root": str(tmp_path),
            }
        ]


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
