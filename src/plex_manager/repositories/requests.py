"""``RequestRepository`` implementation over an :class:`AsyncSession`."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import CursorResult, case, func, insert, or_, select, update
from sqlalchemy import delete as sa_delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import aliased

from plex_manager.models import (
    Download,
    DownloadHistory,
    DownloadHistoryEvent,
    MediaRequest,
    MediaType,
    RequestDedupLock,
    RequestStatus,
    SeasonRequest,
)
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


def _season_tuple(value: Sequence[Any] | None) -> tuple[int, ...] | None:
    if value is None:
        return None
    return tuple(sorted({int(v) for v in value}))


def _season_json(value: Sequence[int] | None) -> list[int] | None:
    if value is None:
        return None
    return list(_season_tuple(value) or [])


def _episode_map(value: dict[str, Any] | None) -> dict[int, tuple[int, ...]] | None:
    if value is None:
        return None
    return {
        int(season): tuple(sorted({int(episode) for episode in cast(list[Any], episodes)}))
        for season, episodes in value.items()
        if isinstance(episodes, list)
    }


def _episode_json(
    value: Mapping[int, Sequence[int]] | None,
) -> dict[str, list[int]] | None:
    if value is None:
        return None
    return {
        str(int(season)): sorted({int(episode) for episode in episodes})
        for season, episodes in value.items()
    }


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
        completed_at=_as_utc(row.completed_at),
        keep_forever=bool(row.keep_forever),
        search_attempts=row.search_attempts,
        next_search_at=_as_utc(row.next_search_at),
        # NULL (every pre-migration row, or one inserted outside this app's own
        # create path) reads as ``False`` -- "not an eviction regrab" is the safe
        # default (see the column's docstring in ``models.py``).
        eviction_regrab=bool(row.eviction_regrab),
        tv_request_mode=row.tv_request_mode,
        requested_seasons=_season_tuple(row.requested_seasons_json),
        requested_episodes=_episode_map(row.requested_episodes_json),
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

    async def list_personalization_history(self, user_id: int) -> list[RequestRecord]:
        """Return only ``user_id``'s retained request intent, one row per title.

        Privacy is enforced in SQL, not by loading global history and filtering in
        Python. Cancelled rows are withdrawn intent and excluded before duplicate
        collapse. For each media identity the same display rule as Discover tiles
        applies: the oldest active row wins, otherwise the newest settled row.
        Chosen rows are returned by id for a deterministic repository contract.
        """
        stmt = (
            select(MediaRequest)
            .where(
                MediaRequest.user_id == user_id,
                MediaRequest.status != RequestStatus.cancelled,
            )
            .order_by(MediaRequest.id)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        grouped: dict[tuple[int, str], list[MediaRequest]] = {}
        for row in rows:
            grouped.setdefault((row.tmdb_id, row.media_type.value), []).append(row)

        chosen: list[MediaRequest] = []
        for group in grouped.values():
            active = next(
                (row for row in group if row.status not in _SETTLED_REQUEST_STATUSES), None
            )
            chosen.append(active if active is not None else group[-1])
        chosen.sort(key=lambda row: row.id)
        return [_to_record(row) for row in chosen]

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

    async def find_in_library(
        self, tmdb_id: int, media_type: str, *, prefer_user_id: int | None = None
    ) -> RequestRecord | None:
        """Return an already-in-library (available/completed) request, or ``None``.

        ``prefer_user_id`` (the per-user visibility scope — see the port
        docstring) reorders WHICH terminal row wins when several exist for the
        same media: (a) a row OWNED by that user first, then (b) an OWNERLESS
        (claimable) row, then (c) anyone else's — newest-by-id within each rank.
        Without the preference, the newest GLOBAL row wins unconditionally, so a
        user whose own older ``available`` row is shadowed by another user's
        newer one would be handed the foreign row — which the service can only
        honestly reject (409) even though a perfectly returnable row of their own
        exists. ``None`` (admins / API-key automation) keeps the plain
        newest-row-wins behavior unchanged.
        """
        stmt = select(MediaRequest).where(
            MediaRequest.tmdb_id == tmdb_id,
            MediaRequest.media_type == MediaType(media_type),
            MediaRequest.status.in_([RequestStatus.available, RequestStatus.completed]),
        )
        if prefer_user_id is not None:
            ownership_rank = case(
                (MediaRequest.user_id == prefer_user_id, 0),
                (MediaRequest.user_id.is_(None), 1),
                else_=2,
            )
            stmt = stmt.order_by(ownership_rank, MediaRequest.id.desc()).limit(1)
        else:
            stmt = stmt.order_by(MediaRequest.id.desc()).limit(1)
        row = (await self._session.execute(stmt)).scalars().first()
        return _to_record(row) if row is not None else None

    async def latest_request_evicted(self, tmdb_id: int, media_type: str) -> bool:
        """Whether the NEWEST request row for this media is ``evicted`` (ADR-0012).

        The in-library short-circuit (``request_service.create_request``) consults
        this AFTER :meth:`find_in_library` finds no ``available``/``completed`` row.
        A fresh ``LibraryPort.is_available`` reading is eventually-consistent and
        STALE during the eviction delete window: the sweep commits the row
        ``evicted`` BEFORE it unlinks the file and BEFORE the post-delete Plex
        refresh (``eviction_service._evict_one``, ADR-0012 #67), so for that whole
        window Plex still reports the (doomed / just-removed) file present. Minting
        a fresh ``available`` row off that stale reading would leave a request
        marked available with nothing on disk to watch and nothing queued to grab
        -- the exact race this guards. When this returns ``True`` the caller
        re-grabs (``pending``) instead of trusting Plex.

        Keyed on the NEWEST NON-``cancelled`` row (``ORDER BY id DESC``) so a
        movie legitimately re-downloaded after an earlier eviction (whose newest
        row is ``available`` / active, already resolved by ``find_in_library`` /
        ``find_active`` before this is ever reached) is never falsely suppressed
        -- only a media whose most recent history really is an eviction is
        treated as stale-in-Plex. ``cancelled`` rows are IGNORED outright rather
        than counted as "newest": a cancellation says nothing about on-disk
        truth, so an in-window re-grab the user then cancelled (evicted ->
        pending -> cancelled) must not reset this guard and let the NEXT
        re-request mint ``available`` off the same stale Plex reading while the
        sweep is still deleting the file.
        """
        stmt = (
            select(MediaRequest.status)
            .where(
                MediaRequest.tmdb_id == tmdb_id,
                MediaRequest.media_type == MediaType(media_type),
                MediaRequest.status != RequestStatus.cancelled,
            )
            .order_by(MediaRequest.id.desc())
            .limit(1)
        )
        status = (await self._session.execute(stmt)).scalars().first()
        return status == RequestStatus.evicted

    async def list_for_media(
        self, tmdb_id: int, media_type: str, statuses: frozenset[str]
    ) -> list[RequestRecord]:
        """Every request row for ``(tmdb_id, media_type)`` whose status is in
        ``statuses``, oldest first.

        Backs the eviction restore's re-grab reconciliation (ADR-0012 #67):
        after a failed/interrupted delete restores the claimed row to
        ``available``, any in-window re-request for the SAME media that has not
        yet grabbed anything (``pending``/``searching``/``no_acceptable_release``)
        is redundant -- the file never left -- and the restore cancels it via a
        per-row :meth:`set_status_if_in` CAS. A plain read, not itself a CAS: the
        caller must re-compare on write.
        """
        stmt = (
            select(MediaRequest)
            .where(
                MediaRequest.tmdb_id == tmdb_id,
                MediaRequest.media_type == MediaType(media_type),
                MediaRequest.status.in_([RequestStatus(s) for s in statuses]),
            )
            .order_by(MediaRequest.id)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_record(row) for row in rows]

    async def clear_library_path_if_set(
        self,
        request_id: int,
        *,
        expected_path: str | None = None,
        expected_statuses: frozenset[str] | None = None,
    ) -> bool:
        """Null the eviction breadcrumb ONLY if it is currently set (and, with
        ``expected_path``, only if it still holds EXACTLY that value; with
        ``expected_statuses``, only if it is still in one of those statuses);
        return whether this call actually cleared it.

        The guarded (``WHERE library_path IS NOT NULL``) variant of
        :meth:`clear_library_path`, for the eviction finalize (ADR-0012 #67):
        clearing the breadcrumb is what marks a claimed ``evicted`` row as
        FINALIZED (file gone), and two resume passes racing over the same
        interrupted eviction use this as their single-winner gate -- only the
        caller that actually cleared it (``True``) writes the history row, so a
        concurrent finalize never double-records the same eviction.

        ``expected_path`` makes the clear VALUE-predicated (``AND library_path =
        expected_path``): the eviction finalize/recovery observes a specific
        stale path, and must never wipe a FRESH breadcrumb a replacement import
        stamped onto the row between recovery's stat and this write -- a
        mismatch means that newer import owns the row now, so the caller leaves
        it (logged), keeping the row's eviction/report handle intact.
        """
        predicates = [MediaRequest.id == request_id, MediaRequest.library_path.is_not(None)]
        if expected_path is not None:
            predicates.append(MediaRequest.library_path == expected_path)
        if expected_statuses is not None:
            predicates.append(
                MediaRequest.status.in_([RequestStatus(status) for status in expected_statuses])
            )
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(MediaRequest)
                .where(*predicates)
                .values(library_path=None)
                .execution_options(synchronize_session="fetch")
            ),
        )
        return result.rowcount == 1

    async def other_row_claims_path(
        self, library_path: str, *, exclude_request_id: int | None = None
    ) -> bool:
        """Whether any (other) request row currently claims ``library_path``.

        The eviction recovery pass's finalized-vs-interrupted discriminator
        (ADR-0012 #67): a breadcrumb whose exact path another LIVE row also
        carries belongs to a media that was re-imported in place under a newer
        request -- restoring the stale row would put two rows over one file, and
        a later sweep evicting either would delete the path out from under the
        actual owner. ``evicted``/``cancelled`` rows do not count as claims
        (their content claim is dead by definition). ``exclude_request_id`` is
        the row being recovered itself.
        """
        predicates = [
            MediaRequest.library_path == library_path,
            MediaRequest.status.notin_([RequestStatus.evicted, RequestStatus.cancelled]),
        ]
        if exclude_request_id is not None:
            predicates.append(MediaRequest.id != exclude_request_id)
        stmt = select(MediaRequest.id).where(*predicates).limit(1)
        return (await self._session.execute(stmt)).scalars().first() is not None

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
        self, keys: Sequence[tuple[int, str]], *, for_user_id: int | None = None
    ) -> dict[tuple[int, str], str]:
        """Batch the DISPLAY status per ``(tmdb_id, media_type)`` — see the port docstring.

        ONE ``SELECT ... WHERE tmdb_id IN (...)`` over the distinct tmdb ids, then
        the pairs are grouped in Python: a composite ``(tmdb_id, media_type)`` tuple
        IN is deliberately avoided (SQLite/PostgreSQL differ on tuple IN, and the
        backend is a config swap). Rows come back ``id``-ascending, so per key the
        first non-settled row is the lowest-id ACTIVE one (matching ``find_active``)
        and ``group[-1]`` is the newest fallback when every row is settled.

        ``for_user_id`` (when set) restricts the scan to that user's own rows —
        the shared-session visibility scope; see the port docstring.
        """
        key_set = set(keys)
        if not key_set:
            return {}
        tmdb_ids = {tmdb_id for tmdb_id, _ in key_set}
        stmt = select(MediaRequest).where(MediaRequest.tmdb_id.in_(tmdb_ids))
        if for_user_id is not None:
            stmt = stmt.where(MediaRequest.user_id == for_user_id)
        stmt = stmt.order_by(MediaRequest.id)
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

    async def list_false_available_movies(self, *, limit: int) -> list[RequestRecord]:
        """Movie rows ``status='available' AND library_path IS NULL AND
        available_heal_verified_at IS NULL``, id-ascending.

        The EXACT false-claim signature the already-in-library short-circuit mints
        (``request_service.create_request_result``): ``mark_available`` never sets
        ``library_path``, so a movie promoted straight from the short-circuit
        carries no breadcrumb, while a normally-imported movie always has one
        (``set_library_path`` runs before promotion). A TV parent's ``available``
        + ``library_path IS NULL`` is a DIFFERENT, legitimate shape (the column
        lives on ``SeasonRequest`` for tv) -- scoped to ``movie`` only so a TV
        rollup row is never mistaken for a false claim. ``id``-ascending + capped
        at ``limit`` bounds the healing pass so a reconcile tick stays cheap.

        ``available_heal_verified_at IS NULL`` is the CONVERGENCE guard: once the
        heal pass (``import_service._heal_false_available_movies``) live-
        reconfirms a row genuinely present, it stamps that column (see
        :meth:`mark_heal_verified_present`) and the row permanently exits this
        scan population. Without it, a genuinely-present row (which never gets a
        ``library_path`` -- there was never a file for THIS app to place) would
        keep the exact same signature forever: re-fetched, re-verified, and
        re-occupying the bounded per-tick ``limit`` window on EVERY reconcile
        tick, which could starve out a later, higher-id GENUINE false claim from
        ever being scanned at all.
        """
        stmt = (
            select(MediaRequest)
            .where(
                MediaRequest.media_type == MediaType.movie,
                MediaRequest.status == RequestStatus.available,
                MediaRequest.library_path.is_(None),
                MediaRequest.available_heal_verified_at.is_(None),
            )
            .order_by(MediaRequest.id)
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_record(row) for row in rows]

    async def mark_heal_verified_present(self, request_id: int) -> bool:
        """CAS-stamp ``available_heal_verified_at`` for a false-available-heal
        candidate the pass just live-reconfirmed genuinely present in Plex.

        A single ``UPDATE ... WHERE id = ? AND status = 'available' AND
        library_path IS NULL AND available_heal_verified_at IS NULL`` -- the
        DATABASE, not a prior read, decides whether the exact false-claim
        signature still holds (mirrors :meth:`rearm_false_available_to_pending`'s
        CAS discipline). The ``available_heal_verified_at IS NULL`` arm makes
        convergence a ONE-WAY door: once stamped, a repeat call (e.g. a stale
        in-memory candidate list from a concurrent/overlapping cycle) is an
        honest no-op rather than pointlessly re-writing the same stamp. Also
        re-stamps ``library_verified_at`` (the row's presence WAS just
        reconfirmed). This is what makes the heal pass CONVERGE: the row
        permanently exits :meth:`list_false_available_movies`'s scan population
        afterward, instead of keeping the exact same signature and being
        re-verified (and re-occupying the bounded per-tick scan window) every
        reconcile tick forever. Returns whether a row was actually updated --
        ``False`` means a concurrent writer already moved the row off this exact
        signature (e.g. collapsed it onto a sibling, re-armed it, or already
        stamped it) and this call must not clobber that outcome.
        """
        now = datetime.now(UTC)
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(MediaRequest)
                .where(
                    MediaRequest.id == request_id,
                    MediaRequest.status == RequestStatus.available,
                    MediaRequest.library_path.is_(None),
                    MediaRequest.available_heal_verified_at.is_(None),
                )
                .values(available_heal_verified_at=now, library_verified_at=now)
                .execution_options(synchronize_session="fetch")
            ),
        )
        return result.rowcount == 1

    async def rearm_false_available_to_pending(self, request_id: int) -> bool:
        """Re-arm a healed false-available row to ``pending`` (honest re-search).

        A single ``UPDATE ... WHERE id = ? AND status = 'available' AND
        library_path IS NULL`` -- the DATABASE, not a prior read, decides whether
        the exact false-claim signature still holds (mirrors :meth:`set_status_if_in`'s
        CAS discipline). Clears ``library_verified_at``/``completed_at`` so the row
        stops asserting in-library (honesty over silence) and resets the auto-grab
        backoff (``search_attempts``/``next_search_at``) so ``list_due_for_search``
        picks the re-armed row up on the very next tick rather than waiting out a
        stale schedule. ``library_path`` is left untouched (already ``NULL``).
        Returns whether a row was actually updated -- ``False`` means a concurrent
        writer already moved the row off this exact signature, and the caller must
        not assume its own stale read still applies.
        """
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(MediaRequest)
                .where(
                    MediaRequest.id == request_id,
                    MediaRequest.status == RequestStatus.available,
                    MediaRequest.library_path.is_(None),
                )
                .values(
                    status=RequestStatus.pending,
                    library_verified_at=None,
                    completed_at=None,
                    search_attempts=0,
                    next_search_at=None,
                )
                .execution_options(synchronize_session="fetch")
            ),
        )
        return result.rowcount == 1

    async def delete_false_available_sibling_collapse(
        self, request_id: int, *, expected_user_id: int | None
    ) -> bool:
        """CAS-delete a false-available heal candidate collapsing onto a sibling
        (branch 1 of ``_heal_false_available_movies``'s sibling collapse).

        A single ``DELETE ... WHERE id = ? AND status = 'available' AND
        library_path IS NULL AND user_id IS <expected_user_id>`` -- the DATABASE,
        not the caller's earlier read, decides whether the row's ownership is
        STILL exactly what the caller's ownership-guard decision (mirroring
        ``request_service._owned_by_another_user``) was computed against.

        Why this matters (issue #58 class): the heal pass reads its candidates'
        owners once, at the very top of ``run_availability_cycle``, then does a
        ``present_ids`` Plex crawl and (for cross-owner siblings) a
        ``confirm_paths`` crawl before this delete ever fires -- a multi-second
        window. An OWNERLESS candidate (``expected_user_id`` ``None``) ranks
        ABOVE a foreign-owned real-path sibling in ``find_in_library``, so a
        concurrent user create can adopt (claim) that exact row in the window
        and return it as THEIR just-succeeded request. A plain ``delete(id)``
        would then silently vanish that user's request. This CAS ties the
        delete to the SAME ownership snapshot the caller's safe-sibling
        decision used: if a concurrent claim (or any other write) has moved the
        row off that exact ``(status, library_path, user_id)`` signature,
        ``rowcount`` is 0 and nothing is deleted -- the caller must leave the
        row for the next cycle to re-evaluate from scratch, never blindly retry
        the delete against the new owner.
        """
        stmt = sa_delete(MediaRequest).where(
            MediaRequest.id == request_id,
            MediaRequest.status == RequestStatus.available,
            MediaRequest.library_path.is_(None),
        )
        if expected_user_id is None:
            stmt = stmt.where(MediaRequest.user_id.is_(None))
        else:
            stmt = stmt.where(MediaRequest.user_id == expected_user_id)
        result = cast(CursorResult[Any], await self._session.execute(stmt))
        return result.rowcount == 1

    async def latest_library_path(self, tmdb_id: int, media_type: str) -> str | None:
        """The ``library_path`` breadcrumb of the NEWEST row (id DESC) for this
        media that carries one, across ANY status, or ``None``.

        The only handle to a file THIS app previously placed for
        ``(tmdb_id, media_type)`` -- backs the dedup-time breadcrumb corroboration
        gate (``request_service.create_request_result``'s in-library short-circuit):
        a prior terminal row's real path is the one grounded signal available at
        create time to corroborate a GUID-present-but-possibly-mistagged movie
        before minting a fresh terminal ``available`` row.
        """
        stmt = (
            select(MediaRequest.library_path)
            .where(
                MediaRequest.tmdb_id == tmdb_id,
                MediaRequest.media_type == MediaType(media_type),
                MediaRequest.library_path.is_not(None),
            )
            .order_by(MediaRequest.id.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalars().first()

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
        eviction_regrab: bool = False,
        tv_request_mode: str | None = None,
        requested_seasons: Sequence[int] | None = None,
        requested_episodes: Mapping[int, Sequence[int]] | None = None,
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
            eviction_regrab=eviction_regrab,
            tv_request_mode=tv_request_mode,
            requested_seasons_json=_season_json(requested_seasons),
            requested_episodes_json=_episode_json(requested_episodes),
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return _to_record(row)

    async def set_tv_request_intent(
        self,
        request_id: int,
        *,
        mode: str,
        requested_seasons: Sequence[int] | None,
        requested_episodes: Mapping[int, Sequence[int]] | None = None,
    ) -> None:
        row = await self._session.get(MediaRequest, request_id)
        if row is None:
            raise LookupError(f"media request {request_id} does not exist")
        row.tv_request_mode = mode
        row.requested_seasons_json = _season_json(requested_seasons)
        row.requested_episodes_json = _episode_json(requested_episodes)
        await self._session.flush()

    async def set_status(self, request_id: int, status: str) -> None:
        row = await self._session.get(MediaRequest, request_id)
        if row is None:
            raise LookupError(f"media request {request_id} does not exist")
        row.status = RequestStatus(status)
        await self._session.flush()

    async def set_status_if_in(
        self,
        request_id: int,
        status: str,
        allowed_from: frozenset[str],
        *,
        require_unpinned: bool = False,
    ) -> bool:
        """Compare-and-swap: move to ``status`` only if the row's CURRENT persisted
        status is in ``allowed_from`` (and, with ``require_unpinned``, only if the
        row is not ``keep_forever``-pinned). Returns whether a row was actually
        updated.

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

        ``require_unpinned`` (opt-in for the eviction CLAIM, ADR-0012 #67) folds the
        pin into the compared predicate: ``... AND keep_forever = false``. Because
        the eviction claim now runs this CAS BEFORE any filesystem delete, a
        ``keep_forever`` pin that commits after candidate assembly but before the
        claim makes the UPDATE match zero rows -- the DATABASE, not a read-then-act
        check, is what refuses to delete a freshly-pinned title. ``keep_forever``
        lives on the same row as ``status``, so a single UPDATE can compare both
        atomically (the TV pin lives on the PARENT row and needs a subquery -- see
        ``SqlSeasonRequestRepository.set_status_if_in``'s ``require_parent_unpinned``).

        ``synchronize_session="fetch"`` keeps any already-loaded identity-map
        instance (e.g. from this session's own ``get_fresh`` re-check moments
        earlier) consistent with the DB result, so anything read afterwards in
        THIS session (an eviction sweep never re-reads the row again, but mirrors
        ``update_status_if_in`` for consistency) sees the honest post-CAS status.
        """
        predicates = [
            MediaRequest.id == request_id,
            MediaRequest.status.in_([RequestStatus(s) for s in allowed_from]),
        ]
        if require_unpinned:
            predicates.append(MediaRequest.keep_forever.is_(False))
        stmt = (
            update(MediaRequest)
            .where(*predicates)
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
        """Set ``available`` + stamp ``library_verified_at`` (Plex-confirmed).

        Also clears ``eviction_regrab`` (issue #156 lifecycle fix, Codex round-2):
        the marker means "this row is THIS eviction's own still-in-flight regrab",
        and a row that has now genuinely imported and been confirmed watchable is
        no longer in flight -- it is exactly as settled as any other available
        request. Leaving the marker set past this point would let a LATER,
        UNRELATED eviction's failed-delete restore
        (``eviction_service._cancel_redundant_movie_regrabs``) cancel this row
        purely because it once was a regrab, even though its own content is now
        the genuinely watchable copy.
        """
        row = await self._session.get(MediaRequest, request_id)
        if row is None:
            raise LookupError(f"media request {request_id} does not exist")
        now = datetime.now(UTC)
        row.status = RequestStatus.available
        row.library_verified_at = now
        if row.completed_at is None:
            row.completed_at = now
        row.eviction_regrab = False
        await self._session.flush()

    async def claim_if_unowned(self, request_id: int, user_id: int) -> bool:
        """Assign ``user_id`` to a request that currently has NO owner.

        A single ``UPDATE ... WHERE id = ? AND user_id IS NULL`` so the DATABASE
        decides: an already-owned row is left untouched (an existing owner is never
        reassigned). Returns whether a row was actually claimed.

        Used on the create-dedup path so a signed-in user whose request collapses
        onto a previously ownerless active request (e.g. one created via the
        API-key automation path, which carries no user identity) has the request
        show up in THEIR own list, rather than succeeding yet silently vanishing
        behind the per-user list filter.
        """
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(MediaRequest)
                .where(MediaRequest.id == request_id, MediaRequest.user_id.is_(None))
                .values(user_id=user_id)
            ),
        )
        return result.rowcount == 1

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

        Also clears ``eviction_regrab`` (issue #156 lifecycle fix, Codex round-2):
        the operator re-arming this row for a BRAND-NEW search is the row leaving
        "THIS eviction's own in-flight regrab" behind -- it is now the operator's
        own deliberate re-search, whatever its provenance was before. Without this,
        a stale marker on a row that later got re-armed here (report-issue) could
        let a DIFFERENT, unrelated eviction's failed-delete restore
        (``eviction_service._cancel_redundant_movie_regrabs``) cancel the
        operator's live re-search purely because of the row's old history.
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
        row.eviction_regrab = False
        await self._session.flush()

    async def heal_completed_at(self, request_id: int) -> None:
        """Re-align a TV parent's ``completed_at`` with its committed done seasons.

        INVARIANT (shared with ``season_request_service.reset_for_research`` /
        ``ensure_seasons``, agreeing with ``_recompute_parent``'s stamp and
        :meth:`stamp_completed_at_if_unset`): after this heal, ``completed_at`` is
        non-``NULL`` iff some tracked season GENUINELY completed an import and is
        still ``completed``/``available`` -- keeping the FIRST stamp untouched when
        one is already set (the never-moves rule), clearing it when nothing backs
        it (so the next genuine re-completion can re-stamp through the ``IS NULL``
        guard, #76), and re-stamping when a backing season exists but the stamp was
        lost. Idempotent and self-correcting: re-running it never changes a
        consistent row. A TV-parent verb only -- a movie request has no season
        rows, so this would always read "unbacked" for one; movies keep using
        :meth:`reset_for_research`'s direct clear.

        "GENUINELY imported" discriminator -- breadcrumb OR imported-download
        linkage, per season:

        * ``SeasonRequest.library_path IS NOT NULL``: ``import_service.
          _import_tv_locked`` writes the breadcrumb in the SAME transaction as
          ``mark_completed`` for every current-version import; ``ensure_seasons``'s
          already-in-Plex creation leaves it ``NULL`` (no import ever ran).
        * OR an ``imported`` ``Download`` row for the same ``(media_request_id,
          season)`` NOT invalidated by a LATER eviction (see below): the import
          finalize CAS moves the placing download to ``imported`` in that same
          transaction, and this linkage is exactly how report-issue resolves its
          culprit ("the download that OWNS the placed library file" --
          ``SqlDownloadRepository.find_latest_imported_for_request``). This arm
          covers LEGACY seasons imported before the breadcrumb column existed
          (``models.SeasonRequest.library_path``: "``None`` for seasons imported
          before this breadcrumb existed") -- their genuinely-backed stamp must
          not be erased just because the breadcrumb predates them. A
          Plex-present-only season (the ``ensure_seasons`` short-circuit) has
          NEITHER marker -- no grab ever ran for that season under this request,
          so no ``imported`` download row can exist for the pair -- and stays
          excluded. Known best-effort edge: a terminal download row can later be
          re-owned by a fresh grab of the same torrent (``update_status``'s
          ``replace_grab_metadata`` path rewrites its request/season scope),
          which could drop legacy evidence; that failure degrades to the
          conservative clear-and-restamp-on-reimport behavior, never to counting
          a Plex-present-only season.

        Eviction invalidates the download arm (Codex round-3): eviction flips the
        season row and writes history but NEVER touches the old ``Download`` row
        (cross-aggregate mutation would be worse than the bug), so its
        ``imported`` status survives the file's deletion. Without an ordering
        clause, an evicted season re-armed straight to ``available`` (Plex still
        reports it present; ``ensure_seasons`` clears the breadcrumb) would make
        its PRE-EVICTION download look like current backing evidence, preserving
        or re-stamping a ``completed_at`` nothing current supports. The honest
        committed markers that order "import happened" vs "eviction happened
        since" are both in the append-only ``download_history`` log: the import
        finalize writes hash-tied ``imported`` events (``import_service.
        _import_tv_locked``), and ``eviction_service._evict_one`` writes an
        ``evicted`` event -- ``torrent_hash=None`` by design (see
        ``DownloadHistoryEvent.evicted``) with only ``tmdb_id`` queryable (the
        season number lives in prose ``message`` text, which is not a schema). So
        the arm requires: NO ``evicted`` event for the parent's ``tmdb_id`` with
        an ``id`` greater than the download's latest ``imported`` event id
        (``download_history.id`` is the append-only order; a download with no
        recorded import event is invalidated by ANY eviction of the show). Two
        documented coarseness edges, both failing CONSERVATIVE (an over-eager
        clear that the next genuine re-import re-stamps, never a wrongly
        preserved stamp, never counting a Plex-present row): (a) the show-scoped
        ``tmdb_id`` means a SIBLING season's later eviction also discounts this
        season's pre-eviction download evidence; (b) ``download_history.tmdb_id``
        is not media-type-namespaced, so a movie sharing the show's tmdb id that
        was evicted later does too. A genuine re-import after an eviction writes
        a NEWER ``imported`` event for its hash, so its evidence validly counts
        again.

        Shape -- two atomic conditional ``UPDATE``s whose predicate lives in their
        OWN ``WHERE`` (DB-authoritative at statement time, never a prior Python
        snapshot; the same discipline as :meth:`set_status_if_in`):

        1. clear-if-unbacked: ``SET completed_at = NULL`` only while NO qualifying
           season exists.
        2. re-stamp-if-backed-but-``NULL``: stamp now() when a qualifying season
           exists but the stamp is missing. This is the recovery arm for the
           masked-sibling TOCTOU on MVCC (the Postgres-ready posture): a sibling
           import finalizing concurrently while MASKED by a higher-precedence
           season (e.g. a third season ``downloading``) never touches the parent
           row -- its rollup write is a no-change and its
           ``stamp_completed_at_if_unset`` no-ops against the still-non-``NULL``
           stale stamp -- so a clear whose statement snapshot predates that commit
           erases the stamp with nobody left to re-stamp. Statement 2 runs with a
           FRESH statement snapshot (READ COMMITTED) and repairs exactly that
           aftermath. The re-stamp value is ``now()`` because the schema records
           NO per-season completion timestamp to recompute the true import time
           from (``Download.completed_at`` is never written by any code path;
           ``retention_telemetry_service`` documents the deliberately-deferred
           per-season ``completed_at`` column) -- in the race aftermath now() is
           within moments of the sibling's actual import commit, and in the legacy
           never-stamped case it restores a backed, re-clearable stamp rather than
           a permanently-unknown one.

        RESIDUAL WINDOW, stated honestly: SQLite's single writer serializes the
        whole heal. On Postgres READ COMMITTED, a sibling whose season write AND
        commit both land between statement 1's snapshot and statement 2's
        snapshot -- while its own conditional stamp evaluated against our
        not-yet-committed clear -- can still end un-stamped. The heal is
        idempotent, so the NEXT invocation (any later report-issue or evicted
        re-arm on this show) repairs it; closing the window entirely would require
        the import hot path and this heal to serialize on a parent-row lock
        (``SELECT FOR UPDATE``), which the import path deliberately does not take.
        """
        # Aliased so the imported-event lookup nested inside the eviction-event
        # subquery below does NOT auto-correlate to that enclosing
        # ``download_history`` SELECT -- they are two independent scans of the
        # same append-only log.
        imported_event = aliased(DownloadHistory)
        latest_import_event_id = (
            select(func.max(imported_event.id))
            .where(
                imported_event.torrent_hash == Download.torrent_hash,
                imported_event.event_type == DownloadHistoryEvent.imported,
            )
            # EXPLICIT: auto-correlation only reaches the nearest enclosing
            # SELECT; two levels deep it would re-introduce ``downloads`` as a
            # cartesian FROM entry instead (verified via compiled SQL).
            .correlate(Download)
            .scalar_subquery()
        )
        # "The show was evicted SINCE this download's import" -- the round-3
        # invalidation clause (see the docstring): an ``evicted`` history row for
        # this show appended after the download's latest ``imported`` event. A
        # download with NO recorded import event compares as -1, so any eviction
        # of the show invalidates it (conservative by design).
        evicted_since_import = (
            select(DownloadHistory.id)
            .where(
                DownloadHistory.tmdb_id == MediaRequest.tmdb_id,
                DownloadHistory.event_type == DownloadHistoryEvent.evicted,
                DownloadHistory.id > func.coalesce(latest_import_event_id, -1),
            )
            # EXPLICIT for the same reason: ``media_requests`` (the UPDATE
            # target) and ``downloads`` (the enclosing evidence SELECT) must
            # correlate outward, never join in locally.
            .correlate(MediaRequest, Download)
            .exists()
        )
        season_genuinely_imported = (
            select(Download.id)
            .where(
                Download.media_request_id == SeasonRequest.media_request_id,
                Download.season == SeasonRequest.season_number,
                # Literal (not the P4 ``DownloadState`` enum) -- mirrors
                # ``repositories.downloads``' deliberately-decoupled string
                # vocabulary; ``imported`` is that enum's value.
                Download.status == "imported",
                ~evicted_since_import,
            )
            .exists()
        )
        qualifying = (
            select(SeasonRequest.id)
            .where(
                # Correlate to the outer ``media_requests`` row so the guard is
                # re-evaluated against live DB state at UPDATE time, not a snapshot.
                SeasonRequest.media_request_id == MediaRequest.id,
                SeasonRequest.status.in_([RequestStatus.completed, RequestStatus.available]),
                or_(
                    SeasonRequest.library_path.is_not(None),
                    season_genuinely_imported,
                ),
            )
            .exists()
        )
        await self._session.execute(
            update(MediaRequest)
            .where(
                MediaRequest.id == request_id,
                MediaRequest.completed_at.is_not(None),
                ~qualifying,
            )
            .values(completed_at=None)
            .execution_options(synchronize_session="fetch")
        )
        await self._session.execute(
            update(MediaRequest)
            .where(
                MediaRequest.id == request_id,
                MediaRequest.completed_at.is_(None),
                qualifying,
            )
            .values(completed_at=datetime.now(UTC))
            .execution_options(synchronize_session="fetch")
        )

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
