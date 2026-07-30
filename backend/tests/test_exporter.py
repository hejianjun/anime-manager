from pathlib import Path

import pytest

from app.errors import AppError
from app.exporter import build_export_plan
from app.models import Anime, LibraryRoot, MediaFile


def media(root: LibraryRoot, path: Path, episode: str | None) -> MediaFile:
    return MediaFile(
        library_root=root,
        library_root_id=1,
        path=str(path),
        relative_path=path.name,
        size=10,
        modified_ns=1,
        parsed_title="Example",
        episode=episode,
        status="present",
    )


def test_tvshow_plan_contains_show_and_episode_nfo(tmp_path: Path) -> None:
    root = LibraryRoot(id=1, path=str(tmp_path))
    anime = Anime(id=1, title="Example", media_type="OVA")
    anime.episode_titles = {"1": "Episode title"}
    anime.files = [media(root, tmp_path / "Example 01.mkv", "1")]
    plan = build_export_plan(anime)
    assert plan["mode"] == "tvshow"
    assert [item["kind"] for item in plan["files"]] == ["tvshow", "episode"]
    assert "<title>Episode title</title>" in plan["files"][1]["content"]
    assert not plan["blockers"]


def test_missing_episode_blocks_tv_export(tmp_path: Path) -> None:
    root = LibraryRoot(id=1, path=str(tmp_path))
    anime = Anime(id=1, title="Example", media_type="TV Series")
    anime.files = [media(root, tmp_path / "Example.mkv", None)]
    plan = build_export_plan(anime)
    assert plan["blockers"] == ["Example.mkv 缺少集号"]


def test_alphanumeric_episode_uses_anidb_title_in_nfo(tmp_path: Path) -> None:
    root = LibraryRoot(id=1, path=str(tmp_path))
    anime = Anime(id=1, title="Example", media_type="OVA")
    anime.episode_titles = {"S1": "Special title"}
    anime.files = [media(root, tmp_path / "Example S1.mkv", "S1")]

    plan = build_export_plan(anime)

    content = plan["files"][1]["content"]
    assert "<title>Special title</title>" in content
    assert "<episode>S1</episode>" in content
    assert not plan["blockers"]


def test_export_rejects_path_outside_root(tmp_path: Path) -> None:
    root = LibraryRoot(id=1, path=str(tmp_path / "library"))
    anime = Anime(id=1, title="Example", media_type="Movie")
    anime.files = [media(root, tmp_path / "outside.mkv", "1")]
    with pytest.raises(AppError) as caught:
        build_export_plan(anime)
    assert caught.value.code == "PATH_OUTSIDE_LIBRARY"
