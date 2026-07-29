from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AppSetting
from .scrapers import SCRAPERS

SCRAPER_NAMES = tuple(SCRAPERS)

DEFAULT_SETTINGS: dict[str, Any] = {
    "enabled_scrapers": list(SCRAPER_NAMES),
    "anidb_client": "",
    "anidb_clientver": 1,
    "dmm_api_id": "",
    "dmm_affiliate_id": "",
    "proxy_url": "",
    "request_interval_seconds": 2.1,
    "scheduled_refresh": False,
}


def enabled_scraper_names(db: Session) -> list[str]:
    row = db.get(AppSetting, "enabled_scrapers")
    configured = row.value if row and isinstance(row.value, list) else list(SCRAPER_NAMES)
    return [name for name in SCRAPER_NAMES if name in configured]


def get_all_settings(db: Session) -> dict[str, Any]:
    result = dict(DEFAULT_SETTINGS)
    for row in db.scalars(select(AppSetting)).all():
        result[row.key] = row.value
    return result
