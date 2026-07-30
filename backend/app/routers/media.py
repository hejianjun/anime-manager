import mimetypes
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..database import get_db
from ..errors import AppError
from ..library_paths import configured_library_paths
from ..models import MediaFile
from ..schemas import MediaFilePatch

router = APIRouter(prefix="/api/media-files", tags=["media"])


@router.patch("/{media_id}", response_model=dict)
def patch_media_file(
    media_id: int,
    payload: MediaFilePatch,
    db: Session = Depends(get_db),
):
    media = db.get(MediaFile, media_id)
    if not media:
        raise AppError("NOT_FOUND", "媒体文件不存在", status_code=404)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(media, key, value)
    db.commit()
    return {
        "id": media.id,
        "episode": media.episode,
        "parsed_title": media.parsed_title,
    }


def resolve_media_stream_path(media: MediaFile) -> Path:
    if media.status != "present":
        raise AppError("MEDIA_UNAVAILABLE", "媒体文件当前不可用", status_code=404)
    try:
        media_path = Path(media.path).resolve(strict=True)
        allowed_roots = [
            path.resolve(strict=True)
            for path in configured_library_paths(media.library_root)
        ]
        if not any(media_path.is_relative_to(root) for root in allowed_roots):
            raise ValueError
    except (OSError, ValueError):
        raise AppError(
            "MEDIA_UNAVAILABLE",
            "媒体文件不存在或不在主目录、扫描目录内",
            status_code=404,
        )
    if not media_path.is_file():
        raise AppError("MEDIA_UNAVAILABLE", "媒体文件不存在", status_code=404)
    return media_path


@router.get("/{media_id}/stream", response_class=FileResponse)
def stream_media_file(media_id: int, db: Session = Depends(get_db)):
    media = db.scalar(
        select(MediaFile)
        .options(selectinload(MediaFile.library_root))
        .where(MediaFile.id == media_id)
    )
    if not media:
        raise AppError("NOT_FOUND", "媒体文件不存在", status_code=404)
    path = resolve_media_stream_path(media)
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(
        path,
        media_type=media_type,
        filename=path.name,
        content_disposition_type="inline",
    )
