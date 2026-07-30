import pytest
from pydantic import ValidationError

from app.episode_numbers import (
    episode_filename_code,
    episode_sort_key,
    normalize_episode_identifier,
)
from app.schemas import MediaFilePatch


def test_normalize_episode_identifier_accepts_anidb_letters() -> None:
    assert normalize_episode_identifier(" s1 ") == "S1"
    assert MediaFilePatch(episode=2).episode == "2"
    assert MediaFilePatch(episode="t1").episode == "T1"
    assert MediaFilePatch(episode="").episode is None


@pytest.mark.parametrize("value", ["S-1", "S 1", "特别篇", "A" * 17])
def test_episode_identifier_rejects_unsafe_values(value: str) -> None:
    with pytest.raises((ValueError, ValidationError)):
        MediaFilePatch(episode=value)


def test_episode_filename_and_sorting_keep_numeric_compatibility() -> None:
    assert episode_filename_code("1") == "01"
    assert episode_filename_code("S1") == "S1"
    values = ["S10", "2", "S2", None, "1"]
    assert sorted(values, key=episode_sort_key) == ["1", "2", "S2", "S10", None]
