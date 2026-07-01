"""Repository ports — async persistence interfaces for the domain.

The domain depends on these Protocols, never on SQLAlchemy. The records here are
the cross-boundary read-models the engine / reconciler / web layer consume; the
P2 SQLAlchemy implementations map ORM rows to and from them. Status fields are
plain ``str`` to avoid coupling to the (separately owned) state-machine enum.

Method sets are intentionally minimal — sufficient for the alpha pipeline
(create request -> grab -> reconcile -> blocklist) and nothing more.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

__all__ = [
    "BlocklistRecord",
    "BlocklistRepository",
    "DownloadRecord",
    "DownloadRepository",
    "RequestRecord",
    "RequestRepository",
    "SeasonRequestRecord",
    "SeasonRequestRepository",
]


class RequestRecord(BaseModel):
    """A media request as the domain reads it."""

    model_config = ConfigDict(frozen=True)

    id: int
    tmdb_id: int
    media_type: str
    title: str
    status: str
    year: int | None = None
    is_anime: bool = False
    user_id: int | None = None
    poster_url: str | None = None
    backdrop_url: str | None = None


class DownloadRecord(BaseModel):
    """A tracked download as the domain reads it."""

    model_config = ConfigDict(frozen=True)

    id: int
    torrent_hash: str
    status: str
    media_request_id: int | None = None
    magnet_link: str | None = None
    progress: float = 0.0
    seed_ratio: float = 0.0
    tmdb_id: int | None = None
    year: int | None = None
    season: int | None = None
    # TV only. ``None`` = import every valid video file found; a list = import
    # only those episode numbers, silently skipping the rest (a season-pack grab
    # scoped to specific missing episodes).
    episodes: list[int] | None = None
    failed_reason: str | None = None
    first_seen_at: datetime | None = None
    download_path: str | None = None


class SeasonRequestRecord(BaseModel):
    """A per-season TV request as the domain reads it.

    Mirrors :class:`RequestRecord` at the per-season granularity: one row per
    ``(media_request_id, season_number)``. ``tmdb_id`` is denormalized from the
    parent :class:`RequestRecord` (a per-season join, never a stored column) so
    callers never need a second fetch to know which show a season belongs to.
    """

    model_config = ConfigDict(frozen=True)

    id: int
    media_request_id: int
    season_number: int
    status: str
    tmdb_id: int


class BlocklistRecord(BaseModel):
    """A blocklist entry as the domain reads it."""

    model_config = ConfigDict(frozen=True)

    id: int
    source_title: str
    reason: str
    tmdb_id: int | None = None
    torrent_hash: str | None = None
    indexer: str | None = None
    protocol: str | None = None
    media_type: str | None = None
    added_at: datetime | None = None


@runtime_checkable
class RequestRepository(Protocol):
    """Persistence for media requests."""

    async def get(self, request_id: int) -> RequestRecord | None:
        """Return the request by id, or ``None``."""

    async def list_by_status(self, status: str | None = None) -> list[RequestRecord]:
        """List requests, optionally filtered by ``status``."""
        raise NotImplementedError

    async def find_active(self, tmdb_id: int, media_type: str) -> RequestRecord | None:
        """Return an existing non-terminal request for this media, for dedup."""

    async def find_in_library(self, tmdb_id: int, media_type: str) -> RequestRecord | None:
        """Return the latest already-in-library (available/completed) request.

        Dedups the Plex-availability short-circuit: a repeat request for a movie
        already recorded as available returns that row instead of a duplicate.
        """
        raise NotImplementedError

    async def create(
        self,
        *,
        tmdb_id: int,
        media_type: str,
        title: str,
        status: str,
        year: int | None = None,
        is_anime: bool = False,
        user_id: int | None = None,
        poster_url: str | None = None,
        backdrop_url: str | None = None,
    ) -> RequestRecord:
        """Insert a new request and return the persisted record."""
        raise NotImplementedError

    async def set_status(self, request_id: int, status: str) -> None:
        """Update a request's status."""

    async def mark_completed(self, request_id: int) -> None:
        """Mark a request ``completed`` (imported, scan triggered) + stamp the time.

        The honest pre-``available`` state: the file is in the library and a Plex
        scan was triggered, but Plex has not yet confirmed it is indexed.
        """
        raise NotImplementedError

    async def mark_available(self, request_id: int) -> None:
        """Mark a request ``available`` + stamp ``library_verified_at``.

        Set only once :meth:`LibraryPort.is_available` confirms Plex has indexed
        the title — never asserts watchable before Plex actually has it.
        """
        raise NotImplementedError


@runtime_checkable
class DownloadRepository(Protocol):
    """Persistence for tracked downloads."""

    async def get_by_hash(self, torrent_hash: str) -> DownloadRecord | None:
        """Return the download for ``torrent_hash``, or ``None``."""

    async def find_active_for_request(
        self, media_request_id: int, *, season: int | None = None
    ) -> DownloadRecord | None:
        """Return an existing non-terminal download owned by ``media_request_id``.

        The parallel-grab guard: a request that already has an active (non-terminal)
        download must not spawn a second one for a *different* release, or a later
        failure of either would re-arm the request while the other still runs.

        ``season`` scopes the guard PER SEASON for TV: passing the season being
        grabbed lets a whole-series request have season 1 and season 2 downloading
        at once (each a DIFFERENT ``SeasonRequest`` under the SAME
        ``media_request_id``), while a second release for the SAME season still
        collides. Movies always pass ``season=None``, which matches ``season IS
        NULL`` -- their existing (unscoped) behaviour is unchanged.
        """
        raise NotImplementedError

    async def list_active(self) -> list[DownloadRecord]:
        """List downloads in a non-terminal state (for the reconcile loop)."""
        raise NotImplementedError

    async def create(
        self,
        *,
        torrent_hash: str,
        status: str,
        media_request_id: int | None = None,
        magnet_link: str | None = None,
        tmdb_id: int | None = None,
        year: int | None = None,
        season: int | None = None,
        episodes: list[int] | None = None,
    ) -> DownloadRecord:
        """Insert a new download and return the persisted record.

        ``episodes`` (TV only) persists to ``Download.episodes_json``: ``None``
        means import every valid video file found for the season; an explicit list
        scopes the import to those episode numbers only.
        """
        raise NotImplementedError

    async def update_status(
        self,
        download_id: int,
        status: str,
        *,
        progress: float | None = None,
        seed_ratio: float | None = None,
        failed_reason: str | None = None,
        download_path: str | None = None,
        first_seen_at: datetime | None = None,
        clear_first_seen_at: bool = False,
        clear_failed_reason: bool = False,
        media_request_id: int | None = None,
    ) -> None:
        """Update a download's status and optional progress fields.

        ``first_seen_at`` stamps the missing-grace anchor: the caller passes
        ``now`` when persisting a ``StateTransition`` whose ``set_first_seen_at``
        flag is set, so the reconciler's grace window can actually start.
        ``clear_first_seen_at`` resets the anchor to NULL (distinct from
        ``first_seen_at=None``, which means *leave unchanged*) when a ClientMissing
        torrent recovers, so a later disappearance gets a fresh grace window.

        ``clear_failed_reason`` wipes a stale failure reason when a terminal row is
        reused for a fresh grab; ``media_request_id`` (when not ``None``) re-owns
        the reused row to the current request. Both are no-ops otherwise.
        """


@runtime_checkable
class SeasonRequestRepository(Protocol):
    """Persistence for per-season TV requests.

    A TV ``MediaRequest`` has no lifecycle of its own -- its ``status`` is a
    computed rollup of its ``SeasonRequest`` rows (see
    ``domain.season_rollup.rollup_status``). This is the per-season equivalent of
    :class:`RequestRepository`.
    """

    async def get(self, season_request_id: int) -> SeasonRequestRecord | None:
        """Return the season request by id, or ``None``."""
        raise NotImplementedError

    async def list_for_request(self, media_request_id: int) -> list[SeasonRequestRecord]:
        """List every season row belonging to ``media_request_id``, ordered by season."""
        raise NotImplementedError

    async def list_by_status(self, status: str | None = None) -> list[SeasonRequestRecord]:
        """List season requests, optionally filtered by ``status``."""
        raise NotImplementedError

    async def ensure(
        self, media_request_id: int, season_number: int, *, status: str
    ) -> SeasonRequestRecord:
        """Idempotently return the ``(media_request_id, season_number)`` row.

        Creates it with ``status`` if it does not yet exist; if it already exists,
        returns the EXISTING row unchanged (``status`` is only the value used on
        first creation, never applied to an already-established season).

        Race-safe under the unconditional ``uq_season_requests_media_season``
        unique index: two callers racing to lazily-create the SAME season resolve
        to the SAME single row, mirroring the IntegrityError-catch-and-reread
        pattern at ``request_service.py:159-184``.
        """
        raise NotImplementedError

    async def set_status(self, season_request_id: int, status: str) -> None:
        """Update a season request's status."""
        raise NotImplementedError

    async def mark_completed(self, season_request_id: int) -> None:
        """Mark a season ``completed`` (imported, scan triggered).

        The honest pre-``available`` state, exactly like
        :meth:`RequestRepository.mark_completed` -- the season's file(s) are in the
        library and a Plex scan was triggered, but Plex has not yet confirmed the
        season is indexed.
        """
        raise NotImplementedError

    async def mark_available(self, season_request_id: int) -> None:
        """Mark a season ``available``.

        Set only once :meth:`LibraryPort.is_available` confirms Plex has indexed
        the season (``leafCount>0``) -- never asserts watchable before Plex
        actually has it.
        """
        raise NotImplementedError


@runtime_checkable
class BlocklistRepository(Protocol):
    """Persistence for the failed / reported-bad release blocklist."""

    async def is_blocklisted(
        self,
        tmdb_id: int | None,
        torrent_hash: str | None,
        source_title: str,
        indexer: str | None,
    ) -> bool:
        """Two-tier identity check: hash first, then title/indexer fallback."""
        raise NotImplementedError

    async def list_for_media(self, tmdb_id: int | None = None) -> list[BlocklistRecord]:
        """List blocklist entries, optionally scoped to one media item."""
        raise NotImplementedError

    async def create(
        self,
        *,
        source_title: str,
        reason: str,
        tmdb_id: int | None = None,
        torrent_hash: str | None = None,
        indexer: str | None = None,
        protocol: str | None = None,
        media_type: str | None = None,
    ) -> BlocklistRecord:
        """Insert a new blocklist entry and return the persisted record."""
        raise NotImplementedError

    async def delete(self, blocklist_id: int) -> None:
        """Remove a blocklist entry (operator un-blocklist)."""
