from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .errors import AppError
from .models import Anime, LibraryRoot, MatchGroup, MediaFile, TaskRecord
from .parser import parse_filename

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".mov", ".wmv", ".m4v", ".ts", ".webm"}
DELETION_DIRECTORY_NAME = ".delete"
FOLDER_YEAR = re.compile(
    r"(?<!\d)(?:19|20)\d{2}(?!\d)"
)
EMPTY_BRACKETS = re.compile(r"[\(\[\{（【]\s*[\)\]\}）】]")
EPISODE_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


def _directory_entries(path: Path, cache: dict[Path, set[str]]) -> set[str]:
    directory = path.parent
    if directory not in cache:
        try:
            cache[directory] = {
                item.name.casefold()
                for item in directory.iterdir()
                if item.is_file()
            }
        except OSError:
            cache[directory] = set()
    return cache[directory]


def _has_episode_image(path: Path, entries: set[str]) -> bool:
    stem = path.stem.casefold()
    return any(
        f"{stem}{suffix}{extension}" in entries
        for suffix in ("", "-thumb")
        for extension in EPISODE_IMAGE_EXTENSIONS
    )


def update_catalog_sidecar_health(db: Session, directory_cache: dict[Path, set[str]]) -> None:
    for anime in db.scalars(select(Anime)).all():
        present = [item for item in anime.files if item.status == "present"]
        if not present:
            anime.has_show_nfo = False
            continue
        is_movie = len(present) == 1 and (anime.media_type or "").casefold() == "movie"
        if is_movie:
            anime.has_show_nfo = True
            continue
        parents = [str(Path(item.path).parent) for item in present]
        common_dir = Path(os.path.commonpath(parents))
        anime.has_show_nfo = (
            "tvshow.nfo" in _directory_entries(common_dir / "_placeholder", directory_cache)
        )


def directory_group_key(path: Path) -> str:
    """Use one stable pending-match group for every physical directory."""
    return f"directory::{str(path.parent.resolve()).casefold()}"


def directory_display_title(path: Path, root_path: Path, parsed_title: str) -> str:
    if path.parent.resolve() == root_path:
        return parsed_title
    return path.parent.name or parsed_title


def folder_search_keyword(title: str) -> str:
    """Remove release years from a folder-derived title used for source searches."""
    keyword = FOLDER_YEAR.sub(" ", title)
    keyword = EMPTY_BRACKETS.sub(" ", keyword)
    keyword = re.sub(r"[\s._\-—–·・]+", " ", keyword).strip()
    return keyword or title


def directory_search_keyword(
    path: Path, root_path: Path, display_title: str
) -> str:
    if path.parent.resolve() == root_path:
        return display_title
    return folder_search_keyword(display_title)


def migrate_pending_folder_search_keywords(db: Session) -> int:
    """Update legacy default keywords without overwriting manual search edits."""
    root_paths = {
        root.id: str(Path(root.path).resolve()).casefold()
        for root in db.scalars(select(LibraryRoot)).all()
    }
    updated = 0
    groups = db.scalars(
        select(MatchGroup).where(
            MatchGroup.status == "pending",
            MatchGroup.search_keyword == MatchGroup.display_title,
            MatchGroup.group_key.startswith("directory::"),
        )
    ).all()
    for group in groups:
        directory_path = group.group_key.removeprefix("directory::")
        if directory_path == root_paths.get(group.library_root_id):
            continue
        keyword = folder_search_keyword(group.display_title)
        if keyword != group.search_keyword:
            group.search_keyword = keyword
            updated += 1
    return updated


def pending_group_for_path(
    db: Session,
    root: LibraryRoot,
    root_path: Path,
    path: Path,
    parsed_title: str,
    existing: MediaFile | None,
) -> MatchGroup:
    group_key = directory_group_key(path)
    group = db.scalar(
        select(MatchGroup).where(
            MatchGroup.library_root_id == root.id,
            MatchGroup.group_key == group_key,
        )
    )
    if group:
        title = directory_display_title(path, root_path, parsed_title)
        if group.search_keyword == group.display_title:
            group.search_keyword = directory_search_keyword(path, root_path, title)
        group.display_title = title
        return group

    previous = existing.match_group if existing else None
    if previous and previous.status == "pending" and previous.anime_id is None:
        previous.group_key = group_key
        previous.display_title = directory_display_title(path, root_path, parsed_title)
        previous.search_keyword = directory_search_keyword(
            path, root_path, previous.display_title
        )
        db.flush()
        return previous

    title = directory_display_title(path, root_path, parsed_title)
    group = MatchGroup(
        library_root_id=root.id,
        group_key=group_key,
        display_title=title,
        search_keyword=directory_search_keyword(path, root_path, title),
    )
    db.add(group)
    db.flush()
    return group


def delete_empty_pending_groups(db: Session, root_id: int) -> int:
    groups = db.scalars(
        select(MatchGroup).where(
            MatchGroup.library_root_id == root_id,
            MatchGroup.status == "pending",
            ~MatchGroup.files.any(),
        )
    ).all()
    for group in groups:
        db.delete(group)
    return len(groups)


def content_hash(path: Path) -> str:
    digest = hashlib.blake2b(digest_size=32)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def probe_media(path: Path) -> tuple[dict, str | None]:
    if not shutil.which("ffprobe"):
        return {}, "ffprobe 未安装，已跳过媒体技术信息"
    try:
        completed = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=codec_type,codec_name,width,height",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        data = json.loads(completed.stdout)
        video = next((item for item in data.get("streams", []) if item.get("codec_type") == "video"), {})
        return {
            "duration": float(data.get("format", {}).get("duration", 0)) or None,
            "width": video.get("width"),
            "height": video.get("height"),
            "video_codec": video.get("codec_name"),
        }, None
    except (subprocess.SubprocessError, ValueError, json.JSONDecodeError) as exc:
        return {}, f"无法读取 {path.name} 的媒体信息: {exc}"


def scan_library(db: Session, root_id: int, task_id: int) -> None:
    task = db.get(TaskRecord, task_id)
    root = db.get(LibraryRoot, root_id)
    if not task or not root:
        return
    try:
        root_path = Path(root.path).resolve(strict=True)
        if not root_path.is_dir():
            raise AppError("INVALID_LIBRARY_ROOT", "媒体库路径不是目录")
        task.status = "running"
        task.message = "正在枚举媒体文件"
        db.query(MediaFile).filter(MediaFile.library_root_id == root.id).update({"status": "missing"})
        db.commit()

        discovered_files = sorted(
            path
            for path in root_path.rglob("*")
            if path.is_file()
            and DELETION_DIRECTORY_NAME not in path.relative_to(root_path).parts
        )
        paths = [
            path for path in discovered_files
            if path.suffix.lower() in VIDEO_EXTENSIONS
        ]
        warnings: list[str] = []
        directory_cache: dict[Path, set[str]] = {}
        for discovered in discovered_files:
            directory_cache.setdefault(
                discovered.parent,
                set(),
            ).add(discovered.name.casefold())
        created = updated = 0
        for index, path in enumerate(paths, start=1):
            stat = path.stat()
            canonical = str(path.resolve())
            existing = db.scalar(
                select(MediaFile).where(
                    MediaFile.library_root_id == root.id,
                    MediaFile.path == canonical,
                )
            )
            media_unchanged = bool(
                existing
                and existing.size == stat.st_size
                and existing.modified_ns == stat.st_mtime_ns
            )
            digest = None
            if media_unchanged:
                digest = existing.content_hash
            if not digest:
                digest = content_hash(path)
            if not existing:
                existing = db.scalar(
                    select(MediaFile).where(
                        MediaFile.library_root_id == root.id,
                        MediaFile.content_hash == digest,
                        MediaFile.status == "missing",
                    )
                )
            parsed = parse_filename(path)
            directory_entries = _directory_entries(path, directory_cache)
            has_nfo = path.with_suffix(".nfo").name.casefold() in directory_entries
            has_episode_image = _has_episode_image(path, directory_entries)
            media_info, warning = ({}, None) if media_unchanged else probe_media(path)
            if warning and len(warnings) < 20:
                warnings.append(warning)
            group = None
            if not existing or existing.anime_id is None:
                group = pending_group_for_path(
                    db, root, root_path, path, parsed.title, existing
                )
            if existing:
                updated += 1
                existing.path = canonical
                existing.relative_path = str(path.relative_to(root_path))
                existing.size = stat.st_size
                existing.modified_ns = stat.st_mtime_ns
                existing.content_hash = digest
                existing.parsed_title = parsed.title
                existing.episode = parsed.episode
                existing.has_nfo = has_nfo
                existing.has_episode_image = has_episode_image
                existing.status = "present"
                if group:
                    existing.match_group_id = group.id
                existing.scanned_at = datetime.now(timezone.utc)
                for key, value in media_info.items():
                    setattr(existing, key, value)
            else:
                created += 1
                db.add(
                    MediaFile(
                        library_root_id=root.id,
                        path=canonical,
                        relative_path=str(path.relative_to(root_path)),
                        size=stat.st_size,
                        modified_ns=stat.st_mtime_ns,
                        content_hash=digest,
                        hash_algorithm="blake2b-256",
                        parsed_title=parsed.title,
                        episode=parsed.episode,
                        has_nfo=has_nfo,
                        has_episode_image=has_episode_image,
                        match_group_id=group.id if group else None,
                        **media_info,
                    )
                )
            task.progress = index / max(len(paths), 1)
            task.message = f"正在扫描 {index}/{len(paths)}"
            db.commit()
        merged_groups = delete_empty_pending_groups(db, root.id)
        update_catalog_sidecar_health(db, directory_cache)
        root.last_scan_at = datetime.now(timezone.utc)
        task.status = "completed"
        task.progress = 1
        task.message = "扫描完成"
        task.result = {
            "found": len(paths),
            "created": created,
            "updated": updated,
            "merged_groups": merged_groups,
            "missing": db.query(MediaFile).filter_by(library_root_id=root.id, status="missing").count(),
            "warnings": warnings,
        }
        db.commit()
    except Exception as exc:
        db.rollback()
        task = db.get(TaskRecord, task_id)
        if task:
            task.status = "failed"
            task.error = {
                "code": getattr(exc, "code", "SCAN_FAILED"),
                "message": str(exc),
                "details": getattr(exc, "details", None),
                "retryable": False,
            }
            task.message = "扫描失败"
            db.commit()
