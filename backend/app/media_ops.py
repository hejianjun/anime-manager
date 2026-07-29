"""媒体绑定、重命名计划生成、计划复核以及文件移动事务。"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from sqlalchemy.orm import Session

from .errors import AppError
from .models import Anime, MatchGroup, MediaFile
from .parser import parse_filename


# Windows 文件名限制和应用约定的旧目录归档位置。
INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
DELETION_DIRECTORY_NAME = ".delete"

# 重命名时随视频一起搬迁的附属文件类型。
SIDECAR_EXTENSIONS = {
    ".nfo": "nfo",
    ".srt": "subtitle",
    ".ass": "subtitle",
    ".jpg": "image",
}


def _safe_name(value: str) -> str:
    """把作品名或集标题转换为可用于 Windows 路径的安全名称。"""
    cleaned = INVALID_FILENAME.sub("_", value).strip().rstrip(".")
    return cleaned or "Untitled"


def _sidecars_for_video(
    source: Path,
    target: Path,
    directory_cache: dict[Path, list[Path]] | None = None,
) -> list[dict]:
    """查找与视频同名的 NFO、字幕和图片，并同步生成目标名称。"""
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
    """从匹配分组中恢复扫描时记录的原始目录，供目录级附属文件搬迁使用。"""
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
    """收集尚未归属单集的目录附属文件，并保留其相对目录结构。"""
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
    """生成旧目录移入 .delete 的计划，并提前检查归档目标冲突。"""
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


def bind_group_to_anime(
    db: Session,
    group: MatchGroup,
    anime: Anime,
    file_ids: list[int] | None = None,
) -> Anime:
    """把待确认分组中选定的媒体文件绑定到已有作品。"""
    files = list(group.files)
    if file_ids is not None:
        requested = set(file_ids)
        files = [media for media in files if media.id in requested]
        if not requested or len(files) != len(requested):
            raise AppError("INVALID_MEDIA_SELECTION", "所选视频不属于当前待确认分组")
    if not files:
        raise AppError("NO_MEDIA_SELECTION", "请至少勾选一个视频")
    partial = len(files) < len(group.files)
    group.anime_id = None if partial else anime.id
    group.status = "pending" if partial else "confirmed"
    for media in files:
        media.anime_id = anime.id
        if partial:
            media.match_group_id = None
    db.commit()
    db.refresh(anime)
    return anime


@dataclass(frozen=True)
class _RenamePlanContext:
    """单作品计划生成期间不变的作品、媒体库和目标目录信息。"""

    anime: Anime
    season: int
    present: list[MediaFile]
    library_root: Path
    target_dir: Path
    target_dir_exists: bool


@dataclass
class _RenamePlanState:
    """单作品计划生成期间累计的文件、冲突和目录扫描状态。"""

    files: list[dict] = field(default_factory=list)
    targets: set[Path] = field(default_factory=set)
    blockers: list[str] = field(default_factory=list)
    source_dirs: set[Path] = field(default_factory=set)
    claimed_sidecars: set[Path] = field(default_factory=set)
    directory_cache: dict[Path, list[Path]] = field(default_factory=dict)


def _prepare_rename_plan(anime: Anime, season: int) -> _RenamePlanContext:
    """校验单作品的媒体库边界，并准备后续生成计划所需的上下文。"""
    present = [item for item in anime.files if item.status == "present"]
    if not present:
        raise AppError("NO_MEDIA_FILES", "作品没有可用媒体文件", status_code=409)

    root_ids = {item.library_root_id for item in present}
    if len(root_ids) != 1:
        raise AppError(
            "MULTIPLE_LIBRARY_ROOTS",
            "同一作品跨越多个媒体库，暂不支持批量移动",
            status_code=409,
        )

    library_root = Path(present[0].library_root.path).absolute()
    target_dir = (library_root / _safe_name(anime.title)).absolute()
    if not target_dir.is_relative_to(library_root):
        raise AppError(
            "PATH_OUTSIDE_LIBRARY",
            "目标目录越过媒体库边界",
            status_code=400,
        )
    return _RenamePlanContext(
        anime=anime,
        season=season,
        present=present,
        library_root=library_root,
        target_dir=target_dir,
        target_dir_exists=target_dir.exists(),
    )


def _append_rename_entry(
    files: list[dict],
    targets: set[Path],
    blockers: list[str],
    entry: dict,
    *,
    target_dir_exists: bool,
    metadata: dict | None = None,
) -> None:
    """登记单个重命名条目，并统一执行目标冲突和覆盖检查。"""
    source = Path(entry["source"])
    target = Path(entry["target"])

    # 同一作品内的所有视频和附属文件共享目标集合，避免互相覆盖。
    if target in targets:
        blockers.append(f"多个文件将写入同一目标: {target}")
    elif target_dir_exists and target.exists() and target != source:
        blockers.append(f"目标文件已存在: {target}")

    targets.add(target)
    files.append(
        {
            **entry,
            **(metadata or {}),
            "changed": source != target,
        }
    )


def _episode_target(
    context: _RenamePlanContext,
    media: MediaFile,
    source: Path,
) -> tuple[Path, str | None]:
    """根据作品元数据和原文件名生成单集目标路径及最终集标题。"""
    parsed = parse_filename(source)
    episode_title = (context.anime.episode_titles or {}).get(
        str(media.episode)
    ) or parsed.episode_title
    suffix = f" - {_safe_name(episode_title)}" if episode_title else ""
    filename = (
        f"{_safe_name(context.anime.title)}"
        f" - S{context.season:02d}E{media.episode:02d}"
        f"{suffix}{source.suffix.lower()}"
    )
    return context.target_dir / filename, episode_title


def _add_media_rename_entries(
    context: _RenamePlanContext,
    state: _RenamePlanState,
    media: MediaFile,
) -> None:
    """为一个视频及其同名附属文件生成并登记重命名条目。"""
    source = Path(media.path).absolute()
    state.source_dirs.add(source.parent)
    recorded_source_dir = _recorded_source_directory(media)
    if recorded_source_dir:
        state.source_dirs.add(recorded_source_dir)

    # 没有集号无法形成稳定的 Jellyfin 文件名，但仍保留源目录供冲突预览。
    if media.episode is None:
        state.blockers.append(f"{media.relative_path} 缺少集号")
        return

    target, episode_title = _episode_target(context, media, source)
    entries = [
        {
            "media_id": media.id,
            "source": str(source),
            "target": str(target),
            "kind": "video",
        },
        *_sidecars_for_video(source, target, state.directory_cache),
    ]
    for entry in entries:
        if entry["kind"] != "video":
            state.claimed_sidecars.add(Path(entry["source"]))
        _append_rename_entry(
            state.files,
            state.targets,
            state.blockers,
            entry,
            target_dir_exists=context.target_dir_exists,
            metadata={
                "episode": media.episode,
                "episode_title": episode_title,
            },
        )


def _add_remaining_sidecar_entries(
    context: _RenamePlanContext,
    state: _RenamePlanState,
) -> None:
    """登记未与具体视频同名匹配的目录级或嵌套附属文件。"""
    entries = _remaining_sidecars(
        state.source_dirs,
        context.target_dir,
        state.claimed_sidecars,
        context.library_root,
    )
    for entry in entries:
        _append_rename_entry(
            state.files,
            state.targets,
            state.blockers,
            entry,
            target_dir_exists=context.target_dir_exists,
        )


def build_rename_plan(anime: Anime, season: int = 1) -> dict:
    """生成单作品的只读重命名预览，不移动文件或修改数据库。"""
    context = _prepare_rename_plan(anime, season)
    state = _RenamePlanState()

    # 先登记视频和同名附属文件，再补充尚未认领的目录级附属文件。
    ordered_media = sorted(
        context.present,
        key=lambda item: (item.episode is None, item.episode or 0, item.path),
    )
    for media in ordered_media:
        _add_media_rename_entries(context, state, media)
    _add_remaining_sidecar_entries(context, state)

    cleanup_dirs, cleanup_blockers = _cleanup_directory_plan(
        state.source_dirs,
        context.target_dir,
        context.library_root,
    )
    state.blockers.extend(cleanup_blockers)
    return {
        "anime_id": anime.id,
        "season": season,
        "library_root": str(context.library_root),
        "target_dir": str(context.target_dir),
        "deletion_dir": str(context.library_root / DELETION_DIRECTORY_NAME),
        "blockers": state.blockers,
        "files": state.files,
        "cleanup_dirs": cleanup_dirs,
    }


@dataclass
class _BulkRenamePlanState:
    """批量计划的累计状态，集中保存跨作品去重和冲突检测所需的数据。"""

    files: list[dict] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    targets: dict[Path, tuple[int, str]] = field(default_factory=dict)
    cleanup_sources: set[Path] = field(default_factory=set)
    cleanup_targets: dict[Path, tuple[Path, int, str]] = field(default_factory=dict)
    cleanup_dirs: list[dict] = field(default_factory=list)
    included_anime_ids: set[int] = field(default_factory=set)


def _probe_library_roots(animes: list[Anime]) -> dict[str, bool]:
    """每个媒体库只探测一次，避免 NAS 离线时为每部作品重复等待网络超时。"""
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
    return root_availability


def _bulk_rename_skip_reason(
    anime: Anime,
    root_availability: dict[str, bool],
) -> str | None:
    """返回无需生成计划的原因；None 表示该作品需要继续检查。"""
    present = [item for item in anime.files if item.status == "present"]
    if not present:
        return "没有可用媒体文件"

    unavailable_roots = {
        item.library_root.path
        for item in present
        if not root_availability.get(item.library_root.path, False)
    }
    if unavailable_roots:
        return f"媒体库不可访问: {'、'.join(sorted(unavailable_roots))}"
    if not anime.catalog_health["directory_name_mismatch"]:
        return "目录名已一致"
    return None


def _merge_bulk_files(
    state: _BulkRenamePlanState,
    anime: Anime,
    plan: dict,
) -> None:
    """合并文件计划，同时检测不同作品是否生成了相同目标路径。"""
    for item in plan["files"]:
        target = Path(item["target"])
        previous = state.targets.get(target)
        if previous and previous[0] != anime.id:
            state.blockers.append(
                f"作品「{previous[1]}」与「{anime.title}」将写入同一目标: {target}"
            )
        else:
            state.targets[target] = (anime.id, anime.title)
        state.files.append(
            {
                **item,
                "anime_id": anime.id,
                "anime_title": anime.title,
                "target_dir": plan["target_dir"],
                "library_root": plan["library_root"],
            }
        )


def _merge_bulk_cleanup_dirs(
    state: _BulkRenamePlanState,
    anime: Anime,
    plan: dict,
) -> None:
    """合并旧目录归档计划，并避免重复归档或写入同一 .delete 目标。"""
    for item in plan["cleanup_dirs"]:
        source = Path(item["source"])
        target = Path(item["target"])
        if source in state.cleanup_sources:
            continue

        previous = state.cleanup_targets.get(target)
        if previous and previous[0] != source:
            state.blockers.append(
                f"作品「{previous[2]}」与「{anime.title}」的旧目录将写入同一 .delete 目录: {target}"
            )
            continue

        state.cleanup_sources.add(source)
        state.cleanup_targets[target] = (source, anime.id, anime.title)
        state.cleanup_dirs.append(
            {
                **item,
                "anime_id": anime.id,
                "anime_title": anime.title,
                "library_root": plan["library_root"],
            }
        )


def _merge_bulk_anime_plan(
    state: _BulkRenamePlanState,
    anime: Anime,
    plan: dict,
) -> None:
    """把单作品计划并入全局计划，并补充作品上下文。"""
    state.included_anime_ids.add(anime.id)
    state.blockers.extend(f"{anime.title}: {item}" for item in plan["blockers"])
    _merge_bulk_files(state, anime, plan)
    _merge_bulk_cleanup_dirs(state, anime, plan)


def build_bulk_rename_plan(
    animes: list[Anime],
    season: int = 1,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> dict:
    """逐部生成并合并重命名计划；单部目录故障不会中止整个批次。"""
    state = _BulkRenamePlanState()
    root_availability = _probe_library_roots(animes)
    total = len(animes)

    for index, anime in enumerate(animes, start=1):
        try:
            skip_reason = _bulk_rename_skip_reason(anime, root_availability)
            if skip_reason:
                state.skipped.append(
                    {
                        "anime_id": anime.id,
                        "title": anime.title,
                        "reason": skip_reason,
                    }
                )
                continue

            try:
                plan = build_rename_plan(anime, season)
            except AppError as error:
                state.blockers.append(f"{anime.title}: {error.message}")
                continue
            except OSError as error:
                # 作品目录在根目录探测后仍可能离线，将故障隔离到当前作品。
                state.skipped.append(
                    {
                        "anime_id": anime.id,
                        "title": anime.title,
                        "reason": f"目录不可访问: {error}",
                    }
                )
                continue

            _merge_bulk_anime_plan(state, anime, plan)
        finally:
            # 无论作品成功、跳过还是失败，都向 SSE 任务报告已完成检查。
            if progress_callback:
                progress_callback(index, total, anime.title)

    return {
        "season": season,
        "anime_count": len(state.included_anime_ids),
        "file_count": len(state.files),
        "changed_count": sum(1 for item in state.files if item["changed"]),
        "cleanup_count": sum(1 for item in state.cleanup_dirs if item["changed"]),
        "blockers": state.blockers,
        "skipped": state.skipped,
        "files": state.files,
        "cleanup_dirs": state.cleanup_dirs,
    }


def _execute_plan_files(
    db: Session,
    plan: dict,
    moved: list[tuple[Path, Path]] | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> list[tuple[Path, Path]]:
    """按计划移动文件、同步媒体路径，并实时记录可供回滚的移动顺序。"""
    moved = moved if moved is not None else []
    # 先创建全部目标目录，避免移动到一半才因父目录缺失中止。
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
            # 仅视频条目关联 media_file；字幕、图片等附属文件没有数据库记录。
            media.path = str(target)
            media.relative_path = str(target.relative_to(Path(media.library_root.path).resolve()))
        if progress_callback:
            progress_callback(str(target))
    return moved


def _rollback_moves(moved: list[tuple[Path, Path]]) -> None:
    """按执行的相反顺序恢复已移动文件或已归档目录。"""
    for source, target in reversed(moved):
        if target.exists() and not source.exists():
            source.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(target), str(source))


def _execute_plan_directories(
    plan: dict,
    archived: list[tuple[Path, Path]] | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> list[tuple[Path, Path]]:
    """文件全部处理后，将计划中的旧目录归档到媒体库 .delete。"""
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
    """同步生成并执行单作品计划，失败时同时回滚数据库和文件系统。"""
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
        # 目录最后移动、最先回滚，随后再恢复目录内的各个文件。
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
    """复核并执行已缓存的批量预览计划，不重新扫描全部作品。"""
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
        # 文件系统不受数据库事务管理，必须显式按相反顺序补偿。
        _rollback_moves(archived)
        _rollback_moves(moved)
        raise


def execute_bulk_rename_plan(db: Session, animes: list[Anime], season: int = 1) -> dict:
    """兼容同步调用：即时生成批量计划后交给缓存计划执行器。"""
    plan = build_bulk_rename_plan(animes, season)
    return execute_cached_bulk_rename_plan(db, plan)
