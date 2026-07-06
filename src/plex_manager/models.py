"""SQLAlchemy 2.0 typed ORM models — the persisted schema (owned by Alembic).

Twelve tables back the alpha + beta pipeline: ``users``, ``settings``,
``system_settings``, ``audit_log``, ``media_requests``, ``request_dedup_locks``,
``season_requests``, ``downloads``, ``download_history``, ``blocklist``,
``tmdb_cache``, and ``log_events``. Column shapes, indexes, and ``ON DELETE``
behaviour follow the persistence design (see the analysis extract's
"Persistence + Schema Migrations" section, ADR-0007, and — for
``log_events``/``library_path``/``keep_forever``/the ``evicted`` status —
ADR-0012).

Conventions:

* Typed style only (``Mapped[...]`` / ``mapped_column``); nullability is inferred
  from whether the ``Mapped`` type includes ``None``.
* ``created_at``-style columns get ``server_default=func.now()`` so bulk inserts
  are timestamped by the database.
* Enum-like columns use named
  ``sa.Enum(PyEnum, native_enum=False, create_constraint=True)`` constraints
  (a portable VARCHAR + CHECK on SQLite/PostgreSQL). ``downloads.status`` is a
  plain indexed ``String``: the canonical ``DownloadState`` StrEnum is owned by
  the P4 state machine and writes its values here, and the ``DownloadRecord`` DTO
  reads it as ``str``.
* Secrets (``users.encrypted_plex_token``) use :class:`EncryptedStr`.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

import sqlalchemy as sa
from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from plex_manager.adapters.encryption import EncryptedStr
from plex_manager.db import Base

__all__ = [
    "AuditLog",
    "Blocklist",
    "BlocklistReason",
    "Download",
    "DownloadHistory",
    "DownloadHistoryEvent",
    "LogEvent",
    "MediaRequest",
    "MediaType",
    "RequestDedupLock",
    "RequestStatus",
    "SeasonRequest",
    "Setting",
    "SystemSettings",
    "TmdbCache",
    "User",
]


# --------------------------------------------------------------------------- #
# Enumerations (stored as their lowercase string values via native_enum=False)
# --------------------------------------------------------------------------- #
class MediaType(StrEnum):
    """Kind of media a request / download / blocklist entry concerns."""

    movie = "movie"
    tv = "tv"


class RequestStatus(StrEnum):
    """Lifecycle of a media (or season) request."""

    pending = "pending"
    searching = "searching"
    no_acceptable_release = "no_acceptable_release"
    downloading = "downloading"
    completed = "completed"
    available = "available"
    # TV-only rollup: some seasons of the show are `available`, others are still
    # in flight. Computed (never set directly by a single-season transition) by
    # the pure ``domain/season_rollup.rollup_status`` and persisted onto the
    # parent ``MediaRequest`` after every season transition. Still "in progress"
    # from the requester's point of view — a season can move it back to fully
    # `available` later — so it stays IN ``uq_media_requests_active`` (keeps
    # blocking a duplicate request for the same show) but stays OUT of
    # ``TERMINAL_REQUEST_STATUS_VALUES`` / ``_SETTLED_REQUEST_STATUSES``.
    partially_available = "partially_available"
    failed = "failed"
    # The download finished but the import was blocked (a bad file or an import
    # error). A surfaced, retryable "needs attention" state — never a silent fail,
    # never a dishonest "downloading". The operator retries the import or rejects
    # the release (blocklist + re-search). Non-terminal, so it keeps dedup-blocking.
    import_blocked = "import_blocked"
    # The disk-pressure eviction sweep (ADR-0012) deleted this title's (movie) or
    # season's placed file to relieve disk pressure — always AFTER it was fully
    # watched, past its grace period, and never pinned (``keep_forever``). It is
    # NON-terminal (the title is honestly re-requestable — a re-request re-grabs
    # it) but, UNLIKE ``partially_available``/``completed``/``import_blocked``
    # above, it is deliberately excluded from ``uq_media_requests_active``'s
    # predicate (like the SETTLED ``available``/``failed`` statuses): the old,
    # now-off-disk row must never block a fresh active request for the same
    # media, so a re-request creates a new row rather than resurrecting this one.
    evicted = "evicted"
    # The operator cancelled a not-yet-imported request (ADR-0014's cancel verb):
    # "I don't want this anymore", distinct from report-issue's "redo it". SETTLED
    # (terminal) and, exactly like ``available``/``failed``/``evicted``, deliberately
    # OUTSIDE ``uq_media_requests_active``'s predicate so a later fresh request for
    # the same media is allowed (the cancelled row is kept only for history). The
    # ``status`` column is a plain VARCHAR (``native_enum=False`` => no CHECK
    # constraint, see ``41d427bd38e6``) and ``cancelled`` (9 chars) fits the existing
    # length, so adding this member needs NO column migration; and because the active
    # partial index is an INCLUSION list that never named ``cancelled``, it is
    # excluded by omission -- no index migration either (mirrors ``evicted``).
    cancelled = "cancelled"


class BlocklistReason(StrEnum):
    """Why a release was blocklisted."""

    failed = "failed"
    bad_quality = "bad_quality"
    wrong_media = "wrong_media"
    user_reported = "user_reported"


class DownloadHistoryEvent(StrEnum):
    """Durable per-torrent history event (the state-recovery anchor)."""

    grabbed = "grabbed"
    import_started = "import_started"
    imported = "imported"
    failed = "failed"
    # The disk-pressure eviction sweep reclaimed this title's/season's file
    # (ADR-0012). Unlike every other member, it is not tied to a torrent —
    # ``DownloadHistory.torrent_hash`` is left ``None`` for an eviction row.
    # ``download_history.event_type`` is a portable non-native enum. The
    # hardening migration's CHECK explicitly includes this value for migrated
    # databases, and ``Base.metadata.create_all`` does the same for fresh ones.
    evicted = "evicted"
    # ADR-0014 correction verbs: the audit row a report-issue / cancel writes. Like
    # ``evicted`` these are not tied to a live torrent (``torrent_hash`` may be the
    # culprit's, or ``None`` when no download ever existed). Same plain-VARCHAR
    # ``event_type`` column, so neither member needs a migration of its own.
    reported = "reported"
    cancelled = "cancelled"


def _enum(enum_cls: type[StrEnum], *, name: str) -> sa.Enum:
    """Build a portable, non-native ``Enum`` column type for ``enum_cls``."""
    return sa.Enum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
    )


# --------------------------------------------------------------------------- #
# Tables
# --------------------------------------------------------------------------- #
class User(Base):
    """A Plex user known to the app (Plex OAuth lands post-alpha)."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    plex_id: Mapped[int | None] = mapped_column(unique=True, index=True)
    username: Mapped[str] = mapped_column(String)
    email: Mapped[str | None] = mapped_column(String)
    avatar_url: Mapped[str | None] = mapped_column(String)
    encrypted_plex_token: Mapped[str | None] = mapped_column(EncryptedStr)
    permissions: Mapped[int] = mapped_column(default=1, server_default=sa.text("1"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_login: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Setting(Base):
    """A single key/value app setting.

    Non-secret config lives in plaintext ``value``. Secret config (service
    credentials such as TMDB / Prowlarr / qBittorrent API keys) MUST be written
    to ``encrypted_value`` — a Fernet-encrypted column (:class:`EncryptedStr`) —
    with ``is_secret=True`` and ``value`` left NULL, so a DB backup never leaks
    the secret at rest (ADR-0005). ``is_secret`` is the discriminator the access
    layer uses to choose the column; it does not by itself protect anything.
    """

    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String, unique=True, index=True)
    # Plaintext home for non-secret values. NULL when the value is a secret.
    value: Mapped[str | None] = mapped_column(Text)
    # Encrypted-at-rest home for secret values. NULL when the value is plaintext.
    encrypted_value: Mapped[str | None] = mapped_column(EncryptedStr)
    # ``sa.false()`` renders the dialect's boolean literal (``0`` on SQLite,
    # ``false`` on PostgreSQL). A bare ``sa.text("0")`` emits ``DEFAULT 0``, which
    # PostgreSQL rejects on a BOOLEAN column — Postgres is a config swap (ADR), so
    # the schema must be dialect-portable.
    is_secret: Mapped[bool] = mapped_column(default=False, server_default=sa.false())
    description: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SystemSettings(Base):
    """Single-row install state: the setup flag + the app API key (encrypted).

    A genuine singleton: the row is pinned to ``id=1`` and a ``CHECK (id = 1)``
    constraint forbids any second row at the database level, so two workers racing
    to initialise an empty DB cannot both insert (the loser hits a PK / CHECK
    violation, caught and resolved to a re-read in :func:`ensure_system_settings`).

    The app API key is **encrypted at rest** (Fernet, via :class:`EncryptedStr`),
    exactly like every other service secret — never stored in plaintext. The
    encryption key lives only in ``data/secret.key`` (file-only, never in the DB),
    so a DB-backup leak cannot yield a usable key. The plaintext is revealed once
    in the ``/setup/complete`` response; thereafter the incoming ``X-Api-Key``
    header is constant-time-compared against the decrypted value (ADR-0005).
    """

    __tablename__ = "system_settings"
    __table_args__ = (sa.CheckConstraint("id = 1", name="ck_system_settings_singleton"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    # ``sa.false()`` renders the dialect's boolean literal (``0`` on SQLite,
    # ``false`` on PostgreSQL); ``sa.text("0")`` would emit ``DEFAULT 0``, rejected
    # by PostgreSQL on a BOOLEAN column.
    initialized: Mapped[bool] = mapped_column(default=False, server_default=sa.false())
    # The app API key, Fernet-encrypted at rest (EncryptedStr) — never plaintext.
    app_api_key: Mapped[str | None] = mapped_column(EncryptedStr)
    setup_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    setup_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    """An immutable record of a state-changing action."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    action_type: Mapped[str] = mapped_column(String, index=True)
    entity_type: Mapped[str] = mapped_column(String)
    entity_id: Mapped[int | None] = mapped_column(index=True)
    old_value: Mapped[dict[str, Any] | None] = mapped_column(sa.JSON)
    new_value: Mapped[dict[str, Any] | None] = mapped_column(sa.JSON)
    description: Mapped[str | None] = mapped_column(String)
    ip_address: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class MediaRequest(Base):
    """A user request for a movie or TV show — the entity the reconciler polls."""

    __tablename__ = "media_requests"
    __table_args__ = (
        Index("ix_media_requests_tmdb_media", "tmdb_id", "media_type"),
        # Serialize active-request dedup at the database level: two concurrent
        # POST /requests for the same (tmdb_id, media_type) can both pass the
        # application-level find_active() check, then both INSERT. A PARTIAL UNIQUE
        # index scoped to the statuses that must keep blocking a duplicate request
        # (pending / searching / no_acceptable_release / downloading /
        # import_blocked / completed / partially_available) makes the second
        # insert raise IntegrityError, which create_request catches and resolves to
        # the existing active request. SETTLED statuses are deliberately OUTSIDE the
        # predicate, so a fresh request after one finishes is still allowed:
        # ``available`` / ``failed`` (a normal finished/dead request), and — as of
        # ADR-0012 — ``evicted`` (the file was deleted by the disk-pressure sweep;
        # the old row must never block a fresh re-request that actually re-grabs
        # the content; see ``RequestStatus.evicted``'s docstring). The predicate
        # must be supplied per-dialect: ``sqlite_where`` alone is ignored by
        # PostgreSQL, which would then build an UNCONDITIONAL unique index and
        # reject a valid re-request after a prior one reached a settled status.
        # ``postgresql_where`` carries the SAME predicate so the partial index is
        # honoured on both backends (Postgres is a config swap).
        Index(
            "uq_media_requests_active",
            "tmdb_id",
            "media_type",
            unique=True,
            sqlite_where=sa.text(
                "status IN ('pending', 'searching', 'no_acceptable_release', "
                "'downloading', 'import_blocked', 'completed', 'partially_available')"
            ),
            postgresql_where=sa.text(
                "status IN ('pending', 'searching', 'no_acceptable_release', "
                "'downloading', 'import_blocked', 'completed', 'partially_available')"
            ),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    tmdb_id: Mapped[int] = mapped_column(index=True)
    media_type: Mapped[MediaType] = mapped_column(
        _enum(MediaType, name="ck_media_requests_media_type_enum")
    )
    title: Mapped[str] = mapped_column(String)
    year: Mapped[int | None] = mapped_column()
    status: Mapped[RequestStatus] = mapped_column(
        _enum(RequestStatus, name="ck_media_requests_status_enum"), index=True
    )
    is_anime: Mapped[bool | None] = mapped_column()
    # TMDB art persisted at request time so Requests / Queue rows can render a
    # poster (and the detail backdrop) without a per-row TMDB re-fetch.
    poster_url: Mapped[str | None] = mapped_column(String)
    backdrop_url: Mapped[str | None] = mapped_column(String)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    library_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    library_removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The final placed path the importer wrote this movie into (ADR-0012),
    # captured at import/availability time and STORED — never reconstructed from
    # naming at eviction time, which would be fragile. The disk-pressure eviction
    # sweep ``fs.delete()``s exactly this path. ``None`` for rows that predate this
    # breadcrumb (or a tv rollup row, where the breadcrumb lives per-season on
    # ``SeasonRequest`` instead) — eviction skips + logs rather than guessing one.
    library_path: Mapped[str | None] = mapped_column(String)
    # Operator pin (ADR-0012): a pinned title is NEVER selected by
    # ``domain/eviction.py``, regardless of watch state or disk pressure. Movie or
    # (whole) show granularity — TV eviction is scoped per season on
    # ``SeasonRequest``, but the pin itself lives on the parent show, so pinning a
    # series protects every one of its seasons.
    keep_forever: Mapped[bool] = mapped_column(default=False, server_default=sa.false())
    # Auto-grab scheduling (ADR-0013). ``search_attempts`` counts the number of
    # background auto-grab searches that returned nothing acceptable for this
    # (movie) request; it drives the escalating per-scope backoff. ``next_search_at``
    # is the earliest instant the auto-grab worker may search this request again --
    # ``NULL`` means "due now" (a freshly-created request is searched on the next
    # tick). Both are movie-scoped here; the TV mirror lives on ``SeasonRequest``,
    # since a TV grab is always per-season. Only touched by the auto-grab worker;
    # the manual grab path never reads or writes them.
    search_attempts: Mapped[int] = mapped_column(default=0, server_default=sa.text("0"))
    next_search_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RequestDedupLock(Base):
    """Serializable lock row for per-media request decisions.

    PostgreSQL's MVCC can let two concurrent transactions both miss an uncommitted
    terminal ``available`` row. The active partial unique index intentionally
    excludes ``available``, so in-library short-circuits need a separate row to lock
    before checking/inserting terminal request records.
    """

    __tablename__ = "request_dedup_locks"

    tmdb_id: Mapped[int] = mapped_column(primary_key=True)
    media_type: Mapped[MediaType] = mapped_column(
        _enum(MediaType, name="ck_request_dedup_locks_media_type_enum"), primary_key=True
    )


class SeasonRequest(Base):
    """A per-season request belonging to a TV :class:`MediaRequest`."""

    __tablename__ = "season_requests"
    __table_args__ = (
        # A show can never have two rows for the same season, regardless of
        # status — unconditional (no WHERE), unlike the request/download active
        # indexes. This is what makes ``SeasonRequestRepository.ensure()``
        # race-safe: two concurrent grabs racing to lazily-create the SAME season
        # row raise IntegrityError, caught and resolved to a re-read (mirrors the
        # IntegrityError-catch-and-reread pattern at ``request_service.py:159-184``).
        Index(
            "uq_season_requests_media_season",
            "media_request_id",
            "season_number",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    media_request_id: Mapped[int] = mapped_column(
        ForeignKey("media_requests.id", ondelete="CASCADE"), index=True
    )
    season_number: Mapped[int] = mapped_column()
    status: Mapped[RequestStatus] = mapped_column(
        _enum(RequestStatus, name="ck_season_requests_status_enum"), index=True
    )
    # The final placed path this season's import wrote into — the per-season
    # mirror of ``MediaRequest.library_path`` (ADR-0012): same "store, never
    # reconstruct" rule, same eviction target. ``None`` for seasons imported
    # before this breadcrumb existed.
    library_path: Mapped[str | None] = mapped_column(String)
    # Auto-grab scheduling (ADR-0013) — the per-season mirror of the identically
    # named columns on ``MediaRequest``. A TV grab is always per-season, so the
    # backoff ladder is tracked here (not on the parent, whose status is a computed
    # rollup). ``search_attempts`` counts nothing-acceptable searches for this
    # season; ``next_search_at`` gates the next auto-grab search (``NULL`` = due now).
    search_attempts: Mapped[int] = mapped_column(default=0, server_default=sa.text("0"))
    next_search_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Download(Base):
    """A tracked torrent. ``status`` holds the P4 ``DownloadState`` value (str)."""

    __tablename__ = "downloads"
    __table_args__ = (
        # At most one ACTIVE download per (request, season) — the DB backstop for
        # the app-level parallel-grab guard (which is otherwise a TOCTOU check).
        # Mirrors uq_media_requests_active: terminal statuses are excluded so a
        # fresh grab after one finishes/fails is still allowed, and the predicate
        # is supplied per-dialect so the partial index is honoured on Postgres too.
        #
        # Widened from a plain (media_request_id) unique to (media_request_id,
        # COALESCE(season, -1)) so whole-series TV requests can grab S1 and S2
        # concurrently (two DIFFERENT SeasonRequest rows under the SAME
        # MediaRequest) without tripping the guard. A bare (media_request_id,
        # season) unique index would NOT work for movies: SQL NULL is never equal
        # to NULL, so two movie downloads (season IS NULL on both) would no longer
        # collide, silently reopening the very TOCTOU the index exists to close.
        # The COALESCE(-1) sentinel folds every movie row onto the SAME synthetic
        # key, restoring the single-active-movie-download guarantee, while real
        # (non-NULL) season values keep TV downloads scoped per season.
        Index(
            "uq_downloads_active_request",
            "media_request_id",
            sa.func.coalesce(sa.column("season", sa.Integer()), -1),
            unique=True,
            sqlite_where=sa.text(
                "media_request_id IS NOT NULL "
                "AND status NOT IN ('imported', 'failed', 'no_acceptable_release')"
            ),
            postgresql_where=sa.text(
                "media_request_id IS NOT NULL "
                "AND status NOT IN ('imported', 'failed', 'no_acceptable_release')"
            ),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    media_request_id: Mapped[int | None] = mapped_column(
        ForeignKey("media_requests.id", ondelete="SET NULL")
    )
    torrent_hash: Mapped[str] = mapped_column(String, unique=True, index=True)
    # The grab source (magnet OR Prowlarr download url). A Prowlarr download url
    # embeds the Prowlarr api key as a query param, so this is a secret at rest:
    # it is Fernet-encrypted (:class:`EncryptedStr`) like the other credentials
    # (ADR-0005), never returned in any response and never logged.
    magnet_link: Mapped[str | None] = mapped_column(EncryptedStr)
    status: Mapped[str] = mapped_column(String, index=True)
    progress: Mapped[float] = mapped_column(default=0.0, server_default=sa.text("0"))
    seed_ratio: Mapped[float] = mapped_column(default=0.0, server_default=sa.text("0"))
    target_seed_ratio: Mapped[float] = mapped_column(default=1.0, server_default=sa.text("1"))
    media_type: Mapped[MediaType | None] = mapped_column(
        _enum(MediaType, name="ck_downloads_media_type_enum")
    )
    tmdb_id: Mapped[int | None] = mapped_column()
    year: Mapped[int | None] = mapped_column()
    season: Mapped[int | None] = mapped_column()
    episodes_json: Mapped[list[Any] | None] = mapped_column(sa.JSON)
    timeout_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_reason: Mapped[str | None] = mapped_column(String)
    retry_count: Mapped[int] = mapped_column(default=0, server_default=sa.text("0"))
    torrent_attempt: Mapped[int] = mapped_column(default=1, server_default=sa.text("1"))
    scored_releases_json: Mapped[list[dict[str, Any]] | None] = mapped_column(sa.JSON)
    download_path: Mapped[str | None] = mapped_column(String)


class DownloadHistory(Base):
    """Append-only per-torrent event log — the durable state-recovery anchor."""

    __tablename__ = "download_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    tmdb_id: Mapped[int | None] = mapped_column()
    torrent_hash: Mapped[str | None] = mapped_column(String, index=True)
    event_type: Mapped[DownloadHistoryEvent] = mapped_column(
        _enum(DownloadHistoryEvent, name="ck_download_history_event_type_enum")
    )
    source_title: Mapped[str | None] = mapped_column(Text)
    quality_json: Mapped[dict[str, Any] | None] = mapped_column(sa.JSON)
    indexer: Mapped[str | None] = mapped_column(String)
    message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Blocklist(Base):
    """A failed / reported-bad release. Checked before every grab (two-tier id)."""

    __tablename__ = "blocklist"

    id: Mapped[int] = mapped_column(primary_key=True)
    torrent_hash: Mapped[str | None] = mapped_column(String, index=True)
    source_title: Mapped[str] = mapped_column(Text)
    indexer: Mapped[str | None] = mapped_column(String)
    protocol: Mapped[str | None] = mapped_column(String)
    media_type: Mapped[MediaType | None] = mapped_column(
        _enum(MediaType, name="ck_blocklist_media_type_enum")
    )
    tmdb_id: Mapped[int | None] = mapped_column()
    reason: Mapped[BlocklistReason] = mapped_column(
        _enum(BlocklistReason, name="ck_blocklist_reason_enum")
    )
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class TmdbCache(Base):
    """A TTL cache of TMDB responses (swept on startup by ``expires_at``)."""

    __tablename__ = "tmdb_cache"

    id: Mapped[int] = mapped_column(primary_key=True)
    cache_key: Mapped[str] = mapped_column(String, unique=True, index=True)
    cache_type: Mapped[str] = mapped_column(String)
    data_json: Mapped[dict[str, Any]] = mapped_column(sa.JSON)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LogEvent(Base):
    """A captured application log record — the durable, LLM-diagnosable trail
    (ADR-0012).

    Populated ONLY by the capture pipeline's background drain task, never
    written to directly by the (synchronous) logging handler: the handler pushes
    onto an in-process ``asyncio.Queue``, and this async drain task is what
    batch-inserts rows here, so a DB write never happens on the handler's call
    stack (no event-loop blocking, no reentrancy). Only INFO-and-above records
    reach this table; DEBUG and below live only in the in-memory ring buffer (the
    live all-levels tail), never persisted. Rows are pruned by a periodic
    retention sweep (the web-editable ``log_retention_days`` setting), so growth
    is bounded.

    ``context_json`` carries correlation ids (e.g. ``request_id`` / ``download_id``
    / ``tmdb_id``) set at key decision points (reconcile failure, adapter outage,
    import block, grab failure) so ``GET /ops/logs/export`` can assemble one
    coherent trail for a single failure, or for a time window, to paste into an
    LLM. Never a secret-bearing column: log call sites are responsible for never
    logging a credential (honesty over silence never trades away that rule).
    """

    __tablename__ = "log_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    level: Mapped[str] = mapped_column(String)
    logger: Mapped[str] = mapped_column(String)
    message: Mapped[str] = mapped_column(Text)
    context_json: Mapped[dict[str, Any] | None] = mapped_column(sa.JSON)
