from __future__ import annotations

import re
import shutil
from pathlib import Path

from sqlalchemy.orm import Session

from .errors import AppError
from .models import Anime, MatchGroup, MediaFile
from .parser import parse_filename


INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_name(value: str) -> str:
    cleaned = INVALID_FILENAME.sub("_", value).strip().rstrip(".")
    return cleaned or "Untitled"


def bind_group_to_anime(db: Session, group: MatchGroup, anime: Anime) -> Anime:
    group.anime_id = anime.id
    group.status = "confirmed"
    for media in group.files:
        media.anime_id = anime.id
    db.commit()
    db.refresh(anime)
    return anime


def build_rename_plan(anime: Anime, season: int = 1) -> dict:
    present = [item for item in anime.files if item.status == "present"]
    if not present:
        raise AppError("NO_MEDIA_FILES", "作品没有可用媒体文件", status_code=409)
    root_ids = {item.library_root_id for item in present}
    if len(root_ids) != 1:
        raise AppError("MULTIPLE_LIBRARY_ROOTS", "同一作品跨越多个媒体库，暂不支持批量移动", status_code=409)

    library_root = Path(present[0].library_root.path).resolve()
    target_dir = (library_root / _safe_name(anime.title)).resolve()
    if not target_dir.is_relative_to(library_root):
        raise AppError("PATH_OUTSIDE_LIBRARY", "目标目录越过媒体库边界", status_code=400)

    files: list[dict] = []
    targets: set[Path] = set()
    blockers: list[str] = []
    for media in sorted(present, key=lambda item: (item.episode is None, item.episode or 0, item.path)):
        source = Path(media.path).resolve()
        parsed = parse_filename(source)
        if media.episode is None:
            blockers.append(f"{media.relative_path} 缺少集号")
            continue
        suffix = f" - {_safe_name(parsed.episode_title)}" if parsed.episode_title else ""
        filename = f"{_safe_name(anime.title)} - S{season:02d}E{media.episode:02d}{suffix}{source.suffix.lower()}"
        target = target_dir / filename
        if target in targets:
            blockers.append(f"多个文件将写入同一目标: {target}")
        elif target.exists() and target != source:
            blockers.append(f"目标文件已存在: {target}")
        targets.add(target)
        files.append(
            {
                "media_id": media.id,
                "source": str(source),
                "target": str(target),
                "episode": media.episode,
                "episode_title": parsed.episode_title,
                "changed": source != target,
            }
        )
    return {"anime_id": anime.id, "season": season, "target_dir": str(target_dir), "blockers": blockers, "files": files}


def build_bulk_rename_plan(animes: list[Anime], season: int = 1) -> dict:
    files: list[dict] = []
    blockers: list[str] = []
    skipped: list[dict] = []
    targets: dict[Path, tuple[int, str]] = {}
    included_anime_ids: set[int] = set()

    for anime in animes:
        if not any(item.status == "present" for item in anime.files):
            skipped.append({"anime_id": anime.id, "title": anime.title, "reason": "没有可用媒体文件"})
            continue
        try:
            plan = build_rename_plan(anime, season)
        except AppError as error:
            blockers.append(f"{anime.title}: {error.message}")
            continue

        included_anime_ids.add(anime.id)
        blockers.extend(f"{anime.title}: {item}" for item in plan["blockers"])
        for item in plan["files"]:
            target = Path(item["target"])
            previous = targets.get(target)
            if previous and previous[0] != anime.id:
                blockers.append(f"作品「{previous[1]}」与「{anime.title}」将写入同一目标: {target}")
            else:
                targets[target] = (anime.id, anime.title)
            files.append(
                {
                    **item,
                    "anime_id": anime.id,
                    "anime_title": anime.title,
                    "target_dir": plan["target_dir"],
                }
            )

    return {
        "season": season,
        "anime_count": len(included_anime_ids),
        "file_count": len(files),
        "changed_count": sum(1 for item in files if item["changed"]),
        "blockers": blockers,
        "skipped": skipped,
        "files": files,
    }


def _execute_plan_files(db: Session, plan: dict) -> list[tuple[Path, Path]]:
    moved: list[tuple[Path, Path]] = []
    target_dirs = {Path(item["target"]).parent for item in plan["files"] if item["changed"]}
    for target_dir in target_dirs:
        target_dir.mkdir(parents=True, exist_ok=True)
    for item in plan["files"]:
        if not item["changed"]:
            continue
        source = Path(item["source"])
        target = Path(item["target"])
        shutil.move(str(source), str(target))
        moved.append((source, target))
        media = db.get(MediaFile, item["media_id"])
        if media:
            media.path = str(target)
            media.relative_path = str(target.relative_to(Path(media.library_root.path).resolve()))
    return moved


def _rollback_moves(moved: list[tuple[Path, Path]]) -> None:
    for source, target in reversed(moved):
        if target.exists() and not source.exists():
            source.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(target), str(source))


def execute_rename_plan(db: Session, anime: Anime, season: int = 1) -> dict:
    plan = build_rename_plan(anime, season)
    if plan["blockers"]:
        raise AppError("RENAME_BLOCKED", "批量重命名前需要处理冲突", details=plan["blockers"], status_code=409)

    moved: list[tuple[Path, Path]] = []
    try:
        moved = _execute_plan_files(db, plan)
        db.commit()
        return {"moved": [str(target) for _, target in moved]}
    except Exception:
        db.rollback()
        _rollback_moves(moved)
        raise


def execute_bulk_rename_plan(db: Session, animes: list[Anime], season: int = 1) -> dict:
    plan = build_bulk_rename_plan(animes, season)
    if plan["blockers"]:
        raise AppError(
            "RENAME_BLOCKED",
            "全部作品批量重命名前需要处理冲突",
            details=plan["blockers"],
            status_code=409,
        )

    moved: list[tuple[Path, Path]] = []
    try:
        moved = _execute_plan_files(db, plan)
        db.commit()
        return {
            "anime_count": plan["anime_count"],
            "skipped": plan["skipped"],
            "moved": [str(target) for _, target in moved],
        }
    except Exception:
        db.rollback()
        _rollback_moves(moved)
        raise
