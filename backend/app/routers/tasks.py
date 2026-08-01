from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import SessionLocal, get_db
from ..errors import AppError
from ..models import TaskRecord
from ..schemas import TaskOut
from ..task_events import task_event_stream

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


@router.get("/{task_id}/children", response_model=list[TaskOut])
def get_task_children(task_id: int, db: Session = Depends(get_db)):
    if not db.get(TaskRecord, task_id):
        raise AppError("NOT_FOUND", "任务不存在", status_code=404)
    return list(
        db.scalars(
            select(TaskRecord)
            .where(TaskRecord.parent_task_id == task_id)
            .order_by(TaskRecord.id)
        ).all()
    )


@router.get("/{task_id}", response_model=TaskOut)
def get_task(task_id: int, db: Session = Depends(get_db)):
    task = db.get(TaskRecord, task_id)
    if not task:
        raise AppError("NOT_FOUND", "任务不存在", status_code=404)
    return task


@router.get("/{task_id}/events")
def task_events(task_id: int):
    # The stream reads task updates using short-lived sessions internally; do
    # not retain a pooled connection for the lifetime of the SSE response.
    with SessionLocal() as db:
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
