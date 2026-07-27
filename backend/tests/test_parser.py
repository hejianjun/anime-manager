from pathlib import Path

from app.parser import normalize_title, parse_filename


def test_parse_group_quality_and_episode() -> None:
    parsed = parse_filename(Path("[ABC] xxx The Animation - 01 [1080p][HEVC].mkv"))
    assert parsed.title == "xxx The Animation"
    assert parsed.episode == 1


def test_parse_volume() -> None:
    parsed = parse_filename(Path("作品名 Vol.2 1080p.mp4"))
    assert parsed.title == "作品名"
    assert parsed.episode == 2


def test_normalize_preserves_cjk() -> None:
    assert normalize_title("星界の紋章：OVA") == "星界の紋章 ova"

