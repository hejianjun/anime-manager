from __future__ import annotations

import asyncio
import os
import tempfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

from lxml import etree
from sqlalchemy import select

from ..database import SessionLocal
from ..description_translation import auto_translate_anime_description
from ..errors import AppError
from ..exporter import (
    _atomic_write,
    _episode_nfo,
    _movie_nfo,
    _season_nfo,
    _show_nfo,
)
from ..library_paths import containing_library_path
from ..models import Anime, MediaFile, ScrapeHistory, SourceMapping, TaskRecord
from ..queries import anime_query
from ..scrapers import SCRAPERS, Candidate, SourceMetadata
from ..source_settings import enabled_scraper_names
from ..task_events import publish_task_event

PREVIEW_KIND = "bulk_getchu_description_preview"
WRITE_KIND = "getchu_description_write"

_preview_locks: dict[int, asyncio.Lock] = {}
_write_semaphore_value: asyncio.Semaphore | None = None
_write_semaphore_loop: asyncio.AbstractEventLoop | None = None
_detail_semaphore_value: asyncio.Semaphore | None = None
_detail_semaphore_loop: asyncio.AbstractEventLoop | None = None


def _preview_lock(task_id: int) -> asyncio.Lock:
    return _preview_locks.setdefault(task_id, asyncio.Lock())


def _write_semaphore() -> asyncio.Semaphore:
    global _write_semaphore_value, _write_semaphore_loop
    loop = asyncio.get_running_loop()
    if _write_semaphore_value is None or _write_semaphore_loop is not loop:
        _write_semaphore_value = asyncio.Semaphore(2)
        _write_semaphore_loop = loop
    return _write_semaphore_value


def _detail_semaphore() -> asyncio.Semaphore:
    global _detail_semaphore_value, _detail_semaphore_loop
    loop = asyncio.get_running_loop()
    if _detail_semaphore_value is None or _detail_semaphore_loop is not loop:
        _detail_semaphore_value = asyncio.Semaphore(1)
        _detail_semaphore_loop = loop
    return _detail_semaphore_value


def _error_payload(exc: Exception, default_code: str) -> dict[str, Any]:
    is_app_error = isinstance(exc, AppError)
    return {
        "code": exc.code if is_app_error else default_code,
        "message": exc.message if is_app_error else str(exc),
        "details": exc.details if is_app_error else None,
        "retryable": bool(exc.retryable) if is_app_error else False,
    }


def _candidate_payload(candidate: Candidate) -> dict[str, Any]:
    return {
        "source_id": candidate.source_id,
        "title": candidate.title,
        "year": candidate.year,
        "cover_url": candidate.cover_url,
        "score": candidate.score,
    }


def _description_excerpt(description: str | None, limit: int = 500) -> str | None:
    text = (description or "").strip()
    if not text:
        return None
    return text if len(text) <= limit else f"{text[:limit].rstrip()}…"


def _target_plan(anime: Anime, *, inspect_exists: bool) -> dict[str, Any]:
    present = [item for item in anime.files if item.status == "present"]
    if not present:
        raise AppError("NO_MEDIA_FILES", "作品没有可用媒体文件", status_code=409)
    root_ids = {item.library_root_id for item in present}
    source_roots = {
        containing_library_path(Path(item.path), item.library_root) for item in present
    }
    if len(root_ids) != 1 or len(source_roots) != 1 or None in source_roots:
        raise AppError(
            "MULTIPLE_LIBRARY_ROOTS",
            "同一作品跨越多个媒体库目录或媒体文件位于库外",
            status_code=409,
        )
    library_root = next(iter(source_roots))
    assert library_root is not None
    file_paths = [Path(item.path).absolute() for item in present]
    common_dir = Path(os.path.commonpath([str(path.parent) for path in file_paths]))
    is_movie = len(present) == 1 and (anime.media_type or "").casefold() == "movie"
    blockers: list[str] = []
    targets: list[dict[str, Any]] = []
    if is_movie:
        targets.append(
            {
                "path": str(file_paths[0].with_suffix(".nfo")),
                "kind": "movie_nfo",
                "expected_root": "movie",
                "media_id": present[0].id,
                "episode": None,
            }
        )
    else:
        targets.extend(
            [
                {
                    "path": str(common_dir / "tvshow.nfo"),
                    "kind": "tvshow_nfo",
                    "expected_root": "tvshow",
                    "media_id": None,
                    "episode": None,
                },
                {
                    "path": str(common_dir / "season.nfo"),
                    "kind": "season_nfo",
                    "expected_root": "season",
                    "media_id": None,
                    "episode": None,
                },
            ]
        )
        for media, video in zip(present, file_paths, strict=True):
            target = video.with_suffix(".nfo")
            if media.episode is None and (not inspect_exists or not target.exists()):
                blockers.append(f"{media.relative_path} 缺少集号，无法创建剧集 NFO")
            targets.append(
                {
                    "path": str(target),
                    "kind": "episode_nfo",
                    "expected_root": "episodedetails",
                    "media_id": media.id,
                    "episode": media.episode,
                }
            )
    seen: set[str] = set()
    for target in targets:
        path = Path(target["path"])
        if not path.absolute().is_relative_to(library_root.absolute()):
            raise AppError("PATH_OUTSIDE_LIBRARY", "NFO 路径越过媒体库边界", status_code=400)
        normalized = os.path.normcase(str(path.absolute()))
        if normalized in seen:
            blockers.append(f"同一作品存在重复 NFO 目标: {path}")
        seen.add(normalized)
        target["exists"] = path.exists() if inspect_exists else None
    return {"targets": targets, "blockers": blockers, "library_root": library_root}


def build_initial_rows(animes: list[Anime]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    target_owners: dict[str, list[int]] = {}
    for anime in animes:
        try:
            target_plan = _target_plan(anime, inspect_exists=False)
            targets = target_plan["targets"]
            blockers = list(target_plan["blockers"])
        except AppError as exc:
            targets = []
            blockers = [exc.message]
        row = {
            "anime_id": anime.id,
            "anime_title": anime.title,
            "search_keyword": (anime.original_title or anime.title).strip(),
            "search_status": "pending",
            "candidates": [],
            "default_source_id": None,
            "description_preview": None,
            "nfo_targets": targets,
            "blockers": blockers,
            "error": None,
        }
        rows.append(row)
        for target in targets:
            key = os.path.normcase(str(Path(target["path"]).absolute()))
            target_owners.setdefault(key, []).append(anime.id)
    conflicts = {key: owners for key, owners in target_owners.items() if len(owners) > 1}
    for row in rows:
        for target in row["nfo_targets"]:
            key = os.path.normcase(str(Path(target["path"]).absolute()))
            if key in conflicts:
                row["blockers"].append(
                    f"NFO 与其他作品共享，不能批量更新: {target['path']}"
                )
    return rows


def _find_row(task: TaskRecord, anime_id: int) -> dict[str, Any]:
    row = next(
        (item for item in (task.result or {}).get("items", []) if item["anime_id"] == anime_id),
        None,
    )
    if row is None:
        raise AppError("INVALID_GETCHU_PREVIEW_ITEM", "作品不属于该批量预览", status_code=409)
    return row


def validate_preview_candidate(
    task: TaskRecord, anime_id: int, source_id: str
) -> dict[str, Any]:
    if task.kind != PREVIEW_KIND:
        raise AppError("INVALID_GETCHU_PREVIEW", "任务不是 Getchu 简介预览", status_code=409)
    row = _find_row(task, anime_id)
    if row.get("search_status") != "ready":
        raise AppError("GETCHU_ITEM_NOT_READY", "该作品尚未完成搜索", status_code=409)
    if row.get("blockers"):
        raise AppError(
            "GETCHU_NFO_BLOCKED",
            "该作品存在 NFO 阻塞项",
            details=row["blockers"],
            status_code=409,
        )
    if source_id not in {item["source_id"] for item in row.get("candidates", [])}:
        raise AppError("INVALID_GETCHU_CANDIDATE", "候选不属于该预览结果", status_code=409)
    return row


async def _replace_preview_row(task_id: int, anime_id: int, row: dict[str, Any]) -> None:
    async with _preview_lock(task_id):
        with SessionLocal() as db:
            task = db.get(TaskRecord, task_id)
            if not task:
                return
            result = dict(task.result or {})
            items = list(result.get("items", []))
            for index, item in enumerate(items):
                if item["anime_id"] == anime_id:
                    items[index] = row
                    break
            result["items"] = items
            result["processed"] = sum(
                item.get("search_status") not in {"pending", "searching"} for item in items
            )
            task.result = result
            task.progress = result["processed"] / max(result.get("total", len(items)), 1)
            db.commit()
            await publish_task_event(task)


async def run_getchu_description_preview(task_id: int) -> None:
    try:
        with SessionLocal() as db:
            task = db.get(TaskRecord, task_id)
            if not task:
                return
            task.status = "running"
            task.message = "正在搜索 Getchu 简介"
            db.commit()
            await publish_task_event(task)

        with SessionLocal() as db:
            task = db.get(TaskRecord, task_id)
            anime_ids = [item["anime_id"] for item in (task.result or {}).get("items", [])]

        for index, anime_id in enumerate(anime_ids, start=1):
            with SessionLocal() as db:
                task = db.get(TaskRecord, task_id)
                row = dict(_find_row(task, anime_id))
                if row.get("search_status") == "cancelled":
                    continue
                row["search_status"] = "searching"
                task.message = f"正在搜索 {index}/{len(anime_ids)}：{row['anime_title']}"
                db.commit()
            await _replace_preview_row(task_id, anime_id, row)

            try:
                with SessionLocal() as db:
                    anime = db.scalar(anime_query().where(Anime.id == anime_id))
                    if not anime:
                        raise AppError("NOT_FOUND", "作品不存在", status_code=404)
                    if (anime.description or "").strip():
                        raise AppError("DESCRIPTION_EXISTS", "当前作品已有简介", status_code=409)
                    if row.get("blockers"):
                        raise AppError(
                            "GETCHU_NFO_BLOCKED",
                            "该作品存在 NFO 阻塞项",
                            details=row["blockers"],
                            status_code=409,
                        )
                    mapping = next(
                        (item for item in anime.mappings if item.source == "getchu"), None
                    )
                    if mapping:
                        metadata = await SCRAPERS["getchu"].detail(db, mapping.source_id)
                        candidates = [
                            Candidate(
                                source="getchu",
                                source_id=mapping.source_id,
                                title=metadata.title,
                                year=metadata.year,
                                cover_url=metadata.cover_url,
                                score=1,
                            )
                        ]
                        default_id = mapping.source_id
                        description_preview = _description_excerpt(metadata.description)
                    else:
                        candidates = (
                            await SCRAPERS["getchu"].search(db, row["search_keyword"])
                        )[:5]
                        exact = next((item for item in candidates if item.score >= 1), None)
                        default_id = exact.source_id if exact else None
                        description_preview = None
                        if exact:
                            metadata = await SCRAPERS["getchu"].detail(db, exact.source_id)
                            description_preview = _description_excerpt(metadata.description)
                    db.commit()
                    target_plan = _target_plan(anime, inspect_exists=True)
                    row["nfo_targets"] = target_plan["targets"]
                    row["blockers"] = target_plan["blockers"]
                    row["candidates"] = [_candidate_payload(item) for item in candidates]
                    row["default_source_id"] = default_id
                    row["description_preview"] = description_preview
                    row["search_status"] = "ready" if candidates else "no_candidate"
                    row["error"] = None
            except Exception as exc:
                row["search_status"] = "failed"
                row["error"] = _error_payload(exc, "GETCHU_PREVIEW_FAILED")
            await _replace_preview_row(task_id, anime_id, row)

        with SessionLocal() as db:
            task = db.get(TaskRecord, task_id)
            if task:
                task.status = "completed"
                task.progress = 1
                task.message = "Getchu 简介搜索完成"
                db.commit()
                await publish_task_event(task)
    except Exception as exc:
        with SessionLocal() as db:
            task = db.get(TaskRecord, task_id)
            if task:
                task.status = "failed"
                task.message = "Getchu 简介搜索失败"
                task.error = _error_payload(exc, "GETCHU_PREVIEW_FAILED")
                db.commit()
                await publish_task_event(task)
    finally:
        _preview_locks.pop(task_id, None)


async def cancel_preview_item(task_id: int, anime_id: int) -> dict[str, Any]:
    async with _preview_lock(task_id):
        with SessionLocal() as db:
            task = db.get(TaskRecord, task_id)
            if not task or task.kind != PREVIEW_KIND:
                raise AppError("NOT_FOUND", "Getchu 简介预览不存在", status_code=404)
            row = dict(_find_row(task, anime_id))
            if row.get("search_status") != "pending":
                raise AppError("GETCHU_ITEM_ALREADY_STARTED", "该作品已经开始搜索", status_code=409)
            row["search_status"] = "cancelled"
            result = dict(task.result or {})
            result["items"] = [
                row if item["anime_id"] == anime_id else item
                for item in result.get("items", [])
            ]
            task.result = result
            db.commit()
            await publish_task_event(task)
            return row


async def get_candidate_detail(
    preview_task_id: int, anime_id: int, source_id: str
) -> dict[str, Any]:
    # Queue before opening a session: Getchu itself is globally serialized, so
    # concurrent detail requests must not occupy the whole database pool while
    # they wait for their network turn.
    async with _detail_semaphore():
        with SessionLocal() as db:
            task = db.get(TaskRecord, preview_task_id)
            if not task:
                raise AppError("NOT_FOUND", "Getchu 简介预览不存在", status_code=404)
            row = validate_preview_candidate(task, anime_id, source_id)
            if "getchu" not in enabled_scraper_names(db):
                raise AppError("SOURCE_DISABLED", "Getchu 已在设置中停用", status_code=409)
            metadata = await SCRAPERS["getchu"].detail(db, source_id)
            db.commit()
            return {
                "source_id": source_id,
                "title": metadata.title,
                "year": metadata.year,
                "description": metadata.description,
                "description_preview": _description_excerpt(metadata.description),
                "nfo_targets": row["nfo_targets"],
            }


def _render_missing_nfo(anime: Anime, target: dict[str, Any]) -> bytes:
    kind = target["kind"]
    if kind == "tvshow_nfo":
        return _show_nfo(anime)
    if kind == "season_nfo":
        return _season_nfo(anime, 1)
    if kind == "movie_nfo":
        return _movie_nfo(anime)
    if target.get("episode") is None:
        raise AppError("EPISODE_REQUIRED", "缺少集号，无法创建剧集 NFO", status_code=409)
    return _episode_nfo(anime, target["episode"])


def _xml_local_name(node: Any) -> str | None:
    """Return a tag name only for real XML elements, never comments/PIs."""
    tag = getattr(node, "tag", None)
    return etree.QName(tag).localname if isinstance(tag, str) else None


def _merge_plot(original: bytes, expected_root: str, description: str) -> bytes:
    try:
        parser = etree.XMLParser(remove_blank_text=False, remove_comments=False)
        tree = etree.parse(BytesIO(original), parser)
    except (etree.XMLSyntaxError, ValueError) as exc:
        raise AppError(
            "INVALID_NFO_XML",
            "已有 NFO 不是有效 XML，不会覆盖",
            details=str(exc),
            status_code=409,
        ) from exc
    root = tree.getroot()
    if _xml_local_name(root) != expected_root:
        raise AppError(
            "INVALID_NFO_ROOT",
            f"已有 NFO 根节点不是 {expected_root}，不会覆盖",
            status_code=409,
        )
    plots = [
        child
        for child in root
        if _xml_local_name(child) == "plot"
    ]
    if len(plots) == 1 and (plots[0].text or "") == description:
        return original
    if plots:
        plot = plots[0]
        for duplicate in plots[1:]:
            root.remove(duplicate)
    else:
        namespace = etree.QName(root.tag).namespace
        plot = etree.Element(f"{{{namespace}}}plot" if namespace else "plot")
        insert_at = next(
            (
                index + 1
                for index, child in enumerate(root)
                if _xml_local_name(child) in {"title", "originaltitle"}
            ),
            len(root),
        )
        root.insert(insert_at, plot)
    plot.text = description
    encoding = tree.docinfo.encoding or "UTF-8"
    doctype = tree.docinfo.doctype or None
    return etree.tostring(
        tree,
        encoding=encoding,
        xml_declaration=True,
        doctype=doctype,
        pretty_print=False,
    )


@dataclass
class _WriteJournal:
    originals: dict[Path, bytes | None]
    changed: list[Path]

    def restore(self) -> list[dict[str, str]]:
        failures: list[dict[str, str]] = []
        for path in reversed(self.changed):
            try:
                original = self.originals[path]
                if original is None:
                    path.unlink(missing_ok=True)
                else:
                    _replace_without_backup(path, original)
            except Exception as exc:
                failures.append({"path": str(path), "message": str(exc)})
        return failures


def _replace_without_backup(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".anime-manager-restore-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _write_nfo_bundle(anime: Anime) -> tuple[_WriteJournal, dict[str, Any]]:
    plan = _target_plan(anime, inspect_exists=True)
    if plan["blockers"]:
        raise AppError(
            "GETCHU_NFO_BLOCKED",
            "作品存在无法写入的 NFO",
            details=plan["blockers"],
            status_code=409,
        )
    originals: dict[Path, bytes | None] = {}
    contents: dict[Path, bytes] = {}
    for target in plan["targets"]:
        path = Path(target["path"])
        original = path.read_bytes() if path.exists() else None
        originals[path] = original
        contents[path] = (
            _merge_plot(original, target["expected_root"], anime.description or "")
            if original is not None
            else _render_missing_nfo(anime, target)
        )
    journal = _WriteJournal(originals=originals, changed=[])
    created: list[str] = []
    updated: list[str] = []
    unchanged: list[str] = []
    try:
        for path, content in contents.items():
            original = originals[path]
            if original == content:
                unchanged.append(str(path))
                continue
            _atomic_write(path, content, overwrite=original is not None)
            journal.changed.append(path)
            (updated if original is not None else created).append(str(path))
    except Exception as exc:
        restore_failures = journal.restore()
        raise AppError(
            "GETCHU_NFO_WRITE_FAILED",
            "写入 NFO 失败，已尝试恢复本次修改",
            details={"cause": str(exc), "restore_failed": restore_failures},
            status_code=500,
        ) from exc
    return journal, {"created": created, "updated": updated, "unchanged": unchanged}


async def run_getchu_description_write(task_id: int) -> None:
    async with _write_semaphore():
        journal: _WriteJournal | None = None
        with SessionLocal() as db:
            task = db.get(TaskRecord, task_id)
            if not task:
                return
            try:
                task.status = "running"
                task.message = "正在获取 Getchu 简介"
                db.commit()
                await publish_task_event(task)

                preview = db.get(TaskRecord, task.parent_task_id)
                anime_id = int((task.result or {})["anime_id"])
                source_id = str((task.result or {})["source_id"])
                if not preview:
                    raise AppError("NOT_FOUND", "Getchu 简介预览不存在", status_code=404)
                if "getchu" not in enabled_scraper_names(db):
                    raise AppError("SOURCE_DISABLED", "Getchu 已在设置中停用", status_code=409)
                validate_preview_candidate(preview, anime_id, source_id)
                anime = db.scalar(anime_query().where(Anime.id == anime_id))
                if not anime:
                    raise AppError("NOT_FOUND", "作品不存在", status_code=404)
                if (anime.description or "").strip():
                    raise AppError("DESCRIPTION_EXISTS", "当前作品已有简介，不会覆盖", status_code=409)
                existing_owner = db.scalar(
                    select(SourceMapping).where(
                        SourceMapping.source == "getchu",
                        SourceMapping.source_id == source_id,
                    )
                )
                if existing_owner and existing_owner.anime_id != anime.id:
                    raise AppError(
                        "SOURCE_ALREADY_USED",
                        "选择的 Getchu 条目已绑定到其他作品",
                        status_code=409,
                    )
                existing_mapping = next(
                    (item for item in anime.mappings if item.source == "getchu"), None
                )
                if existing_mapping and existing_mapping.source_id != source_id:
                    raise AppError(
                        "SOURCE_ALREADY_BOUND",
                        "作品已绑定另一个 Getchu 条目，请先在单部流程中修正",
                        status_code=409,
                    )

                metadata: SourceMetadata = await SCRAPERS["getchu"].detail(db, source_id)
                # Surface cache/database errors before invoking a paid translation
                # provider or touching NFO files.
                db.flush()
                description = (metadata.description or "").strip()
                if metadata.is_mock or not description:
                    raise AppError(
                        "SOURCE_DESCRIPTION_EMPTY",
                        "所选 Getchu 条目没有可用简介",
                        status_code=409,
                    )
                if not existing_mapping:
                    mapping = SourceMapping(
                        anime_id=anime.id,
                        source="getchu",
                        source_id=source_id,
                        is_mock=False,
                    )
                    anime.mappings.append(mapping)
                anime.description = description
                provenance = dict(anime.field_provenance or {})
                provenance["description"] = "getchu"
                anime.field_provenance = provenance
                translation = await auto_translate_anime_description(db, anime)

                task.message = "正在备份并更新全部关联 NFO"
                await publish_task_event(task)
                journal, files = await asyncio.to_thread(_write_nfo_bundle, anime)
                anime.has_show_nfo = any(
                    target["kind"] == "tvshow_nfo"
                    for target in _target_plan(anime, inspect_exists=True)["targets"]
                )
                for media in anime.files:
                    if media.status == "present":
                        media.has_nfo = True
                db.add(
                    ScrapeHistory(
                        anime_id=anime.id,
                        source="getchu",
                        success=True,
                        message="批量确认 Getchu 候选并同步简介到 NFO",
                    )
                )
                if translation["status"] in {"translated", "failed"}:
                    db.add(
                        ScrapeHistory(
                            anime_id=anime.id,
                            source="translation",
                            success=translation["status"] == "translated",
                            message=(
                                "简介已自动翻译为简体中文"
                                if translation["status"] == "translated"
                                else f"{translation.get('code')}: {translation.get('message')}"
                            ),
                        )
                    )
                task.status = "completed"
                task.progress = 1
                task.message = "Getchu 简介和 NFO 已写入"
                task.result = {
                    "preview_task_id": task.parent_task_id,
                    "anime_id": anime_id,
                    "source_id": source_id,
                    "translation": translation,
                    **files,
                }
                db.commit()
                journal = None
                await publish_task_event(task)
            except Exception as exc:
                db.rollback()
                restore_failures = journal.restore() if journal else []
                task = db.get(TaskRecord, task_id)
                if task:
                    task.status = "failed"
                    task.message = "Getchu 简介写入失败"
                    task.error = _error_payload(exc, "GETCHU_DESCRIPTION_WRITE_FAILED")
                    if restore_failures:
                        details = dict(task.error.get("details") or {})
                        details["restore_failed"] = restore_failures
                        task.error = {**task.error, "details": details}
                    db.commit()
                    await publish_task_event(task)
