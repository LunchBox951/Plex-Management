"""Application configuration.

Only low-level bootstrap settings live here. Service credentials (Plex token,
TMDB / Prowlarr / qBittorrent keys) are configured through the in-app setup
wizard and stored encrypted in the database (see ADR-0005), never via
environment variables.
"""

from __future__ import annotations

from collections.abc import Sequence
from functools import lru_cache
from typing import Annotated

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


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

    # Container auto-update bootstrap (ADR-0024). Policy lives in the database;
    # these values define only the private sidecar boundary and fixed target.
    image: str = "ghcr.io/lunchbox951/plex-manager:stable"
    container_name: str = "plex-manager"
    updater_secret_file: str | None = None

    # Optional override for the at-rest encryption key. When unset, the key is
    # generated once into ``<data_dir>/secret.key`` (mode 0600) on first start.
    # Wrapped in ``SecretStr`` so it never leaks through a log line or ``repr`` of
    # the settings object; read the raw value with ``.get_secret_value()``.
    fernet_key: SecretStr | None = None

    # Development convenience ONLY; the :stable deployment leaves this False. When
    # True, authenticate_request()/require_setup_admin() return an anonymous
    # AuthContext(method=dev_bypass, is_admin=True) BEFORE any session, CSRF,
    # setup-token, or role/ownership check — every request is a credential-less
    # administrator. Never enable on a shared or network-reachable listener.
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
    # own (unknown, possibly host-default) save directory. ``None`` (unset,
    # bare metal / no Docker split) leaves qBittorrent's own default in charge —
    # unchanged prior behaviour, never a guessed path. There is deliberately no
    # ``/proc/self/mountinfo`` fallback: see
    # ``path_visibility.resolve_downloads_host_root`` for why that field cannot
    # recover a host-namespace path.
    downloads_root: str | None = None

    # Override auth-cookie Secure handling for TLS-terminating reverse proxies.
    # ``None`` means infer from the request scheme.
    auth_cookie_secure: bool | None = None

    # How many trusted reverse-proxy hops sit in front of this app. ``0`` (default)
    # keys the sign-in throttle on ``request.client.host`` alone -- the exact prior
    # behaviour, safe for the documented topology (docker-compose binds 127.0.0.1;
    # an UNCONFIGURED operator proxy makes ``request.client.host`` always the
    # proxy's address). Set to the number of proxies you operate and trust to
    # append to ``X-Forwarded-For`` (usually 1) so the throttle keys on the real
    # client IP instead of collapsing into one global cap an attacker could trip
    # to lock out the real owner. Never set this higher than the number of
    # proxies you control -- anything further left in the header can be forged
    # by the client itself.
    trusted_proxy_hops: int = 0

    # Extra HTTP Host names trusted beyond the always-trusted set (``localhost``
    # plus any loopback/private-range/link-local IP literal — see
    # ``web.trusted_host``). Populate this when a reverse proxy forwards a public
    # hostname; a bare browser only ever sends an IP-literal ``Host`` when it
    # connected to that literal address directly, so IP literals in those ranges
    # cannot be a DNS-rebinding target and need no configuration. The literal
    # ``"*"`` disables Host validation entirely (discouraged; documented escape
    # hatch for unusual topologies). Comma-separated in the environment
    # (``PLEX_MANAGER_ALLOWED_HOSTS=plexmgr.example.com,media.lan``); stored
    # lowercased.
    # NoDecode: pydantic-settings would otherwise try to JSON-decode this
    # (it's a "complex" tuple type) before our validator ever sees the raw
    # comma-separated env string, and fail on plain text. NoDecode hands the
    # validator the raw string untouched.
    allowed_hosts: Annotated[tuple[str, ...], NoDecode] = ()

    log_level: str = "INFO"

    @field_validator("allowed_hosts", mode="before")
    @classmethod
    def _split_allowed_hosts(cls, value: str | Sequence[str] | None) -> tuple[str, ...]:
        """Accept a comma-separated env string (no JSON list required)."""
        if value is None:
            return ()
        if isinstance(value, str):
            return tuple(host.strip().lower() for host in value.split(",") if host.strip())
        return tuple(host.strip().lower() for host in value if host.strip())


@lru_cache
def get_settings() -> Settings:
    """Return the cached application settings."""
    return Settings()
