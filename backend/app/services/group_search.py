from __future__ import annotations

from typing import Any

from ..database import SessionLocal
from ..matching import search_group
from ..models import MatchGroup, TaskRecord
from ..task_events import publish_task_event


async def _update_search_task(
    task_id: int,
    *,
    status: str,
    progress: float,
    message: str,
    result: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
    session_factory=None,
) -> None:
    factory = session_factory or SessionLocal
    with factory() as db:
        task = db.get(TaskRecord, task_id)
        if not task:
            return
        task.status = status
        task.progress = progress
        task.message = message
        if result is not None:
            task.result = result
        task.error = error
        db.commit()
        await publish_task_event(task)


async def run_group_search(
    task_id: int,
    group_id: int,
    keyword: str,
    sources: list[str],
    request_errors: list[dict[str, Any]] | None = None,
    session_factory=None,
) -> None:
    """Search one pending group in the background and publish source-level progress."""
    factory = session_factory or SessionLocal
    request_errors = list(request_errors or [])
    try:
        await _update_search_task(
            task_id,
            status="running",
            progress=0,
            message="正在准备候选搜索",
            session_factory=factory,
        )
        with factory() as db:
            group = db.get(MatchGroup, group_id)
            if not group:
                raise LookupError("匹配分组不存在")

            async def report_progress(completed: int, total: int, source: str) -> None:
                await _update_search_task(
                    task_id,
                    status="running",
                    progress=completed / max(total, 1),
                    message=f"正在搜索 {completed + 1}/{total}：{source}",
                    session_factory=factory,
                )

            candidates, search_errors = await search_group(
                db,
                group,
                keyword,
                sources,
                report_progress,
            )

        errors = [*request_errors, *search_errors]
        await _update_search_task(
            task_id,
            status="completed",
            progress=1,
            message=f"候选搜索完成，共找到 {len(candidates)} 条",
            result={
                "group_id": group_id,
                "candidate_count": len(candidates),
                "errors": errors,
            },
            session_factory=factory,
        )
    except Exception as exc:
        await _update_search_task(
            task_id,
            status="failed",
            progress=0,
            message="候选搜索失败",
            error={"message": str(exc) or type(exc).__name__},
            session_factory=factory,
        )
