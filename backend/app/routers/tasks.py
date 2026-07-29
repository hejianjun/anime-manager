from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..errors import AppError
from ..models import TaskRecord
from ..schemas import TaskOut
from ..task_events import task_event_stream

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


@router.get("/{task_id}", response_model=TaskOut)
def get_task(task_id: int, db: Session = Depends(get_db)):
    task = db.get(TaskRecord, task_id)
    if not task:
        raise AppError("NOT_FOUND", "任务不存在", status_code=404)
    return task


@router.get("/{task_id}/events")
def task_events(task_id: int, db: Session = Depends(get_db)):
    if not db.get(TaskRecord, task_id):
        raise AppError("NOT_FOUND", "任务不存在", status_code=404)
    return StreamingResponse(
        task_event_stream(task_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )
