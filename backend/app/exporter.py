from __future__ import annotations

import difflib
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

import httpx
from sqlalchemy.orm import Session

from .errors import AppError
from .models import Anime, ExportHistory


def _text(parent: ET.Element, tag: str, value: object | None) -> None:
    if value not in (None, ""):
        ET.SubElement(parent, tag).text = str(value)


def _xml_bytes(root: ET.Element) -> bytes:
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _show_nfo(anime: Anime) -> bytes:
    root = ET.Element("tvshow")
    _text(root, "title", anime.title)
    _text(root, "originaltitle", anime.original_title)
    _text(root, "plot", anime.description)
    _text(root, "year", anime.year)
    _text(root, "studio", anime.studio)
    for genre in anime.genres or []:
        _text(root, "genre", genre)
    for tag in anime.tags or []:
        _text(root, "tag", tag)
    for mapping in anime.mappings:
        if not mapping.is_mock:
            unique = ET.SubElement(root, "uniqueid", type=mapping.source)
            unique.text = mapping.source_id
    return _xml_bytes(root)


def _episode_nfo(anime: Anime, episode: int) -> bytes:
    root = ET.Element("episodedetails")
    _text(root, "title", f"{anime.title} - {episode:02d}")
    _text(root, "showtitle", anime.title)
    _text(root, "season", 1)
    _text(root, "episode", episode)
    _text(root, "plot", anime.description)
    return _xml_bytes(root)


def _movie_nfo(anime: Anime) -> bytes:
    root = ET.Element("movie")
    _text(root, "title", anime.title)
    _text(root, "originaltitle", anime.original_title)
    _text(root, "plot", anime.description)
    _text(root, "year", anime.year)
    _text(root, "studio", anime.studio)
    for genre in anime.genres or []:
        _text(root, "genre", genre)
    for tag in anime.tags or []:
        _text(root, "tag", tag)
    for mapping in anime.mappings:
        if not mapping.is_mock:
            unique = ET.SubElement(root, "uniqueid", type=mapping.source)
            unique.text = mapping.source_id
    return _xml_bytes(root)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def build_export_plan(anime: Anime) -> dict:
    present = [item for item in anime.files if item.status == "present"]
    if not present:
        raise AppError("NO_MEDIA_FILES", "作品没有可用媒体文件", status_code=409)
    roots = {Path(item.library_root.path).resolve() for item in present}
    if len(roots) != 1:
        raise AppError("MULTIPLE_LIBRARY_ROOTS", "同一作品跨越多个媒体库，暂不支持导出", status_code=409)
    library_root = next(iter(roots))
    file_paths = [Path(item.path).resolve() for item in present]
    common_dir = Path(os.path.commonpath([str(path.parent) for path in file_paths]))
    if not _inside(common_dir, library_root):
        raise AppError("PATH_OUTSIDE_LIBRARY", "导出路径越过媒体库边界", status_code=400)
    is_movie = len(present) == 1 and (anime.media_type or "").casefold() == "movie"
    blockers: list[str] = []
    outputs: list[tuple[Path, bytes, str]] = []
    if is_movie:
        video = file_paths[0]
        outputs.append((video.with_suffix(".nfo"), _movie_nfo(anime), "movie"))
        poster_dir = video.parent
    else:
        outputs.append((common_dir / "tvshow.nfo", _show_nfo(anime), "tvshow"))
        poster_dir = common_dir
        for media in present:
            if media.episode is None:
                blockers.append(f"{media.relative_path} 缺少集号")
                continue
            video = Path(media.path)
            outputs.append((video.with_suffix(".nfo"), _episode_nfo(anime, media.episode), "episode"))
    for path, _, _ in outputs:
        if not _inside(path, library_root):
            raise AppError("PATH_OUTSIDE_LIBRARY", "导出目标越过媒体库边界", status_code=400)
    previews = []
    for path, content, kind in outputs:
        current = path.read_bytes() if path.exists() else b""
        current_text = current.decode("utf-8", errors="replace")
        content_text = content.decode("utf-8")
        previews.append(
            {
                "path": str(path),
                "kind": kind,
                "exists": path.exists(),
                "content": content_text,
                "diff": "\n".join(
                    difflib.unified_diff(
                        current_text.splitlines(),
                        content_text.splitlines(),
                        fromfile="current",
                        tofile="new",
                        lineterm="",
                    )
                ),
            }
        )
    if anime.cover_url:
        previews.append(
            {
                "path": str(poster_dir / "poster.jpg"),
                "kind": "artwork",
                "exists": (poster_dir / "poster.jpg").exists(),
                "content": None,
                "diff": "海报将在导出时下载并校验",
            }
        )
    return {
        "anime_id": anime.id,
        "mode": "movie" if is_movie else "tvshow",
        "blockers": blockers,
        "files": previews,
        "_outputs": outputs,
        "_poster_dir": poster_dir,
        "_library_root": library_root,
    }


def _atomic_write(path: Path, content: bytes, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise AppError("FILE_EXISTS", f"目标文件已存在: {path}", status_code=409)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(path, path.with_name(f"{path.name}.{stamp}.bak"))
    fd, temp_name = tempfile.mkstemp(prefix=".anime-manager-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


async def export_anime(db: Session, anime: Anime, overwrite: bool) -> dict:
    plan = build_export_plan(anime)
    if plan["blockers"]:
        raise AppError(
            "EXPORT_BLOCKED",
            "导出前需要修正媒体文件集号",
            details=plan["blockers"],
            status_code=409,
        )
    written: list[str] = []
    try:
        for path, content, _ in plan["_outputs"]:
            _atomic_write(path, content, overwrite)
            written.append(str(path))
        if anime.cover_url:
            target = plan["_poster_dir"] / "poster.jpg"
            if target.exists() and not overwrite:
                raise AppError("FILE_EXISTS", f"目标文件已存在: {target}", status_code=409)
            async with httpx.AsyncClient(timeout=45, follow_redirects=True) as client:
                response = await client.get(anime.cover_url)
                response.raise_for_status()
            mime = response.headers.get("content-type", "").split(";")[0]
            if not mime.startswith("image/"):
                raise AppError("INVALID_ARTWORK", "海报响应不是图片", details={"mime": mime})
            _atomic_write(target, response.content, overwrite)
            written.append(str(target))
        db.add(
            ExportHistory(
                anime_id=anime.id,
                success=True,
                files=written,
                message="导出完成",
            )
        )
        db.commit()
        return {"written": written}
    except Exception as exc:
        db.add(
            ExportHistory(
                anime_id=anime.id,
                success=False,
                files=written,
                message=str(exc),
            )
        )
        db.commit()
        raise


def public_export_plan(plan: dict) -> dict:
    return {key: value for key, value in plan.items() if not key.startswith("_")}

