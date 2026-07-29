from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from sqlalchemy.orm import Session

from .errors import AppError
from .exporter import _atomic_write, _episode_nfo, _movie_nfo, _show_nfo
from .models import Anime, MediaFile, TaskRecord


def build_bulk_artifact_plan(animes: list[Anime]) -> dict:
    entries: list[dict] = []
    blockers: list[str] = []
    skipped: list[dict] = []
    anime_ids: set[int] = set()

    for anime in animes:
        present = [item for item in anime.files if item.status == "present"]
        if not present:
            continue
        roots = {Path(item.library_root.path) for item in present}
        if len(roots) != 1:
            skipped.append({
                "anime_id": anime.id,
                "title": anime.title,
                "reason": "作品跨越多个媒体库",
            })
            continue
        root = next(iter(roots))
        videos = [Path(item.path) for item in present]
        try:
            common_dir = Path(os.path.commonpath([str(video.parent) for video in videos]))
        except ValueError:
            skipped.append({
                "anime_id": anime.id,
                "title": anime.title,
                "reason": "无法确定作品目录",
            })
            continue
        if not common_dir.is_relative_to(root):
            skipped.append({
                "anime_id": anime.id,
                "title": anime.title,
                "reason": "作品目录越过媒体库边界",
            })
            continue

        is_movie = len(present) == 1 and (anime.media_type or "").casefold() == "movie"
        if is_movie:
            media = present[0]
            if not media.has_nfo:
                entries.append({
                    "anime_id": anime.id,
                    "anime_title": anime.title,
                    "media_id": media.id,
                    "kind": "movie_nfo",
                    "path": str(videos[0].with_suffix(".nfo")),
                    "_content": _movie_nfo(anime),
                })
                anime_ids.add(anime.id)
            continue

        if not anime.has_show_nfo:
            entries.append({
                "anime_id": anime.id,
                "anime_title": anime.title,
                "media_id": None,
                "kind": "tvshow_nfo",
                "path": str(common_dir / "tvshow.nfo"),
                "_content": _show_nfo(anime),
            })
            anime_ids.add(anime.id)

        for media, video in zip(present, videos, strict=True):
            if not media.has_nfo:
                if media.episode is None:
                    skipped.append({
                        "anime_id": anime.id,
                        "title": anime.title,
                        "reason": f"{media.relative_path} 未填写集号，已跳过剧集 NFO",
                    })
                else:
                    entries.append({
                        "anime_id": anime.id,
                        "anime_title": anime.title,
                        "media_id": media.id,
                        "kind": "episode_nfo",
                        "path": str(video.with_suffix(".nfo")),
                        "_content": _episode_nfo(anime, media.episode),
                    })
                    anime_ids.add(anime.id)
            if not media.has_episode_image:
                entries.append({
                    "anime_id": anime.id,
                    "anime_title": anime.title,
                    "media_id": media.id,
                    "kind": "episode_image",
                    "path": str(video.with_name(f"{video.stem}-thumb.jpg")),
                    "source": str(video),
                    "seek_seconds": max(1.0, min((media.duration or 600) * 0.1, 300.0)),
                })
                anime_ids.add(anime.id)

    image_count = sum(item["kind"] == "episode_image" for item in entries)
    if image_count and not shutil.which("ffmpeg"):
        blockers.append("未安装 ffmpeg，无法生成剧集图片")
    return {
        "anime_count": len(anime_ids),
        "nfo_count": sum(item["kind"].endswith("_nfo") for item in entries),
        "episode_image_count": image_count,
        "blockers": blockers,
        "skipped": skipped,
        "files": [
            {key: value for key, value in item.items() if not key.startswith("_")}
            for item in entries
        ],
        "_entries": entries,
    }


def _extract_episode_image(source: Path, target: Path, seek_seconds: float) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=".anime-manager-thumb-",
        suffix=".jpg",
        dir=target.parent,
    )
    os.close(fd)
    os.unlink(temp_name)
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{seek_seconds:.3f}",
                "-i",
                str(source),
                "-frames:v",
                "1",
                "-vf",
                "scale=1280:-2:force_original_aspect_ratio=decrease",
                "-q:v",
                "2",
                "-y",
                temp_name,
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        )
        os.replace(temp_name, target)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def execute_bulk_artifact_plan(
    db: Session,
    animes: list[Anime],
    task: TaskRecord,
) -> None:
    try:
        plan = build_bulk_artifact_plan(animes)
        if plan["blockers"]:
            raise AppError(
                "BULK_ARTIFACT_BLOCKED",
                "批量写入前需要处理阻塞项",
                details=plan["blockers"],
                status_code=409,
            )
        entries = plan["_entries"]
        task.status = "running"
        task.message = "正在写入 NFO 和剧集图片"
        db.commit()

        written: list[str] = []
        existing: list[str] = []
        failures: list[dict[str, str]] = []
        for index, entry in enumerate(entries, start=1):
            target = Path(entry["path"])
            media = db.get(MediaFile, entry["media_id"]) if entry["media_id"] else None
            anime = db.get(Anime, entry["anime_id"])
            try:
                if target.exists():
                    existing.append(str(target))
                elif entry["kind"] == "episode_image":
                    _extract_episode_image(
                        Path(entry["source"]),
                        target,
                        entry["seek_seconds"],
                    )
                    written.append(str(target))
                else:
                    _atomic_write(target, entry["_content"], overwrite=False)
                    written.append(str(target))

                if entry["kind"] == "tvshow_nfo" and anime:
                    anime.has_show_nfo = True
                elif entry["kind"].endswith("_nfo") and media:
                    media.has_nfo = True
                elif entry["kind"] == "episode_image" and media:
                    media.has_episode_image = True
            except Exception as exc:
                failures.append({"path": str(target), "message": str(exc)})
            task.progress = index / max(len(entries), 1)
            task.message = f"正在处理 {index}/{len(entries)}"
            db.commit()

        task.status = "completed"
        task.progress = 1
        task.message = "批量写入完成"
        task.result = {
            "anime_count": plan["anime_count"],
            "written": written,
            "existing": existing,
            "failed": failures,
            "skipped": plan["skipped"],
        }
        db.commit()
    except Exception as exc:
        db.rollback()
        task = db.get(TaskRecord, task.id)
        if task:
            task.status = "failed"
            task.message = "批量写入失败"
            task.error = {
                "code": getattr(exc, "code", "BULK_ARTIFACT_FAILED"),
                "message": str(exc),
                "details": getattr(exc, "details", None),
                "retryable": False,
            }
            db.commit()
