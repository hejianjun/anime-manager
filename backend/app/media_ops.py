from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Callable

from sqlalchemy.orm import Session

from .errors import AppError
from .models import Anime, MatchGroup, MediaFile
from .parser import parse_filename


INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
DELETION_DIRECTORY_NAME = ".delete"
SIDECAR_EXTENSIONS = {
    ".nfo": "nfo",
    ".srt": "subtitle",
    ".ass": "subtitle",
    ".jpg": "image",
}


def _safe_name(value: str) -> str:
    cleaned = INVALID_FILENAME.sub("_", value).strip().rstrip(".")
    return cleaned or "Untitled"


def _sidecars_for_video(
    source: Path,
    target: Path,
    directory_cache: dict[Path, list[Path]] | None = None,
) -> list[dict]:
    """Return episode sidecars whose names are derived from a video name."""
    sidecars: list[dict] = []
    source_stem = source.stem
    folded_stem = source_stem.casefold()
    cache = directory_cache if directory_cache is not None else {}
    directory_files = cache.get(source.parent)
    if directory_files is None:
        directory_files = sorted(
            source.parent.iterdir(),
            key=lambda item: item.name.casefold(),
        )
        cache[source.parent] = directory_files
    for path in directory_files:
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
        resolved_dir = source_dir.absolute()
        if (
            not resolved_dir.is_dir()
            or not resolved_dir.is_relative_to(library_root)
        ):
            continue
        for path in sorted(resolved_dir.rglob("*"), key=lambda item: str(item).casefold()):
            resolved_path = path.absolute()
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
    deletion_dir = (library_root / DELETION_DIRECTORY_NAME).absolute()
    candidates = {
        source_dir.absolute()
        for source_dir in source_dirs
        if source_dir.absolute().is_dir()
        and source_dir.absolute().is_relative_to(library_root)
        and source_dir.absolute() not in {library_root, target_dir, deletion_dir}
        and not source_dir.absolute().is_relative_to(deletion_dir)
        and not target_dir.is_relative_to(source_dir.absolute())
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

    library_root = Path(present[0].library_root.path).absolute()
    target_dir = (library_root / _safe_name(anime.title)).absolute()
    if not target_dir.is_relative_to(library_root):
        raise AppError("PATH_OUTSIDE_LIBRARY", "目标目录越过媒体库边界", status_code=400)
    target_dir_exists = target_dir.exists()

    files: list[dict] = []
    targets: set[Path] = set()
    blockers: list[str] = []
    source_dirs: set[Path] = set()
    claimed_sidecars: set[Path] = set()
    directory_cache: dict[Path, list[Path]] = {}
    for media in sorted(present, key=lambda item: (item.episode is None, item.episode or 0, item.path)):
        source = Path(media.path).absolute()
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
            *_sidecars_for_video(source, target, directory_cache),
        ]
        for entry in entries:
            entry_source = Path(entry["source"])
            entry_target = Path(entry["target"])
            if entry["kind"] != "video":
                claimed_sidecars.add(entry_source)
            if entry_target in targets:
                blockers.append(f"多个文件将写入同一目标: {entry_target}")
            elif target_dir_exists and entry_target.exists() and entry_target != entry_source:
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
        elif target_dir_exists and entry_target.exists() and entry_target != entry_source:
            blockers.append(f"目标文件已存在: {entry_target}")
        targets.add(entry_target)
        files.append({**entry, "changed": entry_source != entry_target})
    cleanup_dirs, cleanup_blockers = _cleanup_directory_plan(source_dirs, target_dir, library_root)
    blockers.extend(cleanup_blockers)
    return {
        "anime_id": anime.id,
        "season": season,
        "library_root": str(library_root),
        "target_dir": str(target_dir),
        "deletion_dir": str(library_root / DELETION_DIRECTORY_NAME),
        "blockers": blockers,
        "files": files,
        "cleanup_dirs": cleanup_dirs,
    }


def build_bulk_rename_plan(
    animes: list[Anime],
    season: int = 1,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> dict:
    files: list[dict] = []
    blockers: list[str] = []
    skipped: list[dict] = []
    targets: dict[Path, tuple[int, str]] = {}
    cleanup_sources: set[Path] = set()
    cleanup_targets: dict[Path, tuple[Path, int, str]] = {}
    cleanup_dirs: list[dict] = []
    included_anime_ids: set[int] = set()
    root_availability: dict[str, bool] = {}

    for anime in animes:
        for media in anime.files:
            if media.status != "present":
                continue
            root_path = media.library_root.path
            if root_path in root_availability:
                continue
            try:
                root_availability[root_path] = Path(root_path).is_dir()
            except OSError:
                root_availability[root_path] = False

    total = len(animes)
    for index, anime in enumerate(animes, start=1):
        if not any(item.status == "present" for item in anime.files):
            skipped.append({"anime_id": anime.id, "title": anime.title, "reason": "没有可用媒体文件"})
            if progress_callback:
                progress_callback(index, total, anime.title)
            continue
        unavailable_roots = {
            item.library_root.path
            for item in anime.files
            if item.status == "present"
            and not root_availability.get(item.library_root.path, False)
        }
        if unavailable_roots:
            roots = "、".join(sorted(unavailable_roots))
            skipped.append(
                {
                    "anime_id": anime.id,
                    "title": anime.title,
                    "reason": f"媒体库不可访问: {roots}",
                }
            )
            if progress_callback:
                progress_callback(index, total, anime.title)
            continue
        if not anime.catalog_health["directory_name_mismatch"]:
            skipped.append({"anime_id": anime.id, "title": anime.title, "reason": "目录名已一致"})
            if progress_callback:
                progress_callback(index, total, anime.title)
            continue
        try:
            plan = build_rename_plan(anime, season)
        except AppError as error:
            blockers.append(f"{anime.title}: {error.message}")
            if progress_callback:
                progress_callback(index, total, anime.title)
            continue
        except OSError as error:
            skipped.append(
                {
                    "anime_id": anime.id,
                    "title": anime.title,
                    "reason": f"目录不可访问: {error}",
                }
            )
            if progress_callback:
                progress_callback(index, total, anime.title)
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
                    "library_root": plan["library_root"],
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
                        "library_root": plan["library_root"],
                    }
                )
        if progress_callback:
            progress_callback(index, total, anime.title)

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


def _execute_plan_files(
    db: Session,
    plan: dict,
    moved: list[tuple[Path, Path]] | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> list[tuple[Path, Path]]:
    moved = moved if moved is not None else []
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
        if progress_callback:
            progress_callback(str(target))
    return moved


def _rollback_moves(moved: list[tuple[Path, Path]]) -> None:
    for source, target in reversed(moved):
        if target.exists() and not source.exists():
            source.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(target), str(source))


def _execute_plan_directories(
    plan: dict,
    archived: list[tuple[Path, Path]] | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> list[tuple[Path, Path]]:
    archived = archived if archived is not None else []
    for item in plan["cleanup_dirs"]:
        if not item["changed"]:
            continue
        source = Path(item["source"])
        target = Path(item["target"])
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        archived.append((source, target))
        if progress_callback:
            progress_callback(str(target))
    return archived


def execute_rename_plan(db: Session, anime: Anime, season: int = 1) -> dict:
    plan = build_rename_plan(anime, season)
    if plan["blockers"]:
        raise AppError("RENAME_BLOCKED", "批量重命名前需要处理冲突", details=plan["blockers"], status_code=409)

    moved: list[tuple[Path, Path]] = []
    archived: list[tuple[Path, Path]] = []
    try:
        _execute_plan_files(db, plan, moved)
        _execute_plan_directories(plan, archived)
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


def _validate_planned_file(db: Session, item: dict) -> list[str]:
    """复核预览中的单个文件，避免使用过期计划移动错误的路径。"""
    errors: list[str] = []
    root = Path(item["library_root"]).absolute()
    source = Path(item["source"]).absolute()
    target = Path(item["target"]).absolute()

    # 缓存计划中的源路径和目标路径都必须仍在原媒体库内。
    if not source.is_relative_to(root) or not target.is_relative_to(root):
        return [f"路径越过媒体库边界: {source} -> {target}"]

    try:
        if not source.is_file():
            errors.append(f"源文件不存在或不可访问: {source}")
        if target != source and target.exists():
            errors.append(f"目标文件已存在: {target}")
    except OSError as error:
        errors.append(f"文件状态检查失败: {source}: {error}")

    # 视频文件还需确认数据库记录没有在预览后被扫描或人工操作改变。
    media_id = item.get("media_id")
    if media_id is not None:
        media = db.get(MediaFile, media_id)
        if (
            not media
            or media.status != "present"
            or Path(media.path).absolute() != source
        ):
            errors.append(f"媒体记录已变化，请重新预览: {source}")
    return errors


def _validate_cleanup_directory(item: dict) -> list[str]:
    """复核旧目录归档操作，并将目标严格限制在媒体库的 .delete 下。"""
    root = Path(item["library_root"]).absolute()
    source = Path(item["source"]).absolute()
    target = Path(item["target"]).absolute()
    deletion_root = (root / DELETION_DIRECTORY_NAME).absolute()

    if (
        not source.is_relative_to(root)
        or not target.is_relative_to(deletion_root)
    ):
        return [f"归档路径越过媒体库边界: {source} -> {target}"]

    errors: list[str] = []
    try:
        if not source.is_dir():
            errors.append(f"待归档目录不存在或不可访问: {source}")
        if target != source and target.exists():
            errors.append(f".delete 目录中已存在同名文件夹: {target}")
    except OSError as error:
        errors.append(f"目录状态检查失败: {source}: {error}")
    return errors


def validate_bulk_rename_plan(db: Session, plan: dict) -> None:
    """执行缓存计划前做一次只读复核，所有校验通过后才允许移动文件。"""
    # 预览阶段发现的命名冲突属于计划本身的问题，不能通过重新校验消除。
    if plan["blockers"]:
        raise AppError(
            "RENAME_BLOCKED",
            "全部作品批量重命名前需要处理冲突",
            details=plan["blockers"],
            status_code=409,
        )

    # 先收集全部变化，确保不会移动一部分后才发现后续路径已经失效。
    errors: list[str] = []
    for item in plan["files"]:
        if item["changed"]:
            errors.extend(_validate_planned_file(db, item))
    for item in plan["cleanup_dirs"]:
        if item["changed"]:
            errors.extend(_validate_cleanup_directory(item))

    if errors:
        raise AppError(
            "RENAME_PLAN_STALE",
            "文件状态已变化，请重新生成重命名预览",
            details=errors,
            status_code=409,
        )


def execute_cached_bulk_rename_plan(
    db: Session,
    plan: dict,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> dict:
    validate_bulk_rename_plan(db, plan)
    moved: list[tuple[Path, Path]] = []
    archived: list[tuple[Path, Path]] = []
    total = sum(1 for item in plan["files"] if item["changed"]) + sum(
        1 for item in plan["cleanup_dirs"] if item["changed"]
    )
    completed = 0

    def report(path: str) -> None:
        nonlocal completed
        completed += 1
        if progress_callback:
            progress_callback(completed, total, path)

    try:
        _execute_plan_files(db, plan, moved, report)
        _execute_plan_directories(plan, archived, report)
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


def execute_bulk_rename_plan(db: Session, animes: list[Anime], season: int = 1) -> dict:
    plan = build_bulk_rename_plan(animes, season)
    return execute_cached_bulk_rename_plan(db, plan)
