import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path


NOISE = re.compile(
    r"(?ix)"
    r"\b(?:2160p|1080p|720p|480p|4k|uhd|bluray|bdrip|webrip|web-dl|"
    r"x26[45]|h\.?26[45]|hevc|avc|aac|flac|10bit|8bit|dual.audio)\b"
)
GROUP = re.compile(r"^\s*\[[^\]]+\]\s*")
BRACKETS = re.compile(r"[\[\(](?:\s*(?:2160p|1080p|720p|x26[45]|hevc|aac|flac|[0-9a-f]{8})\s*)[\]\)]", re.I)
EPISODE_PATTERNS = [
    re.compile(r"(?i)(?:^|[\s._-])(?:S\d{1,2}E|EP?|Episode|Vol\.?)\s*0*(\d{1,4})(?:v\d+)?(?:$|[\s._-])"),
    re.compile(r"(?i)(?:^|[\s._-])-?\s*0*(\d{1,3})(?:v\d+)?(?:$|[\s._-])"),
]


@dataclass(frozen=True)
class ParsedFilename:
    title: str
    episode: int | None


def normalize_title(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    value = re.sub(r"[\s._\-:：·・/\\]+", " ", value)
    return re.sub(r"[^\w\u3040-\u30ff\u3400-\u9fff ]+", "", value).strip()


def parse_filename(path: Path) -> ParsedFilename:
    stem = unicodedata.normalize("NFKC", path.stem)
    cleaned = GROUP.sub("", stem)
    cleaned = BRACKETS.sub(" ", cleaned)
    cleaned = NOISE.sub(" ", cleaned)
    episode = None
    for pattern in EPISODE_PATTERNS:
        match = pattern.search(cleaned)
        if match:
            episode = int(match.group(1))
            cleaned = cleaned[: match.start()] + " " + cleaned[match.end() :]
            break
    cleaned = re.sub(r"[\[\]\(\)]", " ", cleaned)
    cleaned = re.sub(r"[\s._-]+", " ", cleaned).strip(" -_.")
    title = cleaned or path.parent.name or path.stem
    return ParsedFilename(title=title, episode=episode)

