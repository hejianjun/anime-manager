from __future__ import annotations

from typing import Any

from ..database import SessionLocal
from ..errors import AppError
from ..matching import confirm_group, save_selections, search_group
from ..models import MatchGroup, ScrapeCandidate, TaskRecord
from ..queries import group_query
from ..source_settings import enabled_scraper_names
from ..task_events import publish_task_event


async def run_bulk_search_confirm(
    task_id: int,
    sources: list[str],
    session_factory=None,
) -> None:
    factory = session_factory or SessionLocal
    with factory() as db:
        task = db.get(TaskRecord, task_id)
        if not task:
            return
        try:
            enabled = enabled_scraper_names(db)
            groups = db.scalars(
                group_query()
                .where(MatchGroup.status == "pending", MatchGroup.files.any())
                .order_by(MatchGroup.updated_at.desc())
            ).all()
            total = len(groups)
            items: list[dict[str, Any]] = []
            confirmed = 0
            task.status = "running"
            task.message = f"准备处理 0/{total}"
            task.result = {
                "searched": 0,
                "total": total,
                "confirmed": 0,
                "skipped": 0,
                "failed": 0,
                "items": [],
            }
            db.commit()
            await publish_task_event(task)

            for index, group in enumerate(groups, start=1):
                task.message = f"正在匹配 {index}/{total}：{group.display_title}"
                task.progress = (index - 1) / max(total, 1)
                db.commit()
                await publish_task_event(task)
                try:
                    candidates, search_errors = await search_group(
                        db,
                        group,
                        group.search_keyword,
                        sources,
                    )
                    exact_by_source: dict[str, ScrapeCandidate] = {}
                    for candidate in candidates:
                        if candidate.score >= 1:
                            current = exact_by_source.get(candidate.source)
                            if current is None or candidate.score > current.score:
                                exact_by_source[candidate.source] = candidate
                    if not exact_by_source:
                        items.append(
                            {
                                "group_id": group.id,
                                "title": group.display_title,
                                "status": "skipped",
                                "reason": "没有 100% 匹配候选",
                                "errors": search_errors,
                            }
                        )
                    else:
                        save_selections(
                            db,
                            group,
                            {
                                source: (
                                    exact_by_source[source].id
                                    if source in exact_by_source
                                    else None
                                )
                                for source in enabled
                            },
                        )
                        anime = await confirm_group(db, group, enabled)
                        confirmed += 1
                        items.append(
                            {
                                "group_id": group.id,
                                "title": group.display_title,
                                "status": "confirmed",
                                "anime_id": anime.id,
                                "anime_title": anime.title,
                                "sources": sorted(exact_by_source),
                                "errors": search_errors,
                            }
                        )
                except AppError as exc:
                    db.rollback()
                    items.append(
                        {
                            "group_id": group.id,
                            "title": group.display_title,
                            "status": "failed",
                            "reason": exc.message,
                            "code": exc.code,
                        }
                    )

                task = db.get(TaskRecord, task_id)
                task.progress = index / max(total, 1)
                task.message = f"已处理 {index}/{total}"
                task.result = {
                    "searched": index,
                    "total": total,
                    "confirmed": confirmed,
                    "skipped": sum(item["status"] == "skipped" for item in items),
                    "failed": sum(item["status"] == "failed" for item in items),
                    "items": items,
                }
                db.commit()
                await publish_task_event(task)

            task.status = "completed"
            task.progress = 1
            task.message = f"批量匹配完成，共处理 {total} 个"
            db.commit()
            await publish_task_event(task)
        except Exception as exc:
            db.rollback()
            task = db.get(TaskRecord, task_id)
            if task:
                task.status = "failed"
                task.message = "批量匹配失败"
                task.error = {
                    "code": getattr(exc, "code", "BULK_MATCH_FAILED"),
                    "message": str(exc),
                    "retryable": True,
                }
                db.commit()
                await publish_task_event(task)
