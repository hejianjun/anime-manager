from .anidb import AniDBScraper
from .base import Candidate, Scraper, SourceMetadata
from .dmm import DMMScraper
from .getchu import GetchuScraper

SCRAPERS: dict[str, Scraper] = {
    "anidb": AniDBScraper(),
    "dmm": DMMScraper(),
    "getchu": GetchuScraper(),
}

__all__ = [
    "AniDBScraper",
    "Candidate",
    "DMMScraper",
    "GetchuScraper",
    "SCRAPERS",
    "Scraper",
    "SourceMetadata",
]
