from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import Anime


INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
def _target_directory_name(title: str) -> str:
    cleaned = INVALID_FILENAME.sub("_", title).strip().rstrip(".")
    return cleaned or "Untitled"


def catalog_health(anime: Anime) -> dict[str, bool | int]:
    present = [item for item in anime.files if item.status == "present"]
    if not present:
        return {
            "directory_name_mismatch": False,
            "missing_nfo_count": 0,
            "missing_episode_image_count": 0,
        }

    videos = [Path(item.path) for item in present]
    target_directory = _target_directory_name(anime.title).casefold()
    directory_name_mismatch = any(
        video.parent.name.casefold() != target_directory
        for video in videos
    )

    is_movie = len(present) == 1 and (anime.media_type or "").casefold() == "movie"
    missing_nfo_count = sum(not item.has_nfo for item in present)
    if not is_movie and not anime.has_show_nfo:
        missing_nfo_count += 1
    missing_episode_image_count = (
        0 if is_movie else sum(not item.has_episode_image for item in present)
    )
    return {
        "directory_name_mismatch": directory_name_mismatch,
        "missing_nfo_count": missing_nfo_count,
        "missing_episode_image_count": missing_episode_image_count,
    }
