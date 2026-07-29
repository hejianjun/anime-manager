from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Anime, MatchGroup, MediaFile, TaskRecord

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/dashboard")
def dashboard(db: Session = Depends(get_db)) -> dict[str, int]:
    return {
        "files": db.scalar(select(func.count(MediaFile.id))) or 0,
        "anime": db.scalar(select(func.count(Anime.id))) or 0,
        "pending": db.scalar(
            select(func.count(MatchGroup.id)).where(
                MatchGroup.status == "pending",
                MatchGroup.files.any(),
            )
        )
        or 0,
        "missing": db.scalar(
            select(func.count(MediaFile.id)).where(MediaFile.status == "missing")
        )
        or 0,
        "running_tasks": db.scalar(
            select(func.count(TaskRecord.id)).where(
                TaskRecord.status.in_(["pending", "running"])
            )
        )
        or 0,
    }
