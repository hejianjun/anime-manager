from __future__ import annotations

import asyncio
import json
from typing import Any

from .database import SessionLocal
from .models import TaskRecord

task_event_subscribers: dict[int, set[asyncio.Queue[dict[str, Any]]]] = {}


def task_event_payload(task: TaskRecord) -> dict[str, Any]:
    result = task.result or {}
    if task.kind == "bulk_search_confirm":
        result = {
            key: result.get(key, 0)
            for key in ("searched", "total", "confirmed", "skipped", "failed")
        }
    return {
        "id": task.id,
        "parent_task_id": task.parent_task_id,
        "kind": task.kind,
        "status": task.status,
        "progress": task.progress,
        "message": task.message,
        "result": result,
        "error": task.error,
    }


async def publish_task_event(task: TaskRecord) -> None:
    payload = task_event_payload(task)
    for queue in tuple(task_event_subscribers.get(task.id, ())):
        if queue.full():
            queue.get_nowait()
        queue.put_nowait(payload)


async def task_event_stream(task_id: int, session_factory=None):
    factory = session_factory or SessionLocal
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1)
    subscribers = task_event_subscribers.setdefault(task_id, set())
    subscribers.add(queue)
    try:
        with factory() as db:
            task = db.get(TaskRecord, task_id)
            if not task:
                return
            payload = task_event_payload(task)
        yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        if payload["status"] in {"completed", "failed"}:
            return
        while True:
            try:
                payload = await asyncio.wait_for(queue.get(), timeout=15)
            except TimeoutError:
                yield ": keep-alive\n\n"
                continue
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            if payload["status"] in {"completed", "failed"}:
                return
    finally:
        subscribers.discard(queue)
        if not subscribers:
            task_event_subscribers.pop(task_id, None)
