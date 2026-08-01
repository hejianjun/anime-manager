from pathlib import Path

import anyio
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import Anime, LibraryRoot, MediaFile, TaskRecord
from app.services import bulk_rename


def task_database():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def add_rename_fixture(db: Session, root_path: Path) -> tuple[Anime, Path]:
    root = LibraryRoot(path=str(root_path))
    anime = Anime(title="作品名")
    db.add_all([root, anime])
    db.flush()
    source = root_path / "incoming" / "Example E01.mkv"
    source.parent.mkdir()
    source.write_bytes(b"episode")
    db.add(
        MediaFile(
            library_root_id=root.id,
            anime_id=anime.id,
            path=str(source),
            relative_path=str(source.relative_to(root_path)),
            size=source.stat().st_size,
            modified_ns=source.stat().st_mtime_ns,
            parsed_title="Example",
            episode=1,
            status="present",
        )
    )
    db.commit()
    return anime, source


def test_bulk_rename_tasks_cache_preview_and_execute_it_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    session_factory = task_database()
    monkeypatch.setattr(bulk_rename, "SessionLocal", session_factory)
    with session_factory() as db:
        _, source = add_rename_fixture(db, tmp_path)
        preview = TaskRecord(
            kind="bulk_rename_preview",
            message="等待生成重命名预览",
            result={"season": 1},
        )
        db.add(preview)
        db.commit()
        preview_id = preview.id

    anyio.run(bulk_rename.run_bulk_rename_preview, preview_id, 1)

    with session_factory() as db:
        preview = db.get(TaskRecord, preview_id)
        assert preview is not None
        assert preview.status == "completed"
        assert preview.result["changed_count"] == 2
        assert preview.result["nfo_create_count"] == 3
        target = Path(preview.result["files"][0]["target"])
        execution = bulk_rename.claim_preview_for_execution(
            db,
            preview_id,
            TaskRecord(
                kind="bulk_rename_execute",
                message="等待执行重命名",
                result={"preview_task_id": preview_id},
            ),
        )
        execution_id = execution.id

    monkeypatch.setattr(
        bulk_rename,
        "build_bulk_rename_plan",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("execution must use the cached preview")
        ),
    )
    anyio.run(bulk_rename.run_bulk_rename_execute, execution_id, preview_id)

    with session_factory() as db:
        execution = db.get(TaskRecord, execution_id)
        assert execution is not None
        assert execution.status == "completed"
        assert execution.result["moved"][0] == str(target)
        assert len(execution.result["moved"]) == 2
        assert len(execution.result["written_nfos"]) == 3
        assert target.exists()
        assert not source.exists()

        second = TaskRecord(
            kind="bulk_rename_execute",
            message="等待执行重命名",
            result={"preview_task_id": preview_id},
        )
        try:
            bulk_rename.claim_preview_for_execution(db, preview_id, second)
        except Exception as exc:
            assert getattr(exc, "code", None) == "RENAME_PREVIEW_ALREADY_USED"
        else:
            raise AssertionError("a preview task must only be claimed once")
