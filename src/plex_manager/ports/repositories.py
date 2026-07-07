"""Repository ports — async persistence interfaces for the domain.

The domain depends on these Protocols, never on SQLAlchemy. The records here are
the cross-boundary read-models the engine / reconciler / web layer consume; the
P2 SQLAlchemy implementations map ORM rows to and from them. Status fields are
plain ``str`` to avoid coupling to the (separately owned) state-machine enum.

Method sets are intentionally minimal — sufficient for the alpha pipeline
(create request -> grab -> reconcile -> blocklist) and nothing more; the
operability beta (ADR-0012) adds exactly what its three features need: the
``library_path``/``keep_forever`` breadcrumb + pin on the request/season repos,
and the ``LogEvent`` repository backing the durable, LLM-diagnosable log store.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

__all__ = [
    "LOG_EVENT_CORRELATION_KEYS",
    "BlocklistRecord",
    "BlocklistRepository",
    "DownloadRecord",
    "DownloadRepository",
    "LogEventCreate",
    "LogEventPage",
    "LogEventRecord",
    "LogEventRepository",
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
    # The final placed path the importer wrote this movie into (ADR-0012) --
    # ``None`` until import/availability time sets it (or for a tv rollup row,
    # where the breadcrumb lives per-season on ``SeasonRequestRecord`` instead).
    library_path: str | None = None
    # Operator pin (ADR-0012): ``True`` means ``domain/eviction.py`` must never
    # select this title, regardless of watch state or disk pressure.
    keep_forever: bool = False
    # Auto-grab scheduling (ADR-0013): the escalating-backoff bookkeeping the
    # background worker reads to decide the next search delay. ``search_attempts``
    # counts nothing-acceptable searches so far; ``next_search_at`` is the earliest
    # instant the worker may search again (``None`` = due now). Movie-scoped; the
    # TV mirror lives on ``SeasonRequestRecord``.
    search_attempts: int = 0
    next_search_at: datetime | None = None
    # Provenance marker (ADR-0012, issue #156): ``True`` only for a row THIS app's
    # own eviction-guard fall-through created -- never an operator-initiated
    # request (in particular never a #148 forced re-acquire). See
    # ``MediaRequest.eviction_regrab``'s docstring for the full rationale.
    eviction_regrab: bool = False


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
    media_type: str | None = None
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
    # The per-season mirror of ``RequestRecord.library_path`` (ADR-0012): the
    # final placed path this season's import wrote into, ``None`` until set.
    library_path: str | None = None
    # Auto-grab scheduling (ADR-0013): the per-season mirror of
    # ``RequestRecord.search_attempts`` / ``next_search_at`` -- a TV grab is always
    # per-season, so the backoff ladder is tracked here.
    search_attempts: int = 0
    next_search_at: datetime | None = None
    # The season-level mirror of ``RequestRecord.eviction_regrab`` (issue #156):
    # ``True`` only for a season row ``season_request_service.ensure_seasons``
    # created because Plex reported it present yet its newest tracked history was
    # ``evicted`` -- the season-level eviction guard's own re-grab. See
    # ``SeasonRequest.eviction_regrab``'s docstring.
    eviction_regrab: bool = False


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


class LogEventRecord(BaseModel):
    """A captured log record as the log viewer / export reads it (ADR-0012).

    ``context`` carries correlation ids (e.g. ``request_id`` / ``download_id`` /
    ``tmdb_id``) set at the log call site -- see ``models.LogEvent`` for the full
    rationale. Never a secret-bearing field: the capture pipeline and every call
    site logging into it are responsible for that, this DTO just carries it
    through unexamined.
    """

    model_config = ConfigDict(frozen=True)

    id: int
    created_at: datetime
    level: str
    logger: str
    message: str
    context: dict[str, Any] | None = None


class LogEventCreate(BaseModel):
    """One record to persist -- the unit :meth:`LogEventRepository.create_many`
    batch-inserts.

    ``created_at`` is the ORIGINAL time the underlying ``logging.LogRecord`` was
    emitted (``record.created``), not the time the drain task happens to flush
    it -- a batch can hold several records emitted seconds apart, and losing that
    distinction would misorder an export's reconstructed trail.
    """

    model_config = ConfigDict(frozen=True)

    created_at: datetime
    level: str
    logger: str
    message: str
    context: dict[str, Any] | None = None


class LogEventPage(BaseModel):
    """One page of :meth:`LogEventRepository.list_events` results.

    ``total`` is the count of rows matching the filter (not the whole table), so
    a caller can tell whether more pages exist beyond ``results``.
    """

    model_config = ConfigDict(frozen=True)

    total: int
    results: list[LogEventRecord]


@runtime_checkable
class RequestRepository(Protocol):
    """Persistence for media requests."""

    async def get(self, request_id: int) -> RequestRecord | None:
        """Return the request by id, or ``None``."""

    async def list_by_status(self, status: str | None = None) -> list[RequestRecord]:
        """List requests, optionally filtered by ``status``."""
        raise NotImplementedError

    async def list_due_for_search(
        self, statuses: frozenset[str], now: datetime
    ) -> list[RequestRecord]:
        """List MOVIE requests due for an auto-grab search (ADR-0013).

        Returns every ``media_type == 'movie'`` request whose ``status`` is in
        ``statuses`` AND whose ``next_search_at`` is either ``NULL`` (never
        scheduled -> due now, so a freshly created request is searched on the
        next tick) or ``<= now``. Ordered NULLs-first then oldest-due-first so a
        per-cycle search cap picks the most-overdue scopes. TV requests are
        deliberately excluded -- a TV grab is per-season, driven by
        :meth:`SeasonRequestRepository.list_due_for_search` instead.
        """
        raise NotImplementedError

    async def schedule_search(
        self, request_id: int, *, search_attempts: int, next_search_at: datetime | None
    ) -> None:
        """Persist the auto-grab backoff bookkeeping for a request (ADR-0013).

        Sets ``search_attempts`` and ``next_search_at`` together; never touches
        ``status`` (the caller drives that via the honest state transitions).
        """
        raise NotImplementedError

    async def find_active(self, tmdb_id: int, media_type: str) -> RequestRecord | None:
        """Return an existing non-terminal request for this media, for dedup."""

    async def find_in_library(
        self, tmdb_id: int, media_type: str, *, prefer_user_id: int | None = None
    ) -> RequestRecord | None:
        """Return an already-in-library (available/completed) request for dedup.

        Dedups the Plex-availability short-circuit: a repeat request for a movie
        already recorded as available returns that row instead of a duplicate.

        ``prefer_user_id`` scopes WHICH terminal row is returned when several
        exist for the same media (a legitimate state — see the remove-then-
        reacquire flow): a row owned by that user is preferred, then an ownerless
        (claimable) one, then anyone else's; newest-by-id within each rank. This
        is the per-user visibility rule for shared (non-admin) sessions — without
        it, another user's NEWER terminal row shadows the caller's own older one
        and turns their re-request into a spurious ``requested_by_another_user``
        rejection. ``None`` (the default; admins and API-key automation) returns
        the newest row unconditionally, the pre-preference behavior.
        """
        raise NotImplementedError

    async def latest_request_evicted(self, tmdb_id: int, media_type: str) -> bool:
        """Whether the NEWEST request row for this media is ``evicted`` (ADR-0012).

        Lets the in-library short-circuit refuse to trust a STALE Plex 'present'
        reading during the eviction delete window (the sweep commits ``evicted``
        before it unlinks the file and before the post-delete Plex refresh), so a
        re-request re-grabs instead of minting an ``available`` row over a doomed
        file. See ``SqlRequestRepository.latest_request_evicted``'s docstring.
        """
        raise NotImplementedError

    async def display_statuses_by_tmdb_ids(
        self, keys: Sequence[tuple[int, str]], *, for_user_id: int | None = None
    ) -> dict[tuple[int, str], str]:
        """Batch the DISPLAY request status per ``(tmdb_id, media_type)`` for tiles.

        The batched analogue of :meth:`find_active` for Discover/Search tile
        decoration: one query answers a whole page's keys instead of a per-title
        fan-out. Unlike ``find_active`` it returns the DISPLAY status -- a
        non-settled/active row wins, else the newest row by id (mirroring the title
        modal's ``liveRequest`` selection) -- so a settled ``available`` IS returned
        (``find_active`` deliberately excludes it). For TV the parent
        ``MediaRequest.status`` carries the persisted per-season rollup
        (``partially_available``/...), so no per-season fan-out is needed. Keys with
        no request row are simply ABSENT from the mapping (never a fabricated status).

        ``for_user_id`` scopes the lookup to ONE user's own request rows -- the
        per-user visibility rule for shared (non-admin) sessions, mirroring the
        requests list/get filtering exactly (``user_id == for_user_id``; ownerless
        rows are excluded too, just as the list hides them). ``None`` (the
        default) is unscoped: admins and API-key automation see every row.
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
        eviction_regrab: bool = False,
    ) -> RequestRecord:
        """Insert a new request and return the persisted record.

        ``eviction_regrab`` (issue #156) stamps the provenance marker: ``True``
        only when THIS insert is the eviction-guard fall-through (``request_
        service.create_request``'s ``latest_request_evicted`` branch), never for
        an ordinary or forced (#148) request.
        """
        raise NotImplementedError

    async def set_status(self, request_id: int, status: str) -> None:
        """Update a request's status."""

    async def set_status_if_in(
        self,
        request_id: int,
        status: str,
        allowed_from: frozenset[str],
        *,
        require_unpinned: bool = False,
    ) -> bool:
        """Compare-and-swap: move to ``status`` only if currently in ``allowed_from``
        (and, with ``require_unpinned``, only if not ``keep_forever``-pinned).

        Returns whether the row was actually updated -- ``False`` means a
        genuinely concurrent writer already moved it elsewhere (or pinned it). The
        eviction sweep's authoritative double-count guard (ADR-0012, C6) and, with
        ``require_unpinned``, its pre-delete pin-safe CLAIM (#67): see
        ``SqlRequestRepository.set_status_if_in``'s docstring.
        """
        raise NotImplementedError

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

    async def set_library_path(self, request_id: int, library_path: str) -> None:
        """Store the final placed path this request's import wrote into (ADR-0012).

        Set once at import/availability time and never reconstructed later — the
        disk-pressure eviction sweep ``fs.delete()``s exactly this path, so a
        wrong or stale value here would misdirect (or silently skip) an eviction.
        """
        raise NotImplementedError

    async def set_keep_forever(self, request_id: int, keep_forever: bool) -> None:
        """Set the operator's "keep forever" pin (ADR-0012).

        ``True`` means ``domain/eviction.py`` must never select this title,
        regardless of watch state or disk pressure; the toggle endpoint passes
        the desired value directly rather than this method inferring a flip, so a
        double-submit (e.g. a retried request) is idempotent.
        """
        raise NotImplementedError

    async def set_keep_forever_for_title(
        self, tmdb_id: int, media_type: str, keep_forever: bool
    ) -> None:
        """Set the pin on EVERY ``MediaRequest`` row for this ``(tmdb_id,
        media_type)``, not just one (ADR-0012).

        ``uq_media_requests_active`` only constrains ACTIVE rows, so a title
        commonly has several rows over its lifetime -- e.g. an older SETTLED
        ``available`` request covering seasons 1-2 and a newer ACTIVE request
        for season 3. ``domain/eviction.py``'s ``_season_candidates`` reads
        ``keep_forever`` off EACH season's OWN parent row, so pinning only the
        one row the UI resolved to (the active one) would leave the settled
        sibling's seasons unpinned and still evictable after the operator
        believes they pinned the whole show. Keep-forever is a per-TITLE
        intent, so this updates every row sharing the key, symmetric for both
        pin and unpin.
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
        media_type: str | None = None,
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
        clear_download_path: bool = False,
        media_request_id: int | None = None,
        replace_grab_metadata: bool = False,
        magnet_link: str | None = None,
        tmdb_id: int | None = None,
        year: int | None = None,
        season: int | None = None,
        episodes: list[int] | None = None,
        media_type: str | None = None,
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

    async def list_for_requests(
        self, media_request_ids: Sequence[int]
    ) -> dict[int, list[SeasonRequestRecord]]:
        """Batch-list season rows for MULTIPLE requests in a single query.

        Returns ``{media_request_id: [SeasonRequestRecord, ...]}``; a request id
        with no tracked seasons (a movie, or absent from ``media_request_ids``)
        is simply absent from the mapping rather than present with an empty list.
        Lets ``GET /requests`` embed every tv row's per-season rollup WITHOUT one
        query per row (the ``_to_response`` N+1 :meth:`list_for_request` would
        otherwise cause on a list endpoint).
        """
        raise NotImplementedError

    async def list_by_status(self, status: str | None = None) -> list[SeasonRequestRecord]:
        """List season requests, optionally filtered by ``status``."""
        raise NotImplementedError

    async def evicted_seasons(self, tmdb_id: int) -> frozenset[int]:
        """Season numbers whose NEWEST row (across this ``tmdb_id``'s ``tv``
        requests) is ``evicted`` (ADR-0012).

        ``ensure_seasons`` subtracts these from Plex's ``present_seasons`` snapshot
        so a season the disk-pressure sweep is mid-deleting is never created or
        re-armed straight to ``available`` off a STALE 'present' reading -- the
        season-level twin of :meth:`RequestRepository.latest_request_evicted`. See
        ``SqlSeasonRequestRepository.evicted_seasons``'s docstring.
        """
        raise NotImplementedError

    async def list_due_for_search(
        self, statuses: frozenset[str], now: datetime
    ) -> list[SeasonRequestRecord]:
        """List season requests due for an auto-grab search (ADR-0013).

        The per-season mirror of :meth:`RequestRepository.list_due_for_search`:
        every season whose ``status`` is in ``statuses`` and whose
        ``next_search_at`` is ``NULL`` (due now) or ``<= now``, ordered
        NULLs-first then oldest-due-first. Each record carries its parent show's
        denormalized ``tmdb_id`` (as everywhere in this repo).
        """
        raise NotImplementedError

    async def schedule_search(
        self, season_request_id: int, *, search_attempts: int, next_search_at: datetime | None
    ) -> None:
        """Persist a season's auto-grab backoff bookkeeping (ADR-0013).

        The per-season mirror of :meth:`RequestRepository.schedule_search`; never
        touches ``status`` and never recomputes the parent rollup (the backoff
        columns do not feed ``rollup_status``).
        """
        raise NotImplementedError

    async def ensure(
        self,
        media_request_id: int,
        season_number: int,
        *,
        status: str,
        eviction_regrab: bool = False,
    ) -> SeasonRequestRecord:
        """Idempotently return the ``(media_request_id, season_number)`` row.

        Creates it with ``status`` if it does not yet exist; if it already exists,
        returns the EXISTING row unchanged (``status`` is only the value used on
        first creation, never applied to an already-established season).
        ``eviction_regrab`` (issue #156) is likewise only applied on first
        creation -- see ``SeasonRequest.eviction_regrab``'s docstring.

        Race-safe under the unconditional ``uq_season_requests_media_season``
        unique index: two callers racing to lazily-create the SAME season resolve
        to the SAME single row, mirroring the IntegrityError-catch-and-reread
        pattern at ``request_service.py:159-184``.
        """
        raise NotImplementedError

    async def set_status(self, season_request_id: int, status: str) -> None:
        """Update a season request's status."""
        raise NotImplementedError

    async def set_status_if_in(
        self,
        season_request_id: int,
        status: str,
        allowed_from: frozenset[str],
        *,
        require_parent_unpinned: bool = False,
    ) -> bool:
        """Compare-and-swap: move to ``status`` only if currently in ``allowed_from``
        (and, with ``require_parent_unpinned``, only if the PARENT show is not
        ``keep_forever``-pinned).

        The season-granularity mirror of ``RequestRepository.set_status_if_in``;
        see ``SqlSeasonRequestRepository.set_status_if_in``'s docstring.
        """
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

    async def set_library_path(self, season_request_id: int, library_path: str) -> None:
        """Store the final placed path this season's import wrote into (ADR-0012).

        The per-season mirror of :meth:`RequestRepository.set_library_path` --
        same "set once, never reconstruct" rule, same eviction target.
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
        *,
        media_type: str | None = None,
    ) -> bool:
        """Two-tier identity check: hash first, then title/indexer fallback.

        ``media_type`` scopes to one TMDB namespace (movie/tv share id spaces); see
        ``SqlBlocklistRepository._media_type_scope``.
        """
        raise NotImplementedError

    async def list_for_media(
        self, tmdb_id: int | None = None, *, media_type: str | None = None
    ) -> list[BlocklistRecord]:
        """List blocklist entries, optionally scoped to one media item + namespace."""
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


# Correlation keys a log record's ``context_json`` may carry (ADR-0012). Shared
# between the repository implementation's WHERE clause and any caller building a
# ``context`` dict, so "what counts as a correlation id" has exactly one
# definition.
LOG_EVENT_CORRELATION_KEYS: tuple[str, ...] = ("request_id", "download_id", "tmdb_id")


@runtime_checkable
class LogEventRepository(Protocol):
    """Persistence for captured log records (ADR-0012's LLM-diagnosable log store).

    Populated by the capture pipeline's background drain task ONLY -- never
    written to from the synchronous logging handler itself (that would block or
    re-enter the event loop). See ``models.LogEvent`` for the full rationale.
    """

    async def create(
        self,
        *,
        level: str,
        logger: str,
        message: str,
        created_at: datetime | None = None,
        context: dict[str, Any] | None = None,
    ) -> LogEventRecord:
        """Insert one log record and return the persisted row.

        ``created_at`` defaults to ``None``, letting the database stamp
        ``now()`` (``LogEvent.created_at``'s ``server_default``) -- pass it
        explicitly to preserve an already-elapsed record's original emission
        time (as :meth:`create_many` always does for the drain task's batch).
        """
        raise NotImplementedError

    async def create_many(self, events: Sequence[LogEventCreate]) -> None:
        """Batch-insert every record in ``events`` in one round trip.

        The drain task's write path: a burst of INFO+ records queued between
        drain ticks costs one INSERT, not one per record. A no-op for an empty
        sequence -- the drain task calls this unconditionally on every tick
        whether or not anything queued, so this must never fail on "nothing to
        insert".
        """
        raise NotImplementedError

    async def list_events(
        self,
        *,
        level: str | None = None,
        since: datetime | None = None,
        logger: str | None = None,
        correlation_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
        oldest_first: bool = False,
    ) -> LogEventPage:
        """Return a page of log records, newest first by default, optionally filtered.

        ``level`` matches the exact stored level name (e.g. ``"ERROR"``) --
        mirrors the plain-string status filters elsewhere in this module, no
        severity ordering is baked in here. ``since`` is an inclusive lower bound
        on ``created_at``. ``logger`` matches the exact stored logger name.
        ``correlation_id`` matches a record whose ``context_json`` carries this
        value (compared as a string) under ANY of :data:`LOG_EVENT_CORRELATION_KEYS`
        -- the same identifiers ``GET /ops/logs/export`` assembles one trail for.
        ``limit``/``offset`` page the (already filtered) result set; ``total`` on
        the returned page is the filtered count, not the whole table's.

        ``oldest_first`` defaults to ``False`` (newest-first, unchanged). When
        ``True``, rows are ordered ``created_at ASC, id ASC`` instead -- the
        ordering ``GET /ops/logs/export`` uses so that a window exceeding the
        export cap keeps the OLDEST matching rows (the root-cause lead-up),
        not the newest (see issue #96).
        """
        raise NotImplementedError

    async def prune_older_than(
        self,
        cutoff: datetime,
        *,
        loggers: Sequence[str] | None = None,
        exclude_loggers: bool = False,
    ) -> int:
        """Delete every record with ``created_at < cutoff``; return the count removed.

        The retention sweep's bounded-growth mechanism (the web-editable
        ``log_retention_days`` setting) -- honesty over silence still applies
        here: this never masks a failure, and the real count lets the sweep log
        what it actually did rather than assuming success.

        ``loggers`` optionally scopes the delete to rows whose ``logger`` column
        matches one of the given names exactly (``None`` = no scoping, every
        stale row). Combined with ``exclude_loggers`` this gives the beta-week
        telemetry emitters (the retention sweep, the decision multi-season
        aggregate, the auto-grab cycle summary -- ``services.log_capture_service.
        _TELEMETRY_LOGGERS``) their OWN, longer retention window without a schema
        change: :func:`~plex_manager.services.log_capture_service.prune_once`
        calls this twice per tick -- once with ``exclude_loggers=True`` (the
        ordinary ``log_retention_days`` cutoff, skipping telemetry rows entirely)
        and once with ``exclude_loggers=False`` (telemetry rows only, on their
        own longer cutoff) -- so a short operator-configured
        ``log_retention_days`` can never prune telemetry data out from under the
        beta-week analysis before it is used. The exclusion delete needs a
        multi-name ``NOT IN`` in ONE statement -- composing it from per-logger
        excludes would wrongly delete each telemetry logger's rows on the passes
        that name a different one.
        """
        raise NotImplementedError
