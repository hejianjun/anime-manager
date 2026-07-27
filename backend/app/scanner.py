from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .errors import AppError
from .models import LibraryRoot, MatchGroup, MediaFile, TaskRecord
from .parser import normalize_title, parse_filename

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".mov", ".wmv", ".m4v", ".ts", ".webm"}


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

        paths = sorted(
            path for path in root_path.rglob("*") if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS
        )
        warnings: list[str] = []
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
            digest = None
            if existing and existing.size == stat.st_size and existing.modified_ns == stat.st_mtime_ns:
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
            group_key = f"{str(path.parent.resolve()).casefold()}::{normalize_title(parsed.title)}"
            group = db.scalar(
                select(MatchGroup).where(
                    MatchGroup.library_root_id == root.id,
                    MatchGroup.group_key == group_key,
                )
            )
            if not group:
                group = MatchGroup(
                    library_root_id=root.id,
                    group_key=group_key,
                    display_title=parsed.title,
                    search_keyword=parsed.title,
                )
                db.add(group)
                db.flush()
            media_info, warning = probe_media(path)
            if warning and len(warnings) < 20:
                warnings.append(warning)
            if existing:
                updated += 1
                existing.path = canonical
                existing.relative_path = str(path.relative_to(root_path))
                existing.size = stat.st_size
                existing.modified_ns = stat.st_mtime_ns
                existing.content_hash = digest
                existing.parsed_title = parsed.title
                existing.episode = parsed.episode
                existing.status = "present"
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
                        match_group_id=group.id,
                        **media_info,
                    )
                )
            task.progress = index / max(len(paths), 1)
            task.message = f"正在扫描 {index}/{len(paths)}"
            db.commit()
        root.last_scan_at = datetime.now(timezone.utc)
        task.status = "completed"
        task.progress = 1
        task.message = "扫描完成"
        task.result = {
            "found": len(paths),
            "created": created,
            "updated": updated,
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

