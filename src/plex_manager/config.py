"""Application configuration.

Only low-level bootstrap settings live here. Service credentials (Plex token,
TMDB / Prowlarr / qBittorrent keys) are configured through the in-app setup
wizard and stored encrypted in the database (see ADR-0005), never via
environment variables.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import SecretStr
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

    # The app talks to SQLite asynchronously (aiosqlite). Alembic derives a sync
    # URL from this for migrations — see ``migrations/env.py``.
    database_url: str = "sqlite+aiosqlite:///./data/plex_manager.db"

    # Mounted volume that holds the database and the Fernet key file. Updates and
    # rollbacks never touch it (see the design overview, §6).
    data_dir: str = "./data"

    # Optional override for the at-rest encryption key. When unset, the key is
    # generated once into ``<data_dir>/secret.key`` (mode 0600) on first start.
    # Wrapped in ``SecretStr`` so it never leaks through a log line or ``repr`` of
    # the settings object; read the raw value with ``.get_secret_value()``.
    fernet_key: SecretStr | None = None

    # Skip the API-key check on protected routes. Development convenience only;
    # the :stable deployment leaves this False.
    dev_auth_bypass: bool = False

    # Optional one-time bootstrap token for first-run setup. Docker Compose requires
    # this so an uninitialized host cannot be claimed over the published port.
    setup_token: SecretStr | None = None

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    """Return the cached application settings."""
    return Settings()
