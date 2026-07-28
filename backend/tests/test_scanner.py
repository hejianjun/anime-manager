from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import LibraryRoot, MatchGroup, MediaFile, TaskRecord
from app.scanner import content_hash, scan_library


def test_content_hash_is_stable_after_rename(tmp_path: Path) -> None:
    first = tmp_path / "first.mkv"
    first.write_bytes(b"same media bytes")
    digest = content_hash(first)
    second = tmp_path / "renamed.mkv"
    first.rename(second)
    assert content_hash(second) == digest


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
        assert {item.path for item in groups[0].files} == {
            str(first.resolve()),
            str(second.resolve()),
        }
        assert db.get(TaskRecord, task.id).result["merged_groups"] == 1
        assert probed == []
