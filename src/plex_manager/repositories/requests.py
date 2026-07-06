"""``RequestRepository`` implementation over an :class:`AsyncSession`."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import CursorResult, case, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from plex_manager.models import MediaRequest, MediaType, RequestDedupLock, RequestStatus
from plex_manager.ports.repositories import RequestRecord

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["SqlRequestRepository"]

# Statuses at which a request is SETTLED and no longer dedup-blocking — a new
# request for the same media is allowed once the prior one reaches one of these.
# ``completed`` is deliberately NOT here: it is the in-flight "Finalizing" state
# (imported, before Plex confirms availability), so it must keep deduping a second
# request (and a second grab) for the same movie until it reaches available/failed.
# ``evicted`` (ADR-0012) belongs here for the SAME reason as available/failed: the
# disk-pressure sweep already deleted the file, so the old row must never shadow a
# fresh re-request that actually re-grabs the content. This MUST stay in sync with
# ``uq_media_requests_active``'s partial-index predicate in ``models.py`` (also
# ADR-0012), which excludes ``evicted`` from the DB backstop for the identical
# reason — see ``RequestStatus.evicted``'s docstring there.
_SETTLED_REQUEST_STATUSES: frozenset[RequestStatus] = frozenset(
    {
        RequestStatus.available,
        RequestStatus.failed,
        RequestStatus.evicted,
        # ADR-0014: a cancelled request is settled -- it must never dedup-block a
        # fresh request for the same media (a re-request creates a new row), for
        # the SAME reason as available/failed/evicted above.
        RequestStatus.cancelled,
    }
)


def _as_utc(value: datetime | None) -> datetime | None:
    """Coerce a stored timestamp to tz-aware UTC (SQLite returns naive values).

    Mirrors ``repositories.downloads._as_utc``: the app always stores UTC, and the
    auto-grab worker does aware-datetime arithmetic on ``next_search_at``.
    """
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _to_record(row: MediaRequest) -> RequestRecord:
    """Map a ``MediaRequest`` ORM row to its frozen read-model DTO."""
    return RequestRecord(
        id=row.id,
        tmdb_id=row.tmdb_id,
        media_type=row.media_type.value,
        title=row.title,
        status=row.status.value,
        year=row.year,
        is_anime=bool(row.is_anime),
        user_id=row.user_id,
        poster_url=row.poster_url,
        backdrop_url=row.backdrop_url,
        library_path=row.library_path,
        keep_forever=bool(row.keep_forever),
        search_attempts=row.search_attempts,
        next_search_at=_as_utc(row.next_search_at),
    )


class SqlRequestRepository:
    """Persist and read media requests via SQLAlchemy."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, request_id: int) -> RequestRecord | None:
        row = await self._session.get(MediaRequest, request_id)
        return _to_record(row) if row is not None else None

    async def get_fresh(self, request_id: int) -> RequestRecord | None:
        """Like :meth:`get`, but bypasses THIS session's identity-map staleness.

        ``populate_existing=True`` forces a real SELECT that overwrites any
        already-loaded ORM attributes for this row, even when it is already
        present in this session's identity map from an earlier read in the SAME
        transaction. A plain ``session.get()`` would otherwise silently hand
        back the already-cached (stale) instance and never see a commit written
        by a DIFFERENT session in the meantime.

        The eviction TOCTOU re-check (ADR-0012, :func:`~plex_manager.services.
        eviction_service._evict_one`) is the reason this exists: candidate
        assembly runs several awaited Plex/FS calls before a candidate is
        actually deleted, and an operator's ``keep_forever`` pin committed in
        that window (a SEPARATE request's session) must be seen immediately
        before the delete, not silently missed.
        """
        row = await self._session.get(MediaRequest, request_id, populate_existing=True)
        return _to_record(row) if row is not None else None

    async def list_by_status(self, status: str | None = None) -> list[RequestRecord]:
        stmt = select(MediaRequest)
        if status is not None:
            stmt = stmt.where(MediaRequest.status == RequestStatus(status))
        stmt = stmt.order_by(MediaRequest.id)
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_record(row) for row in rows]

    async def list_due_for_search(
        self, statuses: frozenset[str], now: datetime
    ) -> list[RequestRecord]:
        # The ``next_search_at`` backoff gate applies ONLY to a PARKED
        # (``no_acceptable_release``) request -- it earned its escalating backoff and
        # must wait it out. ``pending`` and ``searching`` are EAGER: always due
        # immediately, so a request deliberately re-armed to ``searching`` (a failed
        # download) during a stale 24h backoff window is picked up on the very next
        # tick instead of staying suppressed until that stale timestamp expires
        # (ADR-0013 §3). ``search_attempts``/``next_search_at`` are only ever bumped
        # when a scope is PARKED, so an eager row's leftover timestamp is exactly the
        # staleness this rule ignores.
        parked = MediaRequest.status == RequestStatus.no_acceptable_release
        due = or_(
            ~parked,  # eager (pending/searching): always due
            MediaRequest.next_search_at.is_(None),  # never scheduled: due now
            MediaRequest.next_search_at <= now,  # parked + backoff elapsed
        )
        # Effective due-time for ordering: a parked row sorts by its scheduled
        # backoff; an eager row collapses to NULL so it sorts due-now, never behind a
        # parked row by a stale ``next_search_at`` (case unmatched -> NULL).
        effective_due = case((parked, MediaRequest.next_search_at))
        stmt = (
            select(MediaRequest)
            .where(
                MediaRequest.media_type == MediaType.movie,
                MediaRequest.status.in_([RequestStatus(s) for s in statuses]),
                due,
            )
            # NULL ("due now") first, then oldest-scheduled, then ``id`` as the
            # deterministic tiebreak. ``nulls_first()`` is EXPLICIT because the
            # default NULL ordering differs by backend (SQLite sorts NULLs first,
            # PostgreSQL last) and Postgres is a config swap -- a never-searched
            # request (search-on-approve) must outrank a scheduled-but-overdue one.
            .order_by(effective_due.asc().nulls_first(), MediaRequest.id)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_record(row) for row in rows]

    async def schedule_search(
        self, request_id: int, *, search_attempts: int, next_search_at: datetime | None
    ) -> None:
        row = await self._session.get(MediaRequest, request_id)
        if row is None:
            raise LookupError(f"media request {request_id} does not exist")
        row.search_attempts = search_attempts
        row.next_search_at = next_search_at
        await self._session.flush()

    async def find_active(self, tmdb_id: int, media_type: str) -> RequestRecord | None:
        stmt = (
            select(MediaRequest)
            .where(
                MediaRequest.tmdb_id == tmdb_id,
                MediaRequest.media_type == MediaType(media_type),
                MediaRequest.status.notin_(_SETTLED_REQUEST_STATUSES),
            )
            .order_by(MediaRequest.id)
            .limit(1)
        )
        row = (await self._session.execute(stmt)).scalars().first()
        return _to_record(row) if row is not None else None

    async def find_in_library(self, tmdb_id: int, media_type: str) -> RequestRecord | None:
        stmt = (
            select(MediaRequest)
            .where(
                MediaRequest.tmdb_id == tmdb_id,
                MediaRequest.media_type == MediaType(media_type),
                MediaRequest.status.in_([RequestStatus.available, RequestStatus.completed]),
            )
            .order_by(MediaRequest.id.desc())
            .limit(1)
        )
        row = (await self._session.execute(stmt)).scalars().first()
        return _to_record(row) if row is not None else None

    async def acquire_media_lock(self, tmdb_id: int, media_type: str) -> None:
        """Serialize create-request decisions for one ``(tmdb_id, media_type)``.

        The active unique index protects in-flight statuses, but terminal
        ``available`` rows are intentionally excluded. A short-circuit that records a
        movie already present in Plex must lock this stable per-media row before it
        checks for an existing terminal row or creates a new one.
        """
        media = MediaType(media_type)
        values = {"tmdb_id": tmdb_id, "media_type": media}
        dialect_name = self._session.get_bind().dialect.name
        if dialect_name == "postgresql":
            await self._session.execute(
                pg_insert(RequestDedupLock)
                .values(**values)
                .on_conflict_do_nothing(index_elements=["tmdb_id", "media_type"])
            )
        elif dialect_name == "sqlite":
            await self._session.execute(
                sqlite_insert(RequestDedupLock)
                .values(**values)
                .on_conflict_do_nothing(index_elements=["tmdb_id", "media_type"])
            )
        else:  # pragma: no cover - SQLite and PostgreSQL are the supported backends.
            await self._session.execute(insert(RequestDedupLock).values(**values))
        stmt = (
            select(RequestDedupLock)
            .where(RequestDedupLock.tmdb_id == tmdb_id, RequestDedupLock.media_type == media)
            .with_for_update()
        )
        await self._session.execute(stmt)

    async def display_statuses_by_tmdb_ids(
        self, keys: Sequence[tuple[int, str]]
    ) -> dict[tuple[int, str], str]:
        """Batch the DISPLAY status per ``(tmdb_id, media_type)`` — see the port docstring.

        ONE ``SELECT ... WHERE tmdb_id IN (...)`` over the distinct tmdb ids, then
        the pairs are grouped in Python: a composite ``(tmdb_id, media_type)`` tuple
        IN is deliberately avoided (SQLite/PostgreSQL differ on tuple IN, and the
        backend is a config swap). Rows come back ``id``-ascending, so per key the
        first non-settled row is the lowest-id ACTIVE one (matching ``find_active``)
        and ``group[-1]`` is the newest fallback when every row is settled.
        """
        key_set = set(keys)
        if not key_set:
            return {}
        tmdb_ids = {tmdb_id for tmdb_id, _ in key_set}
        stmt = (
            select(MediaRequest).where(MediaRequest.tmdb_id.in_(tmdb_ids)).order_by(MediaRequest.id)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        grouped: dict[tuple[int, str], list[MediaRequest]] = {}
        for row in rows:
            key = (row.tmdb_id, row.media_type.value)
            if key not in key_set:
                continue  # a tmdb id shared across movie/tv namespaces, other type
            grouped.setdefault(key, []).append(row)
        result: dict[tuple[int, str], str] = {}
        for key, group in grouped.items():
            # Prefer a non-settled (active) row so a stale settled row never shadows
            # a fresh re-request; else the newest by id (mirrors the modal's liveRequest).
            active = next((r for r in group if r.status not in _SETTLED_REQUEST_STATUSES), None)
            chosen = active if active is not None else group[-1]
            result[key] = chosen.status.value
        return result

    async def find_earliest_available(self, tmdb_id: int, media_type: str) -> RequestRecord | None:
        """Return the OLDEST ``available`` request for this media (lowest id), or None.

        Anchors the in-library short-circuit race-collapse: two concurrent requests
        can each pass ``find_in_library`` (neither committed yet) and insert a separate
        ``available`` row, which the active-dedup partial UNIQUE index does NOT reject
        (it excludes terminal ``available``). After committing, ``create_request``
        re-reads the earliest available row and deletes any later duplicate of it.
        Scoped to ``available`` only (not ``completed``) so an in-flight re-acquire is
        never mistaken for a race loser.
        """
        stmt = (
            select(MediaRequest)
            .where(
                MediaRequest.tmdb_id == tmdb_id,
                MediaRequest.media_type == MediaType(media_type),
                MediaRequest.status == RequestStatus.available,
            )
            .order_by(MediaRequest.id)
            .limit(1)
        )
        row = (await self._session.execute(stmt)).scalars().first()
        return _to_record(row) if row is not None else None

    async def delete(self, request_id: int) -> None:
        """Delete a request row (collapse a race-loser duplicate). No-op if absent."""
        row = await self._session.get(MediaRequest, request_id)
        if row is not None:
            await self._session.delete(row)
            await self._session.flush()

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
        row = MediaRequest(
            tmdb_id=tmdb_id,
            media_type=MediaType(media_type),
            title=title,
            status=RequestStatus(status),
            year=year,
            is_anime=is_anime,
            user_id=user_id,
            poster_url=poster_url,
            backdrop_url=backdrop_url,
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return _to_record(row)

    async def set_status(self, request_id: int, status: str) -> None:
        row = await self._session.get(MediaRequest, request_id)
        if row is None:
            raise LookupError(f"media request {request_id} does not exist")
        row.status = RequestStatus(status)
        await self._session.flush()

    async def set_status_if_in(
        self, request_id: int, status: str, allowed_from: frozenset[str]
    ) -> bool:
        """Compare-and-swap: move to ``status`` only if the row's CURRENT persisted
        status is in ``allowed_from``. Returns whether a row was actually updated.

        Mirrors ``SqlDownloadRepository.update_status_if_in`` (see its docstring): a
        single ``UPDATE ... WHERE id = ? AND status IN (...)`` lets the DATABASE --
        not this session's (possibly stale) in-memory view -- decide whether the
        transition still applies. ``False`` means a genuinely concurrent writer
        already moved the row out of ``allowed_from``; the caller must honor that,
        never overwrite it.

        This is the eviction sweep's AUTHORITATIVE double-count guard (ADR-0012,
        C6): ``eviction_service._still_evictable``'s pre-delete re-read closes the
        keep_forever/in-flight races (C7) but is itself only a read-then-act check,
        not a real compare-and-swap -- two genuinely concurrent sweeps (the
        periodic loop racing a manual trigger) can each pass THAT check in their
        own uncommitted transaction before either commits. This CAS is what
        actually stops the SECOND one from also recording an ``evicted`` history
        row / freed-bytes count for the same request: only the winning UPDATE
        (``rowcount == 1``) is allowed to proceed; the loser sees ``rowcount == 0``
        (the row already left ``available`` once the winner committed) and must
        skip rather than double-count.

        ``synchronize_session="fetch"`` keeps any already-loaded identity-map
        instance (e.g. from this session's own ``get_fresh`` re-check moments
        earlier) consistent with the DB result, so anything read afterwards in
        THIS session (an eviction sweep never re-reads the row again, but mirrors
        ``update_status_if_in`` for consistency) sees the honest post-CAS status.
        """
        stmt = (
            update(MediaRequest)
            .where(
                MediaRequest.id == request_id,
                MediaRequest.status.in_([RequestStatus(s) for s in allowed_from]),
            )
            .values(status=RequestStatus(status))
            .execution_options(synchronize_session="fetch")
        )
        # A DML statement yields a ``CursorResult`` carrying ``rowcount`` (the base
        # ``Result`` that ``AsyncSession.execute`` is typed to does not expose it). The
        # cast target is referenced at runtime (not a string) so CodeQL does not read
        # ``CursorResult``/``Any`` as unused imports.
        result = cast(CursorResult[Any], await self._session.execute(stmt))
        return result.rowcount == 1

    async def mark_completed(self, request_id: int) -> None:
        """Set ``completed`` + stamp ``completed_at`` (imported, scan triggered)."""
        row = await self._session.get(MediaRequest, request_id)
        if row is None:
            raise LookupError(f"media request {request_id} does not exist")
        row.status = RequestStatus.completed
        row.completed_at = datetime.now(UTC)
        await self._session.flush()

    async def mark_available(self, request_id: int) -> None:
        """Set ``available`` + stamp ``library_verified_at`` (Plex-confirmed)."""
        row = await self._session.get(MediaRequest, request_id)
        if row is None:
            raise LookupError(f"media request {request_id} does not exist")
        now = datetime.now(UTC)
        row.status = RequestStatus.available
        row.library_verified_at = now
        if row.completed_at is None:
            row.completed_at = now
        await self._session.flush()

    async def stamp_completed_at_if_unset(self, request_id: int) -> None:
        """Stamp ``completed_at`` = now, but ONLY if it is currently unset.

        Records a request's FIRST completion and never moves once set. A MOVIE
        request stamps ``completed_at`` directly in ``mark_completed`` /
        ``mark_available``; a TV ``MediaRequest.status``, by contrast, is a pure
        COMPUTED fold of its per-season rows and never itself goes through those
        methods, so without this its ``completed_at`` would stay ``None`` forever
        -- every TV time-to-watch interval would read "unknown". The TV
        parent-rollup path (``season_request_service._recompute_parent``) calls
        this the first time a tracked season reaches ``completed``/``available``,
        the per-season analogue of the movie stamp point. Idempotent: a later
        season completing re-enters here but the ``None`` guard leaves the first
        stamp intact, so ``completed_at`` honestly records the show's FIRST
        completion, never the latest.
        The stamp is a single conditional UPDATE (``WHERE completed_at IS
        NULL``), not read-check-write: two seasons of the same show importing
        concurrently each hold only their own per-download lock, so an
        identity-map read here can be stale -- the guarded statement lets
        exactly one writer win and the first stamp stand.
        """
        result = await self._session.execute(
            update(MediaRequest)
            .where(MediaRequest.id == request_id, MediaRequest.completed_at.is_(None))
            .values(completed_at=datetime.now(UTC))
            .execution_options(synchronize_session="fetch")
        )
        if isinstance(result, CursorResult) and result.rowcount == 0:
            row = await self._session.get(MediaRequest, request_id)
            if row is None:
                raise LookupError(f"media request {request_id} does not exist")
            # Already stamped (possibly by a concurrent sibling-season import):
            # the FIRST stamp stands, nothing to do.

    async def set_library_path(self, request_id: int, library_path: str) -> None:
        """Store the final placed path this request's import wrote into (ADR-0012)."""
        row = await self._session.get(MediaRequest, request_id)
        if row is None:
            raise LookupError(f"media request {request_id} does not exist")
        row.library_path = library_path
        await self._session.flush()

    async def reset_for_research(self, request_id: int, *, clear_library_path: bool = True) -> None:
        """Re-arm a reported movie for a fresh search (ADR-0014's report-issue verb).

        Sets ``status`` back to the non-terminal ``searching`` and clears the
        honest-availability anchors (``completed_at`` / ``library_verified_at``) that
        asserted the title was in the library. The subsequent inline re-grab drives the
        row on to ``downloading``; if nothing acceptable is found it lands on the honest
        ``no_acceptable_release`` dead-end -- either way the row never lingers claiming
        an in-library file it no longer has.

        ``clear_library_path`` (default ``True``) also nulls the ``library_path`` purge
        breadcrumb -- correct when the file was actually deleted. The report-issue verb
        passes ``False`` when the purge failed/was refused (the file may still be on
        disk): the breadcrumb is then PRESERVED as the only handle a later retry /
        eviction has to reclaim the orphan (honesty over silence -- never strand a bad
        file with no way to purge it).
        """
        row = await self._session.get(MediaRequest, request_id)
        if row is None:
            raise LookupError(f"media request {request_id} does not exist")
        row.status = RequestStatus.searching
        if clear_library_path:
            row.library_path = None
        row.completed_at = None
        row.library_verified_at = None
        # A report-issue is the operator saying "look again NOW": the auto-grab
        # worker's accrued backoff (ADR-0013) belongs to the culprit's history,
        # not the fresh search, so a later re-park starts the ladder over.
        row.search_attempts = 0
        row.next_search_at = None
        await self._session.flush()

    async def clear_completed_at(self, request_id: int) -> None:
        """Null the parent's ``completed_at`` outside any status transition (ADR-0014).

        Added for the TV report-issue path (``season_request_service.
        reset_for_research``, #76): a TV parent's ``completed_at`` is not stamped
        directly (a TV ``MediaRequest`` never goes through ``mark_completed``/
        ``mark_available`` -- see ``stamp_completed_at_if_unset``'s docstring), so
        report-issue cannot reuse this class's own :meth:`reset_for_research`
        (that method is the single-row MOVIE verb: it also flips ``status`` to
        ``searching`` and clears ``library_verified_at``/``library_path``/backoff
        fields that a TV parent's status is a pure ROLLUP of its seasons for, and
        that live per-season on ``SeasonRequest`` instead). The caller decides
        WHEN to call this (only once no OTHER tracked season is still
        ``completed``/``available`` -- see ``season_request_service.
        reset_for_research``'s docstring for why), so this method itself is
        unconditional, mirroring :meth:`clear_library_path`'s bare, no-transition
        write.
        """
        row = await self._session.get(MediaRequest, request_id)
        if row is None:
            raise LookupError(f"media request {request_id} does not exist")
        row.completed_at = None
        await self._session.flush()

    async def clear_library_path(self, request_id: int) -> None:
        """Drop the eviction/purge breadcrumb without any status transition (ADR-0014).

        The movie-level mirror of ``SqlSeasonRequestRepository.clear_library_path``:
        report-issue re-arms the request (claiming the active slot) BEFORE it knows
        whether the purge will succeed, so it keeps the ``library_path`` breadcrumb
        through the claim and clears it HERE only once the file was actually removed --
        never as part of the re-arm. Clearing ``library_path`` is not a status change,
        so this never re-touches ``uq_media_requests_active`` (unlike
        :meth:`reset_for_research`, whose status flush is the slot claim). No-op-safe if
        the row vanished is not needed here (the caller just re-armed it), but a missing
        row is still an honest error rather than a silent skip.
        """
        row = await self._session.get(MediaRequest, request_id)
        if row is None:
            raise LookupError(f"media request {request_id} does not exist")
        row.library_path = None
        await self._session.flush()

    async def set_keep_forever(self, request_id: int, keep_forever: bool) -> None:
        """Set the operator's "keep forever" pin (ADR-0012)."""
        row = await self._session.get(MediaRequest, request_id)
        if row is None:
            raise LookupError(f"media request {request_id} does not exist")
        row.keep_forever = keep_forever
        await self._session.flush()

    async def set_keep_forever_for_title(
        self, tmdb_id: int, media_type: str, keep_forever: bool
    ) -> None:
        """See ``RequestRepository.set_keep_forever_for_title``'s docstring: pins
        or unpins EVERY row sharing ``(tmdb_id, media_type)``, not just one.

        A single ``UPDATE ... WHERE tmdb_id = ? AND media_type = ?`` -- no
        status filter, deliberately every row (active AND settled) sharing the
        key, since a settled row's own season rows are exactly what an older
        request's ``keep_forever`` protects (``eviction_service.
        _season_candidates`` reads the pin off each season's OWN parent, which
        may be a different, settled row than the one the operator toggled from
        the UI). ``synchronize_session="fetch"`` keeps any already-loaded
        identity-map instance for this title (e.g. the caller's own ``get``
        moments earlier, in the SAME session/transaction) in sync with the
        DB, mirroring ``set_status_if_in``'s same discipline.
        """
        stmt = (
            update(MediaRequest)
            .where(
                MediaRequest.tmdb_id == tmdb_id,
                MediaRequest.media_type == MediaType(media_type),
            )
            .values(keep_forever=keep_forever)
            .execution_options(synchronize_session="fetch")
        )
        await self._session.execute(stmt)
        await self._session.flush()
