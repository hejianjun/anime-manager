from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.main import enabled_scraper_names
from app.models import AppSetting
from app.source_settings import get_all_settings


def test_all_scrapers_are_enabled_by_default() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        assert enabled_scraper_names(db) == ["anidb", "dmm", "getchu"]


def test_enabled_scrapers_are_filtered_and_returned_in_registry_order() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        db.add(
            AppSetting(
                key="enabled_scrapers",
                value=["getchu", "unknown", "anidb"],
            )
        )
        db.commit()

        assert enabled_scraper_names(db) == ["anidb", "getchu"]


def test_translation_settings_have_openai_compatible_defaults() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        settings = get_all_settings(db)

    assert settings["translation_base_url"].endswith("/compatible-mode/v1")
    assert settings["translation_provider"] == "openai"
    assert settings["auto_translate_description"] is False
    assert settings["translation_model"] == "qwen-max"
    assert settings["translation_api_key"] == ""
    assert settings["translation_timeout_seconds"] == 60
    assert settings["tmt_region"] == "ap-guangzhou"
