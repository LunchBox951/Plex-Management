"""Application configuration.

Only low-level bootstrap settings live here. Service credentials are configured
through the in-app setup wizard and stored encrypted in the database (see
ADR-0005), never via environment variables.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings, loaded from the environment (prefix ``PLEX_MANAGER_``)."""

    model_config = SettingsConfigDict(
        env_prefix="PLEX_MANAGER_",
        env_file=".env",
        extra="ignore",
    )

    app_name: str = "Plex Manager"
    host: str = "0.0.0.0"  # noqa: S104 — binding all interfaces is intentional inside the container
    port: int = 8000
    database_url: str = "sqlite:///./data/plex_manager.db"
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    """Return the cached application settings."""
    return Settings()
