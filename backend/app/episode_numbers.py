from __future__ import annotations

import re


EPISODE_IDENTIFIER = re.compile(r"^[A-Za-z0-9]+$")
EPISODE_PARTS = re.compile(r"^([A-Za-z]+)(\d+)$")


def normalize_episode_identifier(value: object | None) -> str | None:
    """统一人工输入的集号；AniDB 特别篇等编号允许使用字母前缀。"""
    if value is None:
        return None
    normalized = str(value).strip().upper()
    if not normalized:
        return None
    if len(normalized) > 16 or not EPISODE_IDENTIFIER.fullmatch(normalized):
        raise ValueError("集号只能包含字母和数字，且不超过 16 个字符")
    return normalized


def episode_filename_code(value: str | int) -> str:
    """普通数字沿用两位补零，字母编号按 AniDB 标识原样保留。"""
    normalized = str(value).upper()
    return normalized.zfill(2) if normalized.isdigit() else normalized


def episode_sort_key(value: str | int | None) -> tuple[int, str, int, str]:
    """常规集优先按数值排序，随后自然排序 S1、S2 等字母编号。"""
    if value is None:
        return (2, "", 0, "")
    normalized = str(value).upper()
    if normalized.isdigit():
        return (0, "", int(normalized), normalized)
    matched = EPISODE_PARTS.fullmatch(normalized)
    if matched:
        return (1, matched.group(1), int(matched.group(2)), normalized)
    return (1, normalized, 0, normalized)
