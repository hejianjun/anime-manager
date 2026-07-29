from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import SessionLocal, get_db
from ..errors import AppError
from ..models import LibraryRoot, TaskRecord
from ..scanner import scan_library
from ..schemas import LibraryRootCreate, LibraryRootOut, LibraryRootPatch, TaskOut

router = APIRouter(prefix="/api/library-roots", tags=["libraries"])


@router.get("", response_model=list[LibraryRootOut])
def list_roots(db: Session = Depends(get_db)):
    return db.scalars(select(LibraryRoot).order_by(LibraryRoot.id)).all()


@router.post("", response_model=LibraryRootOut, status_code=201)
def create_root(payload: LibraryRootCreate, db: Session = Depends(get_db)):
    path = Path(payload.path).expanduser().resolve(strict=True)
    if not path.is_dir():
        raise AppError("INVALID_LIBRARY_ROOT", "媒体库路径不是目录")
    existing = db.scalar(select(LibraryRoot).where(LibraryRoot.path == str(path)))
    if existing:
        return existing
    root = LibraryRoot(path=str(path), enabled=payload.enabled)
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
    if payload.path is not None:
        path = Path(payload.path).expanduser().resolve(strict=True)
        if not path.is_dir():
            raise AppError("INVALID_LIBRARY_ROOT", "媒体库路径不是目录")
        root.path = str(path)
    if payload.enabled is not None:
        root.enabled = payload.enabled
    db.commit()
    db.refresh(root)
    return root


def run_scan(root_id: int, task_id: int) -> None:
    with SessionLocal() as db:
        scan_library(db, root_id, task_id)


@router.post("/{root_id}/scan", response_model=TaskOut, status_code=202)
def start_scan(
    root_id: int,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
):
    root = db.get(LibraryRoot, root_id)
    if not root:
        raise AppError("NOT_FOUND", "媒体库不存在", status_code=404)
    task = TaskRecord(kind="scan_library", message="等待扫描")
    db.add(task)
    db.commit()
    db.refresh(task)
    background.add_task(run_scan, root.id, task.id)
    return task
