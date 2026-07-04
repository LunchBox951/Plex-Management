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
    # Local/non-Docker startup defaults to loopback so first-run setup cannot be
    # claimed from the network. Docker deployments set PLEX_MANAGER_HOST=0.0.0.0
    # explicitly inside the container and require PLEX_MANAGER_SETUP_TOKEN.
    host: str = "127.0.0.1"
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


_UNSAFE_STARTUP_MESSAGE = (
    "Refusing to start a first-run-capable server without PLEX_MANAGER_SETUP_TOKEN. "
    "Set PLEX_MANAGER_SETUP_TOKEN or explicitly enable PLEX_MANAGER_DEV_AUTH_BYPASS."
)


def _has_setup_token(settings: Settings) -> bool:
    token = settings.setup_token
    return token is not None and bool(token.get_secret_value().strip())


def validate_startup_exposure(settings: Settings) -> None:
    """Refuse startup that would expose tokenless first-run setup.

    With ``dev_auth_bypass`` off and no (non-blank) ``setup_token``, an
    uninitialized server would be unclaimable-yet-exposed: the pre-init setup
    dependencies 401 every request (there is no token to match), while
    ``/setup/status`` honestly advertises that no token is required — a setup
    deadlock. And the alternative — allowing tokenless pre-init — would let
    anyone who can reach the port drive the setup ``validate/*`` probes
    (server-side requests to caller-supplied URLs) and complete first-run setup,
    claiming the install. So the only honest posture is to refuse to serve at
    all until the operator picks one: set a token or explicitly enable the dev
    bypass.

    Called from BOTH launch paths so they cannot diverge: the console entry
    point (``python -m plex_manager`` / the Docker entrypoint) before uvicorn
    starts, and the ASGI ``lifespan`` for anything that serves
    ``plex_manager.web.app:app`` directly (``uvicorn plex_manager.web.app:app``,
    ASGI platforms) and would otherwise skip the ``__main__`` guard entirely.
    """
    if settings.dev_auth_bypass or _has_setup_token(settings):
        return
    raise SystemExit(_UNSAFE_STARTUP_MESSAGE)
