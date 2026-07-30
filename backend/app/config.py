from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Anime Manager"
    database_url: str = "sqlite:///./data/anime.db"
    cache_dir: Path = Path("./data/cache")
    host: str = "127.0.0.1"
    port: int = 18010
    frontend_origin: str = "http://127.0.0.1:5173"
    demo_scrapers: bool = True

    model_config = SettingsConfigDict(
        env_prefix="ANIME_MANAGER_",
        env_file=".env",
        extra="ignore",
    )


settings = Settings()
