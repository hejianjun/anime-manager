from pathlib import Path

from app.catalog_health import catalog_health
from app.models import Anime, LibraryRoot, MediaFile


def media(root: LibraryRoot, path: Path, episode: int) -> MediaFile:
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


def test_catalog_health_reports_directory_nfo_and_episode_images(tmp_path: Path) -> None:
    source = tmp_path / "旧目录"
    source.mkdir()
    first = source / "Example - S01E01.mkv"
    second = source / "Example - S01E02.mkv"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    first.with_suffix(".nfo").write_text("<episodedetails />", encoding="utf-8")
    (source / "Example - S01E01-thumb.jpg").write_bytes(b"image")

    root = LibraryRoot(id=1, path=str(tmp_path))
    anime = Anime(id=1, title="Example", media_type="OVA")
    first_media = media(root, first, 1)
    first_media.has_nfo = True
    first_media.has_episode_image = True
    second_media = media(root, second, 2)
    second_media.has_nfo = False
    second_media.has_episode_image = False
    anime.has_show_nfo = False
    anime.files = [first_media, second_media]

    assert catalog_health(anime) == {
        "directory_name_mismatch": True,
        "missing_nfo_count": 2,
        "missing_episode_image_count": 1,
    }


def test_movie_does_not_require_tvshow_nfo_or_episode_image(tmp_path: Path) -> None:
    source = tmp_path / "Movie"
    source.mkdir()
    video = source / "Movie.mkv"
    video.write_bytes(b"movie")
    video.with_suffix(".nfo").write_text("<movie />", encoding="utf-8")

    root = LibraryRoot(id=1, path=str(tmp_path))
    anime = Anime(id=1, title="Movie", media_type="Movie")
    movie_media = media(root, video, 1)
    movie_media.has_nfo = True
    movie_media.has_episode_image = False
    anime.files = [movie_media]

    assert catalog_health(anime) == {
        "directory_name_mismatch": False,
        "missing_nfo_count": 0,
        "missing_episode_image_count": 0,
    }
