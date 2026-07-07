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
        # Treat a blank env var (``PLEX_MANAGER_AUTH_COOKIE_SECURE=``) as UNSET, so a
        # docs-following install that copies ``.env.example`` verbatim boots on the
        # field defaults instead of failing validation. Without this, an empty
        # string reaching a ``bool | None`` / ``int`` field (auth_cookie_secure,
        # port, dev_auth_bypass) raises at startup. Audited safe: every optional /
        # secret field already treats "" as falsy (== its ``None``/default meaning),
        # and no field assigns empty-string a distinct meaning — so ignoring blanks
        # never changes behavior for any other knob.
        env_ignore_empty=True,
    )

    app_name: str = "Plex Manager"
    # Local/non-Docker startup defaults to loopback so first-run setup cannot be
    # claimed from the network. Docker deployments set PLEX_MANAGER_HOST=0.0.0.0
    # explicitly inside the container; first-run setup is then claimed by the
    # first Plex server owner to sign in.
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

    # Optional hardening: when set, first-run setup additionally requires this
    # token. Never required — default installs claim setup via the first Plex
    # owner sign-in.
    setup_token: SecretStr | None = None

    # The HOST-namespace directory docker-compose binds to this container's
    # /downloads (docker-compose.yml's ``PLEX_MANAGER_DOWNLOADS_ROOT`` bind
    # source — ``env_file: .env`` hands the SAME variable to the container, so
    # it is already sitting in the environment, just unread until now). Used to
    # DIRECT qBittorrent's per-add ``save_path`` (issues #133/#157): qBittorrent
    # runs on the HOST, so torrents must be told to land under a path this
    # container's ``/downloads`` mount actually backs, rather than qBittorrent's
    # own (unknown, possibly host-default) save directory. ``None`` (unset) falls
    # back to a best-effort ``/proc/self/mountinfo`` lookup
    # (``path_visibility.host_downloads_root_from_mountinfo``); when neither
    # resolves (bare metal, no Docker split), qBittorrent's own default is left in
    # charge — unchanged prior behaviour, never a guessed path.
    downloads_root: str | None = None

    # Override auth-cookie Secure handling for TLS-terminating reverse proxies.
    # ``None`` means infer from the request scheme.
    auth_cookie_secure: bool | None = None

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    """Return the cached application settings."""
    return Settings()
