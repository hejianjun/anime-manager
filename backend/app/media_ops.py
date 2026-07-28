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


def execute_rename_plan(db: Session, anime: Anime, season: int = 1) -> dict:
    plan = build_rename_plan(anime, season)
    if plan["blockers"]:
        raise AppError("RENAME_BLOCKED", "批量重命名前需要处理冲突", details=plan["blockers"], status_code=409)

    moved: list[tuple[Path, Path]] = []
    try:
        Path(plan["target_dir"]).mkdir(parents=True, exist_ok=True)
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
        db.commit()
        return {"moved": [str(target) for _, target in moved]}
    except Exception:
        db.rollback()
        for source, target in reversed(moved):
            if target.exists() and not source.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(target), str(source))
        raise
