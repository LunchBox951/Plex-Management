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

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

__all__ = [
    "LOG_EVENT_CORRELATION_KEYS",
    "BlocklistRecord",
    "BlocklistRepository",
    "DownloadRecord",
    "DownloadRepository",
    "DownloadScopeRecord",
    "LogEventCreate",
    "LogEventPage",
    "LogEventRecord",
    "LogEventRepository",
    "QueueRecord",
    "RequestRecord",
    "RequestRepository",
    "SeasonEpisodeStateRecord",
    "SeasonEpisodeStateRepository",
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
    # WHEN this request's import finalized (``mark_completed``) / became
    # watchable (``mark_available``) -- i.e. the instant "Finalizing" began.
    # ``None`` for a request that has never imported. The bounded-Finalizing
    # warning (issue #158, ``import_service.run_availability_cycle``) reads this
    # as the anchor for "elapsed time since completed" -- persisted and exact
    # (survives a restart), unlike the in-memory fallback anchor a TV
    # ``SeasonRequestRecord`` must use (it carries no per-season mirror of this
    # column; see ``SqlRequestRepository.heal_completed_at``'s docstring on why
    # one is deliberately deferred).
    completed_at: datetime | None = None
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
    # TV-only request intent used by multi-season pack planning. ``None`` for
    # movies and legacy rows.
    tv_request_mode: str | None = None
    requested_seasons: tuple[int, ...] | None = None
    requested_episodes: dict[int, tuple[int, ...]] | None = None


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
    # Consecutive resumed-import probe-outage retries (``downloads.retry_count``,
    # issue #180) -- see ``services.import_service._refresh_resumed_import_after_probe_outage``
    # and ``_PROBE_OUTAGE_MAX_RETRIES``. Reset to 0 on terminal-row reuse.
    retry_count: int = 0
    first_seen_at: datetime | None = None
    # When this download was grabbed (``downloads.added_at``, server-defaulted at
    # row creation, and explicitly RE-STAMPED to now by
    # ``grab_service._reuse_terminal_row`` when a terminal row is resurrected for
    # a fresh grab under the same torrent hash) — distinct from ``first_seen_at``,
    # which is ONLY the missing-grace anchor (stamped when a torrent first
    # vanishes from the client). The stall self-heal (issue #165) anchors both its
    # stall shapes on this: it correctly represents "since we started waiting" for
    # a fresh grab, while ``first_seen_at`` is usually unset for a healthy,
    # present torrent. Re-stamping on reuse (hardening finding) is what keeps that
    # true across a resurrection -- without it a reused row would carry the STALE
    # original grab time, letting the very next reconcile tick immediately
    # misjudge the brand-new grab as stalled.
    added_at: datetime | None = None
    # The honest download-phase stall deadline (``downloads.timeout_at``):
    # ``added_at`` + the phase window for the live raw_state, recomputed each
    # reconcile cycle. Observability only — ``detect_stalls`` stays anchored on
    # ``added_at`` and never reads this. Exposed on the read-model so the queue
    # reconcile can skip a redundant write when the deadline has not moved.
    timeout_at: datetime | None = None
    download_path: str | None = None
    # The release ("download") title the grab decision picked -- the same value
    # already written to ``DownloadHistory.source_title`` at grab time and used
    # for blocklisting (``blocklist_service.source_title_for``). Denormalized
    # onto the row itself (issue #134) so the queue can show it without a join
    # into the append-only history log. ``None`` for a pre-migration row with no
    # backfillable history.
    release_title: str | None = None
    scopes: tuple[DownloadScopeRecord, ...] = ()


class DownloadScopeRecord(BaseModel):
    """One logical TV scope attached to a physical download."""

    model_config = ConfigDict(frozen=True)

    id: int
    download_id: int
    media_request_id: int | None = None
    season_request_id: int | None = None
    season: int | None = None
    episodes: list[int] | None = None
    status: str = "active"
    completed_at: datetime | None = None


class QueueRecord(DownloadRecord):
    """``DownloadRecord`` enriched with the two ``MediaRequest``-only fields the
    live queue view needs to render a human-legible row (issue #134): the media
    ``title`` and its ``poster_url``. Sourced by
    ``SqlDownloadRepository.list_active_for_queue``'s LEFT OUTER JOIN against
    ``MediaRequest`` -- OUTER because ``media_request_id`` is nullable (SET NULL
    when the owning request is deleted), so an orphaned download still produces a
    row here, just with both fields ``None`` (honesty over silence: the row
    always renders). Deliberately NOT used by ``list_active`` / the reconciler --
    that domain-facing read stays on the plain ``DownloadRecord`` contract.
    """

    title: str | None = None
    poster_url: str | None = None


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
    installed_quality_id: int | None = None
    installed_profile_index: int | None = None
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


class SeasonEpisodeStateRecord(BaseModel):
    """One aired episode's collection state for a whole-season fallback (ADR-0020).

    One row per aired episode of a :class:`SeasonRequestRecord`, tracking
    ``pending -> grabbed -> imported`` progress for the episode-level fallback
    (issue #178). ``air_date`` is ``None`` only for a row seeded before this
    breadcrumb existed (pre-fallback back-compat), never a live "unknown" state --
    a genuinely-unaired episode never gets a row in the first place.
    """

    model_config = ConfigDict(frozen=True)

    id: int
    season_request_id: int
    episode_number: int
    status: str
    air_date: date | None = None
    grabbed_download_id: int | None = None


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
    # Immutable tuple (issue #106): a frozen model blocks reassigning
    # ``page.results`` but not appending to a plain list in place. A ``list``
    # input is coerced by pydantic.
    results: tuple[LogEventRecord, ...]


@runtime_checkable
class RequestRepository(Protocol):
    """Persistence for media requests."""

    async def get(self, request_id: int) -> RequestRecord | None:
        """Return the request by id, or ``None``.

        Raises ``NotImplementedError`` by default (issue #204): an implicit
        ``None`` default is indistinguishable from an honest "not found"
        answer, silently hiding a forgotten override -- must fail loudly at
        call time instead, mirroring #80/#81.
        """
        raise NotImplementedError

    async def list_by_status(self, status: str | None = None) -> list[RequestRecord]:
        """List requests, optionally filtered by ``status``."""
        raise NotImplementedError

    async def list_personalization_history(self, user_id: int) -> list[RequestRecord]:
        """List one user's non-cancelled, display-deduplicated request history.

        The user predicate is applied by the persistence query. Ownerless and
        other users' rows are never candidates, including for an administrator.
        """
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
        """Return an existing non-terminal request for this media, for dedup.

        Raises ``NotImplementedError`` by default (issue #204): an implicit
        ``None`` default is indistinguishable from an honest "no active
        request" answer, silently hiding a forgotten override -- a caller
        relying on this for the create-request dedup guard must fail loudly at
        call time instead, mirroring #80/#81.
        """
        raise NotImplementedError

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
        and collapses their re-request onto a less precise foreign match. ``None``
        (the default; admins and API-key automation) returns
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
        tv_request_mode: str | None = None,
        requested_seasons: Sequence[int] | None = None,
        requested_episodes: Mapping[int, Sequence[int]] | None = None,
    ) -> RequestRecord:
        """Insert a new request and return the persisted record.

        ``eviction_regrab`` (issue #156) stamps the provenance marker: ``True``
        only when THIS insert is the eviction-guard fall-through (``request_
        service.create_request``'s ``latest_request_evicted`` branch), never for
        an ordinary or forced (#148) request.
        """
        raise NotImplementedError

    async def set_tv_request_intent(
        self,
        request_id: int,
        *,
        mode: str,
        requested_seasons: Sequence[int] | None,
        requested_episodes: Mapping[int, Sequence[int]] | None = None,
    ) -> None:
        """Persist TV request intent for multi-season pack planning."""
        raise NotImplementedError

    async def set_status(self, request_id: int, status: str) -> None:
        """Update a request's status.

        Raises ``NotImplementedError`` by default (issue #204): a silent no-op
        default would let a caller believe a status transition committed when
        nothing happened -- must fail loudly at call time instead, mirroring
        #80/#81.
        """
        raise NotImplementedError

    async def set_status_if_in(
        self,
        request_id: int,
        status: str,
        allowed_from: frozenset[str],
        *,
        require_unpinned: bool = False,
        require_not_watchlisted: bool = False,
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
        """Return the download for ``torrent_hash``, or ``None``.

        Raises ``NotImplementedError`` by default (issue #204): an implicit
        ``None`` default is indistinguishable from an honest "not tracked"
        answer, silently hiding a forgotten override -- must fail loudly at
        call time instead, mirroring #80/#81.
        """
        raise NotImplementedError

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
        release_title: str | None = None,
    ) -> DownloadRecord:
        """Insert a new download and return the persisted record.

        ``episodes`` (TV only) persists to ``Download.episodes_json``: ``None``
        means import every valid video file found for the season; an explicit list
        scopes the import to those episode numbers only. ``release_title`` is the
        grab decision's release name (issue #134) -- the same value the caller
        already writes to ``DownloadHistory.source_title``.
        """
        raise NotImplementedError

    async def ensure_scope(
        self,
        download_id: int,
        *,
        media_request_id: int | None,
        season: int | None,
        episodes: list[int] | None = None,
    ) -> DownloadScopeRecord:
        """Attach a logical TV scope to an existing physical download."""
        raise NotImplementedError

    async def list_scopes(self, download_id: int) -> list[DownloadScopeRecord]:
        """List logical scopes attached to ``download_id``."""
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

        Raises ``NotImplementedError`` by default (issue #204): a silent no-op
        default would let the reconciler believe a status/progress write
        committed when nothing happened -- must fail loudly at call time
        instead, mirroring #80/#81.
        """
        raise NotImplementedError


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

    async def list_for_airing_refresh(
        self, statuses: frozenset[str], limit: int, *, checked_before: datetime | None = None
    ) -> list[SeasonRequestRecord]:
        """List up to ``limit`` seasons in ``statuses`` due for an airing-target
        refresh (ADR-0020 §6, ``season_episode_service.reconcile_airing``), oldest
        ``airing_refresh_checked_at`` first (``NULL`` -- never checked -- sorts
        first), tie-broken by ``id``.

        This is a ROTATING window, not a stable top-N: :meth:`mark_airing_refresh_
        checked` advances a row to the back of the queue, so a bounded per-cycle
        ``limit`` still eventually revisits every row instead of permanently
        starving whichever ones do not fit in the first ``limit``-sized slice.

        ``checked_before`` (when set) additionally suppresses any row already
        stamped AT OR AFTER it, so a row re-checked this recently is not re-selected
        -- a never-checked (``NULL``) row is always eligible. This is the per-row
        due cutoff ``wake_waiting_for_air_date`` uses to keep still-future seasons
        on an hours-scale re-check cadence instead of one TMDB lookup per cycle.
        """
        raise NotImplementedError

    async def mark_airing_refresh_checked(
        self, season_request_id: int, checked_at: datetime
    ) -> None:
        """Stamp the airing-refresh rotation cursor (see :meth:`list_for_airing_
        refresh`) after ``reconcile_airing`` has actually looked at this season this
        cycle -- rearmed or not, even after a TMDB error -- so the row moves to the
        back of the rotation instead of being re-selected every cycle. A full
        timestamp (not a date) so same-day cycles keep rotating (P2, issue #178
        review round 2).
        """
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
        require_not_watchlisted: bool = False,
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

    async def set_installed_quality(
        self, season_request_id: int, *, quality_id: int, profile_index: int | None
    ) -> None:
        """Store the imported quality breadcrumb for this season."""
        raise NotImplementedError


@runtime_checkable
class SeasonEpisodeStateRepository(Protocol):
    """Persistence for per-episode fallback-collection state (ADR-0020, #178).

    Bridges the aired-episode target set (from ``MetadataPort.season_episodes``)
    to what has actually been grabbed/imported, so the episode-level fallback can
    compute a season's "missing" set without re-deriving it from free-text
    history.
    """

    async def list_for_season(self, season_request_id: int) -> list[SeasonEpisodeStateRecord]:
        """List every tracked episode row for ``season_request_id``."""
        raise NotImplementedError

    async def upsert_target(self, season_request_id: int, aired: Mapping[int, date | None]) -> None:
        """Idempotently seed/refresh the aired-episode target.

        For each episode in ``aired``: inserts a ``pending`` row if absent, and
        refreshes ``air_date`` on an existing row -- but NEVER downgrades an
        existing ``grabbed``/``imported`` row back to ``pending``. Lets a newly
        aired episode join the target (airing growth, ADR-0020) without
        disturbing progress already made on episodes already tracked.

        Also RETIRES (deletes) ``pending`` rows absent from ``aired`` -- TMDB
        delaying/removing a previously-aired episode must not leave a stale
        pending row that keeps the season searching forever. ``grabbed``/
        ``imported`` rows are never retired.
        """
        raise NotImplementedError

    async def mark_grabbed(
        self, season_request_id: int, episode_numbers: Sequence[int], download_id: int
    ) -> None:
        """Mark ``episode_numbers`` ``grabbed`` (+ their ``grabbed_download_id``).

        Creates a row as ``grabbed`` for an episode with no existing target row
        (defensive; observability/crash-visibility only -- NOT read by
        ``compute_missing``, which treats the live active download as
        authoritative).
        """
        raise NotImplementedError

    async def mark_imported(
        self, season_request_id: int, episode_numbers: Sequence[int], download_id: int
    ) -> None:
        """Upsert ``episode_numbers`` to ``imported`` (+ ``grabbed_download_id``).

        Creates rows for episodes not previously in the target (e.g. a season
        pack placed episodes beyond the seeded aired set) -- they still count as
        imported.

        On an insert race (a concurrent target refresh inserts the same episode
        first) the winning row is re-read and promoted to ``imported`` so an
        import can never leave a just-placed episode ``pending`` for the airing
        refresh to re-arm.
        """
        raise NotImplementedError

    async def adopt_baseline(
        self, season_request_id: int, *, episodes: Sequence[int] | None = None
    ) -> None:
        """Promote PENDING rows for this season to ``imported`` with no backing
        download -- baseline adoption for an already-watchable season whose
        content predates per-episode import tracking (ADR-0020 §6). ``episodes``
        restricts adoption to those episode numbers (the partial-baseline case,
        round 3); ``None`` adopts every pending row. Never touches ``grabbed``
        rows -- they are evidence of our own attempt to fetch the episode, i.e.
        evidence it was NOT already owned.
        """
        raise NotImplementedError

    async def stale_grabbed_episodes(self, season_request_id: int) -> frozenset[int]:
        """Episode numbers whose ``grabbed`` row's backing download is gone or
        terminally dead (``failed``/``no_acceptable_release``) -- stale grab
        breadcrumbs import completeness excludes from its completion target so a
        failed grab of a later-retracted episode cannot wedge the season
        incomplete forever (P2, issue #178 review round 3).
        """
        raise NotImplementedError

    async def counts_for_seasons(
        self, season_request_ids: Sequence[int]
    ) -> dict[int, tuple[int, int]]:
        """Batch ``{season_request_id: (imported_count, target_count)}``.

        One grouped query for however many seasons are asked about -- the
        requests-list "N/M episodes" badge's batched read, avoiding an N+1 over
        the season rows on a page.
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
        """Remove a blocklist entry (operator un-blocklist).

        Raises ``NotImplementedError`` by default (issue #204): a silent no-op
        default would let the un-blocklist button report success while the
        entry still blocks re-search -- must fail loudly at call time instead,
        mirroring #80/#81.
        """
        raise NotImplementedError


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

    async def prune_excess(self, max_rows: int) -> int:
        """Delete the OLDEST rows beyond the newest ``max_rows``; return the count removed.

        The ROW-COUNT companion to :meth:`prune_older_than`'s AGE cutoff
        (issue #152): a chatty install running a generous ``log_retention_days``
        can otherwise grow ``log_events`` unboundedly, since age-based pruning
        alone never trips until a row is actually stale. Ordered by
        ``created_at``, ``id`` (the same tie-break :meth:`list_events` uses for
        rows a single batch-insert stamped with an identical ``created_at``) so
        "oldest" is well-defined even under a burst insert; the newest
        ``max_rows`` survive regardless of level/logger -- this is a total cap,
        not a per-logger one (the telemetry carve-out is an AGE exception only).

        A no-op (0 rows removed) when the table is already at or under the cap
        -- the common, cheap-to-check-for tick. ``max_rows`` is caller-supplied
        already-resolved policy (:func:`~plex_manager.web.deps.
        get_log_max_rows`); a negative value is treated as ``0`` (keep nothing)
        rather than raising, so a corrupt/adversarial cap value degrades to the
        safe (over-pruning, never under-bounding) side.
        """
        raise NotImplementedError
