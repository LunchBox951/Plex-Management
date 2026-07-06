"""``SeasonRequestRepository`` implementation over an :class:`AsyncSession`.

Mirrors :mod:`repositories.requests` (``SqlRequestRepository``): frozen read-model
DTOs, ``flush`` (never ``commit``) so the repository composes inside a
caller-owned unit of work. ``tmdb_id`` on :class:`SeasonRequestRecord` is
denormalized from the parent ``MediaRequest`` via a join -- ``season_requests``
itself carries no ``tmdb_id`` column.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import CursorResult, case, or_, select, update
from sqlalchemy.exc import IntegrityError

from plex_manager.models import MediaRequest, MediaType, RequestStatus, SeasonRequest
from plex_manager.ports.repositories import SeasonRequestRecord

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["SqlSeasonRequestRepository"]


def _as_utc(value: datetime | None) -> datetime | None:
    """Coerce a stored timestamp to tz-aware UTC (SQLite returns naive values).

    Mirrors ``repositories.downloads._as_utc``; the auto-grab worker does
    aware-datetime arithmetic on ``next_search_at``.
    """
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _to_record(row: SeasonRequest, tmdb_id: int) -> SeasonRequestRecord:
    """Map a ``SeasonRequest`` ORM row (+ its parent's ``tmdb_id``) to the DTO."""
    return SeasonRequestRecord(
        id=row.id,
        media_request_id=row.media_request_id,
        season_number=row.season_number,
        status=row.status.value,
        tmdb_id=tmdb_id,
        library_path=row.library_path,
        search_attempts=row.search_attempts,
        next_search_at=_as_utc(row.next_search_at),
    )


class SqlSeasonRequestRepository:
    """Persist and read per-season TV requests via SQLAlchemy."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _find(self, media_request_id: int, season_number: int) -> SeasonRequest | None:
        """Return the raw ORM row for ``(media_request_id, season_number)``, or ``None``."""
        stmt = select(SeasonRequest).where(
            SeasonRequest.media_request_id == media_request_id,
            SeasonRequest.season_number == season_number,
        )
        return (await self._session.execute(stmt)).scalars().first()

    async def _tmdb_id_for(self, media_request_id: int) -> int:
        """Return the parent show's ``tmdb_id`` (the FK guarantees it exists)."""
        tmdb_id = await self._session.scalar(
            select(MediaRequest.tmdb_id).where(MediaRequest.id == media_request_id)
        )
        if tmdb_id is None:  # pragma: no cover - FK guarantees the parent row exists
            raise LookupError(f"media request {media_request_id} does not exist")
        return tmdb_id

    async def get(self, season_request_id: int) -> SeasonRequestRecord | None:
        row = await self._session.get(SeasonRequest, season_request_id)
        if row is None:
            return None
        return _to_record(row, await self._tmdb_id_for(row.media_request_id))

    async def get_fresh(self, season_request_id: int) -> SeasonRequestRecord | None:
        """Like :meth:`get`, but bypasses THIS session's identity-map staleness.

        Same ``populate_existing=True`` TOCTOU fix as ``SqlRequestRepository.
        get_fresh`` (see its docstring), at season granularity — the eviction
        re-check needs the season's CURRENT status (a concurrent sweep may have
        already evicted it, or an import may have re-armed it) immediately
        before deleting its file.
        """
        row = await self._session.get(SeasonRequest, season_request_id, populate_existing=True)
        if row is None:
            return None
        return _to_record(row, await self._tmdb_id_for(row.media_request_id))

    async def list_for_request(self, media_request_id: int) -> list[SeasonRequestRecord]:
        stmt = (
            select(SeasonRequest)
            .where(SeasonRequest.media_request_id == media_request_id)
            .order_by(SeasonRequest.season_number)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        if not rows:
            return []
        tmdb_id = await self._tmdb_id_for(media_request_id)
        return [_to_record(row, tmdb_id) for row in rows]

    async def list_for_requests(
        self, media_request_ids: Sequence[int]
    ) -> dict[int, list[SeasonRequestRecord]]:
        if not media_request_ids:
            return {}
        # JOIN straight to the parent's tmdb_id in ONE query -- avoids both an N+1
        # per request row AND the per-distinct-show follow-up lookup
        # ``list_by_status`` needs (there ``tmdb_id`` isn't otherwise in hand).
        stmt = (
            select(SeasonRequest, MediaRequest.tmdb_id)
            .join(MediaRequest, MediaRequest.id == SeasonRequest.media_request_id)
            .where(SeasonRequest.media_request_id.in_(media_request_ids))
            .order_by(SeasonRequest.media_request_id, SeasonRequest.season_number)
        )
        grouped: dict[int, list[SeasonRequestRecord]] = {}
        for row, tmdb_id in (await self._session.execute(stmt)).all():
            grouped.setdefault(row.media_request_id, []).append(_to_record(row, tmdb_id))
        return grouped

    async def evicted_seasons(self, tmdb_id: int) -> frozenset[int]:
        """Season numbers whose NEWEST tracked row (across every ``tv``
        ``MediaRequest`` for this ``tmdb_id``) is ``evicted`` (ADR-0012).

        ``season_request_service.ensure_seasons`` subtracts these from Plex's fresh
        ``present_seasons`` snapshot, so a season the disk-pressure sweep is
        mid-deleting -- or has just deleted, before the post-delete Plex refresh
        lands -- is never created or re-armed straight to ``available`` off a STALE
        'present' reading (the season-level twin of
        ``SqlRequestRepository.latest_request_evicted``; see its docstring for the
        eviction delete window this closes). It re-grabs (``pending``) instead.

        Keyed on the NEWEST NON-``cancelled`` row PER SEASON (the highest ``id``
        among every ``MediaRequest`` sharing this ``tmdb_id``, since a title
        accrues several rows over its lifetime -- see
        ``uq_media_requests_active``): a season legitimately re-downloaded after
        an earlier eviction (its newest row now ``available``) is NOT falsely
        suppressed, only one whose most recent history really is an eviction.
        ``cancelled`` rows are IGNORED outright, exactly like
        ``SqlRequestRepository.latest_request_evicted`` (see there): a
        cancellation says nothing about on-disk truth, so an in-window re-grab
        the user then cancelled must not reset this guard for the NEXT
        re-request while the sweep is still deleting the season's files. Movies
        never reach here -- only ``tv`` parents own ``season_requests`` rows --
        but the ``media_type`` filter is kept explicit against a ``tmdb_id``
        shared across the movie/tv namespaces.
        """
        stmt = (
            select(SeasonRequest.season_number, SeasonRequest.status)
            .join(MediaRequest, MediaRequest.id == SeasonRequest.media_request_id)
            .where(
                MediaRequest.tmdb_id == tmdb_id,
                MediaRequest.media_type == MediaType.tv,
                SeasonRequest.status != RequestStatus.cancelled,
            )
            .order_by(SeasonRequest.season_number, SeasonRequest.id)
        )
        # Ascending ``id`` per season means the LAST row seen for each season is its
        # newest (cancelled rows already filtered out above); keep only the newest
        # status, then filter to the evicted ones.
        newest: dict[int, RequestStatus] = {}
        for season_number, status in (await self._session.execute(stmt)).all():
            newest[season_number] = status
        return frozenset(s for s, status in newest.items() if status == RequestStatus.evicted)

    async def list_sibling_seasons(
        self, tmdb_id: int, season_number: int, statuses: frozenset[str], exclude_id: int
    ) -> list[SeasonRequestRecord]:
        """Every OTHER request's row for the SAME ``(tmdb_id, season_number)``
        whose status is in ``statuses``, oldest first.

        The season-granularity mirror of ``SqlRequestRepository.list_for_media``
        (see there), for the eviction restore's re-grab reconciliation
        (ADR-0012 #67): a title accrues several ``MediaRequest`` rows over its
        lifetime, so an in-window re-request for a WHOLLY evicted show tracks
        this same season under a NEWER parent -- those duplicates are what the
        restore cancels when the file never actually left. ``exclude_id`` is the
        restored row itself. A plain read, not a CAS: the caller re-compares via
        :meth:`set_status_if_in`.
        """
        stmt = (
            select(SeasonRequest, MediaRequest.tmdb_id)
            .join(MediaRequest, MediaRequest.id == SeasonRequest.media_request_id)
            .where(
                MediaRequest.tmdb_id == tmdb_id,
                MediaRequest.media_type == MediaType.tv,
                SeasonRequest.season_number == season_number,
                SeasonRequest.id != exclude_id,
                SeasonRequest.status.in_([RequestStatus(s) for s in statuses]),
            )
            .order_by(SeasonRequest.id)
        )
        return [
            _to_record(row, parent_tmdb_id)
            for row, parent_tmdb_id in (await self._session.execute(stmt)).all()
        ]

    async def clear_library_path_if_set(
        self, season_request_id: int, *, expected_path: str | None = None
    ) -> bool:
        """Null the season's eviction breadcrumb ONLY if currently set (and, with
        ``expected_path``, only if it still holds EXACTLY that value); return
        whether this call actually cleared it.

        The season-granularity mirror of ``SqlRequestRepository.
        clear_library_path_if_set`` (see there, including the ``expected_path``
        value predicate): the eviction finalize's single-winner gate -- only the
        finalize/recovery pass that actually cleared the breadcrumb writes the
        eviction history row, and a clear predicated on the OBSERVED stale path
        can never wipe the fresh breadcrumb a replacement import stamped onto
        the row mid-recovery.
        """
        predicates = [
            SeasonRequest.id == season_request_id,
            SeasonRequest.library_path.is_not(None),
        ]
        if expected_path is not None:
            predicates.append(SeasonRequest.library_path == expected_path)
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(SeasonRequest)
                .where(*predicates)
                .values(library_path=None)
                .execution_options(synchronize_session="fetch")
            ),
        )
        return result.rowcount == 1

    async def other_row_claims_path(
        self, library_path: str, *, exclude_season_request_id: int | None = None
    ) -> bool:
        """Whether any (other) season row currently claims ``library_path``.

        The season-granularity mirror of ``SqlRequestRepository.
        other_row_claims_path`` (see there): the eviction recovery pass's
        finalized-vs-interrupted discriminator. ``evicted``/``cancelled`` rows do
        not count as claims; ``exclude_season_request_id`` is the row being
        recovered itself.
        """
        predicates = [
            SeasonRequest.library_path == library_path,
            SeasonRequest.status.notin_([RequestStatus.evicted, RequestStatus.cancelled]),
        ]
        if exclude_season_request_id is not None:
            predicates.append(SeasonRequest.id != exclude_season_request_id)
        stmt = select(SeasonRequest.id).where(*predicates).limit(1)
        return (await self._session.execute(stmt)).scalars().first() is not None

    async def list_by_status(self, status: str | None = None) -> list[SeasonRequestRecord]:
        stmt = select(SeasonRequest)
        if status is not None:
            stmt = stmt.where(SeasonRequest.status == RequestStatus(status))
        stmt = stmt.order_by(SeasonRequest.id)
        rows = (await self._session.execute(stmt)).scalars().all()
        # One tmdb_id lookup per distinct show, not one per row -- a show with
        # several tracked seasons only needs its tmdb_id resolved once.
        tmdb_ids: dict[int, int] = {}
        records: list[SeasonRequestRecord] = []
        for row in rows:
            if row.media_request_id not in tmdb_ids:
                tmdb_ids[row.media_request_id] = await self._tmdb_id_for(row.media_request_id)
            records.append(_to_record(row, tmdb_ids[row.media_request_id]))
        return records

    async def list_due_for_search(
        self, statuses: frozenset[str], now: datetime
    ) -> list[SeasonRequestRecord]:
        # JOIN to the parent's tmdb_id in ONE query (no per-row follow-up), same as
        # ``list_for_requests``. The ``next_search_at`` backoff gate applies ONLY to a
        # PARKED (``no_acceptable_release``) season; ``pending``/``searching`` are
        # EAGER (always due immediately) so a season re-armed to ``searching`` (a
        # failed download) during a stale backoff window is picked up on the very next
        # tick, never suppressed until that stale timestamp expires -- the season-level
        # mirror of ``SqlRequestRepository.list_due_for_search`` (see its docstring,
        # ADR-0013 §3). NULL ("due now") first via an EXPLICIT ``nulls_first()`` --
        # the default NULL ordering is backend-dependent (Postgres is a swap).
        parked = SeasonRequest.status == RequestStatus.no_acceptable_release
        due = or_(
            ~parked,  # eager (pending/searching): always due
            SeasonRequest.next_search_at.is_(None),  # never scheduled: due now
            SeasonRequest.next_search_at <= now,  # parked + backoff elapsed
        )
        # A parked season sorts by its scheduled backoff; an eager season collapses
        # to NULL so it sorts due-now, never behind a parked one by a stale timestamp.
        effective_due = case((parked, SeasonRequest.next_search_at))
        stmt = (
            select(SeasonRequest, MediaRequest.tmdb_id)
            .join(MediaRequest, MediaRequest.id == SeasonRequest.media_request_id)
            .where(
                SeasonRequest.status.in_([RequestStatus(s) for s in statuses]),
                due,
            )
            .order_by(effective_due.asc().nulls_first(), SeasonRequest.id)
        )
        return [
            _to_record(row, tmdb_id) for row, tmdb_id in (await self._session.execute(stmt)).all()
        ]

    async def schedule_search(
        self, season_request_id: int, *, search_attempts: int, next_search_at: datetime | None
    ) -> None:
        row = await self._session.get(SeasonRequest, season_request_id)
        if row is None:
            raise LookupError(f"season request {season_request_id} does not exist")
        row.search_attempts = search_attempts
        row.next_search_at = next_search_at
        await self._session.flush()

    async def ensure(
        self, media_request_id: int, season_number: int, *, status: str
    ) -> SeasonRequestRecord:
        existing = await self._find(media_request_id, season_number)
        if existing is not None:
            return _to_record(existing, await self._tmdb_id_for(media_request_id))

        row = SeasonRequest(
            media_request_id=media_request_id,
            season_number=season_number,
            status=RequestStatus(status),
        )
        try:
            # A SAVEPOINT (not a full transaction rollback): on IntegrityError only
            # THIS insert is undone. ``request_service.create_request`` /
            # ``grab_service.grab`` can get away with a plain ``session.rollback()``
            # because their insert is the FIRST write of their transaction; ``ensure()``
            # has no such guarantee -- it is meant to be called repeatedly, once per
            # season, inside a single caller-owned transaction (``ensure_seasons``), so
            # losing one season's race must never wipe out sibling seasons already
            # flushed earlier in the SAME transaction.
            async with self._session.begin_nested():
                self._session.add(row)
                await self._session.flush()
        except IntegrityError:
            # A concurrent ensure() for the SAME (show, season) won the race: the
            # unconditional ``uq_season_requests_media_season`` unique index
            # rejected this insert. Resolve to the winner's row instead of
            # crashing (idempotent dedup, honesty over silence) -- mirrors the
            # IntegrityError-catch-and-reread pattern at
            # ``request_service.py:159-184``.
            winner = await self._find(media_request_id, season_number)
            if winner is None:  # pragma: no cover - the conflicting row must exist
                raise
            return _to_record(winner, await self._tmdb_id_for(media_request_id))
        await self._session.refresh(row)
        return _to_record(row, await self._tmdb_id_for(media_request_id))

    async def set_status(self, season_request_id: int, status: str) -> None:
        row = await self._session.get(SeasonRequest, season_request_id)
        if row is None:
            raise LookupError(f"season request {season_request_id} does not exist")
        row.status = RequestStatus(status)
        await self._session.flush()

    async def set_status_if_in(
        self,
        season_request_id: int,
        status: str,
        allowed_from: frozenset[str],
        *,
        require_parent_unpinned: bool = False,
    ) -> bool:
        """Compare-and-swap: move to ``status`` only if the row's CURRENT persisted
        status is in ``allowed_from`` (and, with ``require_parent_unpinned``, only
        if the PARENT show is not ``keep_forever``-pinned). Returns whether a row
        was actually updated.

        The season-granularity mirror of ``SqlRequestRepository.set_status_if_in``
        (see its docstring for the full rationale) -- the eviction sweep's
        AUTHORITATIVE double-count guard (ADR-0012, C6) for TV. Backs
        ``season_request_service.set_status_if_in``, which additionally
        recomputes the parent rollup, but ONLY when this CAS actually changed
        the row -- a losing sweep must never re-derive (and re-persist) a
        rollup off a row it did not actually get to move.

        ``require_parent_unpinned`` (opt-in for the eviction CLAIM, ADR-0012 #67)
        folds the TV pin into the compared predicate. The pin lives on the PARENT
        ``MediaRequest`` (``keep_forever``), never on the season row, so -- unlike
        the movie CAS, which compares ``keep_forever`` on its own row -- this needs
        a correlated ``EXISTS`` subquery: the UPDATE additionally requires that NO
        parent row with this season's ``media_request_id`` is pinned. Because the
        eviction claim now runs this CAS BEFORE any filesystem delete, a pin that
        commits on the parent after a season candidate was assembled but before the
        claim makes the UPDATE match zero rows -- the DATABASE atomically refuses to
        delete a season whose show was just pinned, rather than a read-then-act
        check that a concurrent pin could slip past.
        """
        predicates = [
            SeasonRequest.id == season_request_id,
            SeasonRequest.status.in_([RequestStatus(s) for s in allowed_from]),
        ]
        if require_parent_unpinned:
            parent_pinned = (
                select(MediaRequest.id)
                .where(
                    MediaRequest.id == SeasonRequest.media_request_id,
                    MediaRequest.keep_forever.is_(True),
                )
                .exists()
            )
            predicates.append(~parent_pinned)
        stmt = (
            update(SeasonRequest)
            .where(*predicates)
            .values(status=RequestStatus(status))
            .execution_options(synchronize_session="fetch")
        )
        result = cast(CursorResult[Any], await self._session.execute(stmt))
        return result.rowcount == 1

    async def mark_completed(self, season_request_id: int) -> None:
        """Set ``completed`` (imported, scan triggered) -- the honest pre-``available`` state."""
        await self.set_status(season_request_id, RequestStatus.completed.value)

    async def mark_available(self, season_request_id: int) -> None:
        """Set ``available`` (Plex-confirmed: ``leafCount>0`` for this season)."""
        await self.set_status(season_request_id, RequestStatus.available.value)

    async def set_library_path(self, season_request_id: int, library_path: str) -> None:
        """Store the final placed path this season's import wrote into (ADR-0012)."""
        row = await self._session.get(SeasonRequest, season_request_id)
        if row is None:
            raise LookupError(f"season request {season_request_id} does not exist")
        row.library_path = library_path
        await self._session.flush()

    async def clear_library_path(self, season_request_id: int) -> None:
        """Drop the eviction/purge breadcrumb (ADR-0014's report-issue verb).

        The season-level mirror of ``SqlRequestRepository.reset_for_research``'s
        ``library_path`` clear: after report-issue purges the season's placed file
        the breadcrumb must not keep pointing at a path that no longer exists (a
        later sweep would only skip+log it, but leaving a stale breadcrumb is
        dishonest). No-op-safe if the row vanished.
        """
        row = await self._session.get(SeasonRequest, season_request_id)
        if row is None:
            raise LookupError(f"season request {season_request_id} does not exist")
        row.library_path = None
        await self._session.flush()
