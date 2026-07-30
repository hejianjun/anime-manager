from pathlib import Path

from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import SessionLocal, get_db
from ..errors import AppError
from ..library_paths import paths_overlap
from ..models import LibraryRoot, TaskRecord
from ..scanner import scan_library
from ..schemas import LibraryRootCreate, LibraryRootOut, LibraryRootPatch, TaskOut

router = APIRouter(prefix="/api/library-roots", tags=["libraries"])


def _directory(value: str, label: str) -> Path:
    try:
        path = Path(value).expanduser().resolve(strict=True)
    except OSError as error:
        raise AppError("INVALID_LIBRARY_ROOT", f"{label}不可访问: {error}")
    if not path.is_dir():
        raise AppError("INVALID_LIBRARY_ROOT", f"{label}不是目录")
    return path


def _validate_paths(main_path: Path, scan_path: Path | None) -> None:
    if scan_path and paths_overlap(main_path, scan_path):
        raise AppError(
            "OVERLAPPING_LIBRARY_ROOTS",
            "主目录和扫描目录不能相同或互相包含",
            status_code=409,
        )


@router.get("", response_model=list[LibraryRootOut])
def list_roots(db: Session = Depends(get_db)):
    return db.scalars(select(LibraryRoot).order_by(LibraryRoot.id)).all()


@router.post("", response_model=LibraryRootOut, status_code=201)
def create_root(payload: LibraryRootCreate, db: Session = Depends(get_db)):
    path = _directory(payload.path, "主目录")
    scan_path = _directory(payload.scan_path, "扫描目录") if payload.scan_path else None
    _validate_paths(path, scan_path)
    existing = db.scalar(select(LibraryRoot).where(LibraryRoot.path == str(path)))
    if existing:
        return existing
    root = LibraryRoot(
        path=str(path),
        scan_path=str(scan_path) if scan_path else None,
        enabled=payload.enabled,
    )
    db.add(root)
    db.commit()
    db.refresh(root)
    return root


@router.patch("/{root_id}", response_model=LibraryRootOut)
def patch_root(
    root_id: int,
    payload: LibraryRootPatch,
    db: Session = Depends(get_db),
):
    root = db.get(LibraryRoot, root_id)
    if not root:
        raise AppError("NOT_FOUND", "媒体库不存在", status_code=404)
    main_path = Path(root.path)
    scan_path = Path(root.scan_path) if root.scan_path else None
    if payload.path is not None:
        candidate = Path(payload.path).expanduser().absolute()
        if candidate != main_path.absolute():
            main_path = _directory(payload.path, "主目录")
    if "scan_path" in payload.model_fields_set:
        if payload.scan_path:
            candidate = Path(payload.scan_path).expanduser().absolute()
            if scan_path is None or candidate != scan_path.absolute():
                scan_path = _directory(payload.scan_path, "扫描目录")
        else:
            scan_path = None
    _validate_paths(main_path, scan_path)
    root.path = str(main_path)
    root.scan_path = str(scan_path) if scan_path else None
    if payload.enabled is not None:
        root.enabled = payload.enabled
    db.commit()
    db.refresh(root)
    return root


def run_scan(root_id: int, task_id: int, source: str) -> None:
    with SessionLocal() as db:
        scan_library(db, root_id, task_id, source)


@router.post("/{root_id}/scan", response_model=TaskOut, status_code=202)
def start_scan(
    root_id: int,
    background: BackgroundTasks,
    source: Literal["main", "scan"] = "main",
    db: Session = Depends(get_db),
):
    root = db.get(LibraryRoot, root_id)
    if not root:
        raise AppError("NOT_FOUND", "媒体库不存在", status_code=404)
    if source == "scan" and not root.scan_path:
        raise AppError("SCAN_PATH_NOT_CONFIGURED", "尚未配置扫描目录", status_code=409)
    label = "主目录" if source == "main" else "扫描目录"
    task = TaskRecord(kind="scan_library", message=f"等待扫描{label}")
    db.add(task)
    db.commit()
    db.refresh(task)
    background.add_task(run_scan, root.id, task.id, source)
    return task
