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

from pydantic import Field, SecretStr, field_validator
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

    # The HOST-namespace TCP port docker-compose publishes this container's
    # ``port`` under (docker-compose.yml's ``PLEX_MANAGER_HOST_PORT``, default
    # 8000, mapped via ``ports: ["...:${PLEX_MANAGER_HOST_PORT:-8000}:8000"]``).
    # The compose file ALSO hands the same variable to the container's own
    # ``environment:`` block (mirroring ``downloads_root`` below), so this is
    # populated for every documented docker-compose install -- never guessed.
    # ``None`` means "not running under that compose file" (bare metal, or a
    # hand-rolled ``docker run``/other orchestrator with no equivalent env var
    # set): the startup setup-URL hint (issue #65) then falls back to ``port``
    # and says so explicitly, since a custom port mapping it cannot see would
    # otherwise make that printed link silently wrong.
    host_port: int | None = None

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

    # Read WITHOUT the ``PLEX_MANAGER_`` prefix (``validation_alias``): this is the
    # widely-used process-manager convention (gunicorn's ``uvicorn.workers``
    # image, Heroku, etc.) an operator scaling this app would set to run more
    # than one worker process. The app itself never reads this to size a worker
    # pool (``__main__.py`` always calls ``uvicorn.run`` with no ``workers=``,
    # i.e. one process) — it exists so this convention var is a documented,
    # typed setting (see ``.env.example``) rather than a magic string, feeding
    # the same "warn loudly, never silently" purpose as
    # ``web.app._warn_if_multi_process``/``web.events.warn_if_multiworker``:
    # surfacing when an operator's own process manager (a
    # ``uvicorn --workers>1`` wrapper, or a multi-container/multi-replica
    # deployment that sets this by convention) is about to violate the
    # single-process assumption several in-process registries depend on for
    # correctness (issue #240): ``services.queue_service``'s removal-physics
    # guards (``_removals_in_flight`` / ``_operator_fail_claims``) and
    # ``services.purge_service``'s purge-vs-import path serialization are
    # plain in-process dicts coordinated with no ``await`` between check and
    # register, exactly like ``web.routers.settings``'s ``_rotate_lock`` /
    # ``_settings_update_lock`` already document. A second worker process (or
    # container replica) would silently reopen every race those registries
    # close, because each process/container gets its OWN copy with no
    # cross-process coordination. Deliberately NOT a hard failure: the app
    # remains fully usable single-process without this variable ever being
    # set, and building real multi-process coordination (a DB-level lock/CAS
    # spanning every one of these registries) is out of scope for this fix —
    # the goal is making the violated assumption LOUD at startup, not silent.
    # NOTE: the startup warnings themselves detect this (and its sibling
    # signals WORKERS/UVICORN_WORKERS/GUNICORN_CMD_ARGS) straight from
    # ``os.environ`` via ``web.events.detect_multiworker_signals`` — ONE shared
    # detection both warnings call, rather than each re-deriving its own
    # partial view — so this field's parsed value isn't itself read by that
    # path; it must still never crash ``Settings()`` on a process manager's
    # own non-integer convention (e.g. gunicorn's ``"auto"``), hence the
    # lenient validator below.
    web_concurrency: int = Field(default=1, validation_alias="WEB_CONCURRENCY")

    @field_validator("web_concurrency", mode="before")
    @classmethod
    def _parse_web_concurrency(cls, value: object) -> object:
        """Tolerate a malformed ``WEB_CONCURRENCY`` instead of crashing startup.

        This is purely an ADVISORY field (see its docstring above): the app
        itself never reads it to size anything, it only feeds a best-effort
        startup warning (``web.app._warn_if_multi_process``). Process managers
        set this variable by differing conventions and some use non-integer
        sentinels (e.g. gunicorn's own ``"auto"``) -- a strict ``int`` field
        would make ``Settings()`` raise and take the whole app down over a
        value nothing here actually depends on. Falls back to the default (1,
        "assume single worker") on anything that doesn't parse, mirroring
        ``web.events.warn_if_multiworker()``'s own tolerant ``int(raw)`` +
        ``except ValueError`` parse of the very same variable. A value that IS
        already an ``int`` (e.g. a programmatic ``Settings(web_concurrency=3)``
        in a test) passes through unchanged.
        """
        if isinstance(value, int):
            return value
        if value is None:
            return 1
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return 1

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
