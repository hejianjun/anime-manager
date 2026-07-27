from pathlib import Path

from app.scanner import content_hash


def test_content_hash_is_stable_after_rename(tmp_path: Path) -> None:
    first = tmp_path / "first.mkv"
    first.write_bytes(b"same media bytes")
    digest = content_hash(first)
    second = tmp_path / "renamed.mkv"
    first.rename(second)
    assert content_hash(second) == digest

