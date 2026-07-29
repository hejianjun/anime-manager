from sqlalchemy import select
from sqlalchemy.orm import selectinload

from .models import Anime, MatchGroup, MediaFile


def group_query():
    return select(MatchGroup).options(
        selectinload(MatchGroup.files),
        selectinload(MatchGroup.candidates),
    )


def anime_query():
    return select(Anime).options(
        selectinload(Anime.files).selectinload(MediaFile.library_root),
        selectinload(Anime.mappings),
    )
