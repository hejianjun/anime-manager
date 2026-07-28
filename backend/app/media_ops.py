from __future__ import annotations

import re
import shutil
from pathlib import Path

from sqlalchemy.orm import Session

from .errors import AppError
from .models import Anime, MatchGroup, MediaFile
from .parser import parse_filename


INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
DELETION_DIRECTORY_NAME = ".delete"
SIDECAR_EXTENSIONS = {
    ".nfo": "nfo",
    ".srt": "subtitle",
    ".jpg": "image",
}


def _safe_name(value: str) -> str:
    cleaned = INVALID_FILENAME.sub("_", value).strip().rstrip(".")
    return cleaned or "Untitled"


def _sidecars_for_video(source: Path, target: Path) -> list[dict]:
    """Return episode sidecars whose names are derived from a video name."""
    sidecars: list[dict] = []
    source_stem = source.stem
    folded_stem = source_stem.casefold()
    for path in sorted(source.parent.iterdir(), key=lambda item: item.name.casefold()):
        if not path.is_file() or path == source or not path.name.casefold().startswith(folded_stem):
            continue
        remainder = path.name[len(source_stem):]
        folded_remainder = remainder.casefold()
        extension = path.suffix.casefold()
        kind = SIDECAR_EXTENSIONS.get(extension)
        if not kind or not remainder.startswith((".", "-")) or not folded_remainder.endswith(extension):
            continue
        target_suffix = f"{remainder[:-len(extension)]}{extension}"
        sidecars.append(
            {
                "media_id": None,
                "source": str(path.resolve()),
                "target": str(target.with_name(f"{target.stem}{target_suffix}")),
                "kind": kind,
            }
        )
    return sidecars


def _recorded_source_directory(media: MediaFile) -> Path | None:
    group = media.match_group
    prefix = "directory::"
    if not group or not group.group_key.casefold().startswith(prefix):
        return None
    return Path(group.group_key[len(prefix):])


def _remaining_sidecars(
    source_dirs: set[Path],
    target_dir: Path,
    claimed_sources: set[Path],
    library_root: Path,
) -> list[dict]:
    """Move directory-level and nested sidecars while retaining relative names."""
    entries: list[dict] = []
    for source_dir in sorted(source_dirs, key=lambda path: str(path).casefold()):
        resolved_dir = source_dir.resolve()
        if (
            not resolved_dir.is_dir()
            or not resolved_dir.is_relative_to(library_root)
        ):
            continue
        for path in sorted(resolved_dir.rglob("*"), key=lambda item: str(item).casefold()):
            resolved_path = path.resolve()
            kind = SIDECAR_EXTENSIONS.get(path.suffix.casefold())
            if not path.is_file() or not kind or resolved_path in claimed_sources:
                continue
            entries.append(
                {
                    "media_id": None,
                    "source": str(resolved_path),
                    "target": str(target_dir / resolved_path.relative_to(resolved_dir)),
                    "kind": kind,
                    "episode": None,
                    "episode_title": None,
                }
            )
    return entries


def _cleanup_directory_plan(
    source_dirs: set[Path],
    target_dir: Path,
    library_root: Path,
) -> tuple[list[dict], list[str]]:
    deletion_dir = (library_root / DELETION_DIRECTORY_NAME).resolve()
    candidates = {
        source_dir.resolve()
        for source_dir in source_dirs
        if source_dir.resolve().is_dir()
        and source_dir.resolve().is_relative_to(library_root)
        and source_dir.resolve() not in {library_root, target_dir, deletion_dir}
        and not source_dir.resolve().is_relative_to(deletion_dir)
        and not target_dir.is_relative_to(source_dir.resolve())
    }
    top_level_sources = {
        source
        for source in candidates
        if not any(source != other and source.is_relative_to(other) for other in candidates)
    }
    cleanup_dirs: list[dict] = []
    blockers: list[str] = []
    targets: set[Path] = set()
    for source in sorted(top_level_sources, key=lambda path: str(path).casefold()):
        target = deletion_dir / source.name
        if target in targets:
            blockers.append(f"多个旧目录将写入同一 .delete 目录: {target}")
        elif target.exists() and target != source:
            blockers.append(f".delete 目录中已存在同名文件夹: {target}")
        targets.add(target)
        cleanup_dirs.append(
            {
                "source": str(source),
                "target": str(target),
                "changed": source != target,
            }
        )
    return cleanup_dirs, blockers


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
    source_dirs: set[Path] = set()
    claimed_sidecars: set[Path] = set()
    for media in sorted(present, key=lambda item: (item.episode is None, item.episode or 0, item.path)):
        source = Path(media.path).resolve()
        source_dirs.add(source.parent)
        recorded_source_dir = _recorded_source_directory(media)
        if recorded_source_dir:
            source_dirs.add(recorded_source_dir)
        parsed = parse_filename(source)
        if media.episode is None:
            blockers.append(f"{media.relative_path} 缺少集号")
            continue
        episode_title = (anime.episode_titles or {}).get(str(media.episode)) or parsed.episode_title
        suffix = f" - {_safe_name(episode_title)}" if episode_title else ""
        filename = f"{_safe_name(anime.title)} - S{season:02d}E{media.episode:02d}{suffix}{source.suffix.lower()}"
        target = target_dir / filename
        entries = [
            {
                "media_id": media.id,
                "source": str(source),
                "target": str(target),
                "kind": "video",
            },
            *_sidecars_for_video(source, target),
        ]
        for entry in entries:
            entry_source = Path(entry["source"])
            entry_target = Path(entry["target"])
            if entry["kind"] != "video":
                claimed_sidecars.add(entry_source)
            if entry_target in targets:
                blockers.append(f"多个文件将写入同一目标: {entry_target}")
            elif entry_target.exists() and entry_target != entry_source:
                blockers.append(f"目标文件已存在: {entry_target}")
            targets.add(entry_target)
            files.append(
                {
                    **entry,
                    "episode": media.episode,
                    "episode_title": episode_title,
                    "changed": entry_source != entry_target,
                }
            )
    for entry in _remaining_sidecars(source_dirs, target_dir, claimed_sidecars, library_root):
        entry_source = Path(entry["source"])
        entry_target = Path(entry["target"])
        if entry_target in targets:
            blockers.append(f"多个文件将写入同一目标: {entry_target}")
        elif entry_target.exists() and entry_target != entry_source:
            blockers.append(f"目标文件已存在: {entry_target}")
        targets.add(entry_target)
        files.append({**entry, "changed": entry_source != entry_target})
    cleanup_dirs, cleanup_blockers = _cleanup_directory_plan(source_dirs, target_dir, library_root)
    blockers.extend(cleanup_blockers)
    return {
        "anime_id": anime.id,
        "season": season,
        "target_dir": str(target_dir),
        "deletion_dir": str(library_root / DELETION_DIRECTORY_NAME),
        "blockers": blockers,
        "files": files,
        "cleanup_dirs": cleanup_dirs,
    }


def build_bulk_rename_plan(animes: list[Anime], season: int = 1) -> dict:
    files: list[dict] = []
    blockers: list[str] = []
    skipped: list[dict] = []
    targets: dict[Path, tuple[int, str]] = {}
    cleanup_sources: set[Path] = set()
    cleanup_targets: dict[Path, tuple[Path, int, str]] = {}
    cleanup_dirs: list[dict] = []
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
        for item in plan["cleanup_dirs"]:
            source = Path(item["source"])
            target = Path(item["target"])
            if source in cleanup_sources:
                continue
            previous = cleanup_targets.get(target)
            if previous and previous[0] != source:
                blockers.append(
                    f"作品「{previous[2]}」与「{anime.title}」的旧目录将写入同一 .delete 目录: {target}"
                )
            else:
                cleanup_sources.add(source)
                cleanup_targets[target] = (source, anime.id, anime.title)
                cleanup_dirs.append(
                    {
                        **item,
                        "anime_id": anime.id,
                        "anime_title": anime.title,
                    }
                )

    return {
        "season": season,
        "anime_count": len(included_anime_ids),
        "file_count": len(files),
        "changed_count": sum(1 for item in files if item["changed"]),
        "cleanup_count": sum(1 for item in cleanup_dirs if item["changed"]),
        "blockers": blockers,
        "skipped": skipped,
        "files": files,
        "cleanup_dirs": cleanup_dirs,
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
        media_id = item.get("media_id")
        media = db.get(MediaFile, media_id) if media_id is not None else None
        if media:
            media.path = str(target)
            media.relative_path = str(target.relative_to(Path(media.library_root.path).resolve()))
    return moved


def _rollback_moves(moved: list[tuple[Path, Path]]) -> None:
    for source, target in reversed(moved):
        if target.exists() and not source.exists():
            source.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(target), str(source))


def _execute_plan_directories(plan: dict) -> list[tuple[Path, Path]]:
    archived: list[tuple[Path, Path]] = []
    for item in plan["cleanup_dirs"]:
        if not item["changed"]:
            continue
        source = Path(item["source"])
        target = Path(item["target"])
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        archived.append((source, target))
    return archived


def execute_rename_plan(db: Session, anime: Anime, season: int = 1) -> dict:
    plan = build_rename_plan(anime, season)
    if plan["blockers"]:
        raise AppError("RENAME_BLOCKED", "批量重命名前需要处理冲突", details=plan["blockers"], status_code=409)

    moved: list[tuple[Path, Path]] = []
    archived: list[tuple[Path, Path]] = []
    try:
        moved = _execute_plan_files(db, plan)
        archived = _execute_plan_directories(plan)
        db.commit()
        return {
            "moved": [str(target) for _, target in moved],
            "archived_dirs": [str(target) for _, target in archived],
        }
    except Exception:
        db.rollback()
        _rollback_moves(archived)
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
    archived: list[tuple[Path, Path]] = []
    try:
        moved = _execute_plan_files(db, plan)
        archived = _execute_plan_directories(plan)
        db.commit()
        return {
            "anime_count": plan["anime_count"],
            "skipped": plan["skipped"],
            "moved": [str(target) for _, target in moved],
            "archived_dirs": [str(target) for _, target in archived],
        }
    except Exception:
        db.rollback()
        _rollback_moves(archived)
        _rollback_moves(moved)
        raise
