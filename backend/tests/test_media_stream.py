from pathlib import Path

import pytest

from app.errors import AppError
from app.main import _resolve_media_stream_path
from app.models import LibraryRoot, MediaFile


def media(root: LibraryRoot, path: Path, status: str = "present") -> MediaFile:
    item = MediaFile(
        path=str(path),
        relative_path=path.name,
        size=path.stat().st_size if path.exists() else 0,
        modified_ns=path.stat().st_mtime_ns if path.exists() else 0,
        parsed_title=path.stem,
        status=status,
    )
    item.library_root = root
    return item


def test_resolve_media_stream_path_accepts_file_inside_library(tmp_path: Path) -> None:
    video = tmp_path / "Example.mp4"
    video.write_bytes(b"video")
    root = LibraryRoot(path=str(tmp_path))

    assert _resolve_media_stream_path(media(root, video)) == video.resolve()


def test_resolve_media_stream_path_rejects_file_outside_library(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    video = tmp_path / "outside.mp4"
    video.write_bytes(b"video")
    root = LibraryRoot(path=str(library))

    with pytest.raises(AppError) as exc:
        _resolve_media_stream_path(media(root, video))

    assert exc.value.code == "MEDIA_UNAVAILABLE"


def test_resolve_media_stream_path_rejects_missing_status(tmp_path: Path) -> None:
    video = tmp_path / "Example.mp4"
    video.write_bytes(b"video")
    root = LibraryRoot(path=str(tmp_path))

    with pytest.raises(AppError) as exc:
        _resolve_media_stream_path(media(root, video, status="missing"))

    assert exc.value.code == "MEDIA_UNAVAILABLE"
