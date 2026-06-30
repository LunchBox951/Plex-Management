"""SQLAlchemy 2.0 typed ORM models — the persisted schema (owned by Alembic).

Ten tables back the alpha pipeline: ``users``, ``settings``, ``system_settings``,
``audit_log``, ``media_requests``, ``season_requests``, ``downloads``,
``download_history``, ``blocklist``, ``tmdb_cache``. Column shapes, indexes, and
``ON DELETE`` behaviour follow the persistence design (see the analysis extract's
"Persistence + Schema Migrations" section and ADR-0007).

Conventions:

* Typed style only (``Mapped[...]`` / ``mapped_column``); nullability is inferred
  from whether the ``Mapped`` type includes ``None``.
* ``created_at``-style columns get ``server_default=func.now()`` so bulk inserts
  are timestamped by the database.
* Enum-like columns use ``sa.Enum(PyEnum, native_enum=False)`` (a portable
  VARCHAR + CHECK on SQLite). ``downloads.status`` is a plain indexed ``String``:
  the canonical ``DownloadState`` StrEnum is owned by the P4 state machine and
  writes its values here, and the ``DownloadRecord`` DTO reads it as ``str``.
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
    "MediaRequest",
    "MediaType",
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
    failed = "failed"


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


def _enum(enum_cls: type[StrEnum]) -> sa.Enum:
    """Build a portable, non-native ``Enum`` column type for ``enum_cls``."""
    return sa.Enum(enum_cls, native_enum=False)


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
        # index scoped to the ACTIVE statuses (pending / searching /
        # no_acceptable_release / downloading) makes the second insert raise
        # IntegrityError, which create_request catches and resolves to the existing
        # active request. Terminal rows (completed / available / failed) are
        # excluded, so a fresh request after one finishes is still allowed.
        # The predicate must be supplied per-dialect: ``sqlite_where`` alone is
        # ignored by PostgreSQL, which would then build an UNCONDITIONAL unique
        # index and reject a valid re-request after a prior one reached a terminal
        # status. ``postgresql_where`` carries the SAME predicate so the partial
        # index is honoured on both backends (Postgres is a config swap).
        Index(
            "uq_media_requests_active",
            "tmdb_id",
            "media_type",
            unique=True,
            sqlite_where=sa.text(
                "status IN ('pending', 'searching', 'no_acceptable_release', 'downloading')"
            ),
            postgresql_where=sa.text(
                "status IN ('pending', 'searching', 'no_acceptable_release', 'downloading')"
            ),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    tmdb_id: Mapped[int] = mapped_column(index=True)
    media_type: Mapped[MediaType] = mapped_column(_enum(MediaType))
    title: Mapped[str] = mapped_column(String)
    year: Mapped[int | None] = mapped_column()
    status: Mapped[RequestStatus] = mapped_column(_enum(RequestStatus), index=True)
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


class SeasonRequest(Base):
    """A per-season request belonging to a TV :class:`MediaRequest`."""

    __tablename__ = "season_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    media_request_id: Mapped[int] = mapped_column(
        ForeignKey("media_requests.id", ondelete="CASCADE"), index=True
    )
    season_number: Mapped[int] = mapped_column()
    status: Mapped[RequestStatus] = mapped_column(_enum(RequestStatus), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Download(Base):
    """A tracked torrent. ``status`` holds the P4 ``DownloadState`` value (str)."""

    __tablename__ = "downloads"
    __table_args__ = (
        # At most one ACTIVE download per request — the DB backstop for the
        # app-level parallel-grab guard (which is otherwise a TOCTOU check).
        # Mirrors uq_media_requests_active: terminal statuses are excluded so a
        # fresh grab after one finishes/fails is still allowed, and the predicate
        # is supplied per-dialect so the partial index is honoured on Postgres too.
        Index(
            "uq_downloads_active_request",
            "media_request_id",
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
    media_type: Mapped[MediaType | None] = mapped_column(_enum(MediaType))
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
    event_type: Mapped[DownloadHistoryEvent] = mapped_column(_enum(DownloadHistoryEvent))
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
    source_title: Mapped[str] = mapped_column(Text, index=True)
    indexer: Mapped[str | None] = mapped_column(String)
    protocol: Mapped[str | None] = mapped_column(String)
    media_type: Mapped[MediaType | None] = mapped_column(_enum(MediaType))
    tmdb_id: Mapped[int | None] = mapped_column()
    reason: Mapped[BlocklistReason] = mapped_column(_enum(BlocklistReason))
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
