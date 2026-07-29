from __future__ import annotations

import anyio
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import SessionLocal
from ..errors import AppError
from ..media_ops import build_bulk_rename_plan, execute_cached_bulk_rename_plan
from ..models import Anime, TaskRecord
from ..queries import anime_query
from ..task_events import publish_task_event


def _error_payload(exc: Exception, default_code: str) -> dict:
    return {
        "code": getattr(exc, "code", default_code),
        "message": str(exc),
        "details": getattr(exc, "details", None),
        "retryable": False,
    }


def _publish_from_worker(task: TaskRecord) -> None:
    anyio.from_thread.run(publish_task_event, task)


def _update_preview_progress(
    db: Session,
    task: TaskRecord,
    index: int,
    total: int,
    title: str,
) -> None:
    task.status = "running"
    task.progress = index / max(total, 1)
    task.message = f"正在检查 {index}/{total}：{title}"
    task.result = {"processed": index, "total": total, "current_anime": title}
    db.commit()
    _publish_from_worker(task)


def run_bulk_rename_preview_sync(task_id: int, season: int) -> None:
    with SessionLocal() as db:
        task = db.get(TaskRecord, task_id)
        if not task:
            return
        try:
            task.status = "running"
            task.message = "正在读取全部作品"
            db.commit()
            _publish_from_worker(task)
            animes = db.scalars(anime_query().order_by(Anime.title)).unique().all()
            plan = build_bulk_rename_plan(
                list(animes),
                season,
                lambda index, total, title: _update_preview_progress(
                    db, task, index, total, title
                ),
            )
            task.status = "completed"
            task.progress = 1
            task.message = "重命名预览已生成"
            task.result = plan
            db.commit()
            _publish_from_worker(task)
        except Exception as exc:
            db.rollback()
            task = db.get(TaskRecord, task_id)
            if task:
                task.status = "failed"
                task.message = "重命名预览失败"
                task.error = _error_payload(exc, "BULK_RENAME_PREVIEW_FAILED")
                db.commit()
                _publish_from_worker(task)


async def run_bulk_rename_preview(task_id: int, season: int) -> None:
    await anyio.to_thread.run_sync(run_bulk_rename_preview_sync, task_id, season)


def _update_execute_progress(
    task_id: int,
    index: int,
    total: int,
    path: str,
) -> None:
    with SessionLocal() as task_db:
        task = task_db.get(TaskRecord, task_id)
        if not task:
            return
        task.progress = index / max(total, 1)
        task.message = f"正在移动 {index}/{total}：{path}"
        task.result = {
            "preview_task_id": task.result.get("preview_task_id"),
            "processed": index,
            "total": total,
            "current_path": path,
        }
        task_db.commit()
        _publish_from_worker(task)


def _finish_execute_task(
    task_id: int,
    *,
    result: dict | None = None,
    error: dict | None = None,
) -> None:
    with SessionLocal() as db:
        task = db.get(TaskRecord, task_id)
        if not task:
            return
        if error:
            task.status = "failed"
            task.message = "全部作品重命名失败"
            task.error = error
        else:
            task.status = "completed"
            task.progress = 1
            task.message = "全部作品重命名完成"
            task.result = {
                "preview_task_id": task.result.get("preview_task_id"),
                **(result or {}),
            }
        db.commit()
        _publish_from_worker(task)


def run_bulk_rename_execute_sync(task_id: int, preview_task_id: int) -> None:
    with SessionLocal() as db:
        task = db.get(TaskRecord, task_id)
        preview_task = db.get(TaskRecord, preview_task_id)
        if not task or not preview_task:
            return
        try:
            plan = preview_task.result
            task.status = "running"
            task.message = "正在复核预览计划"
            db.commit()
            _publish_from_worker(task)
            result = execute_cached_bulk_rename_plan(
                db,
                plan,
                lambda index, total, path: _update_execute_progress(
                    task_id, index, total, path
                ),
            )
            _finish_execute_task(task_id, result=result)
        except Exception as exc:
            db.rollback()
            _finish_execute_task(
                task_id,
                error=_error_payload(exc, "BULK_RENAME_EXECUTE_FAILED"),
            )


async def run_bulk_rename_execute(task_id: int, preview_task_id: int) -> None:
    await anyio.to_thread.run_sync(
        run_bulk_rename_execute_sync,
        task_id,
        preview_task_id,
    )


def claim_preview_for_execution(
    db: Session,
    preview_task_id: int,
    execution_task: TaskRecord,
) -> TaskRecord:
    preview_task = db.get(TaskRecord, preview_task_id)
    if not preview_task:
        raise AppError("NOT_FOUND", "重命名预览任务不存在", status_code=404)
    if preview_task.kind != "bulk_rename_preview" or preview_task.status != "completed":
        raise AppError(
            "INVALID_RENAME_PREVIEW",
            "重命名预览任务尚未完成或类型不正确",
            status_code=409,
        )
    execution_tasks = db.scalars(
        select(TaskRecord).where(TaskRecord.kind == "bulk_rename_execute")
    ).all()
    if any(
        task.result.get("preview_task_id") == preview_task_id
        for task in execution_tasks
    ):
        raise AppError(
            "RENAME_PREVIEW_ALREADY_USED",
            "该重命名预览已经执行过，请重新生成预览",
            status_code=409,
        )
    if preview_task.result.get("blockers"):
        raise AppError(
            "RENAME_BLOCKED",
            "全部作品批量重命名前需要处理冲突",
            details=preview_task.result["blockers"],
            status_code=409,
        )
    if (
        not preview_task.result.get("changed_count")
        and not preview_task.result.get("cleanup_count")
    ):
        raise AppError("NOTHING_TO_RENAME", "没有需要重命名的文件", status_code=409)

    db.add(execution_task)
    db.commit()
    db.refresh(execution_task)
    return execution_task
