"""``DownloadRepository`` implementation over an :class:`AsyncSession`.

``downloads.status`` is a free-form ``str`` column holding the P4
``DownloadState`` value. To keep this layer decoupled from the (separately
owned) state-machine enum, the terminal-state vocabulary is duplicated here as
string literals; it mirrors P4's terminal ``DownloadState`` members.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import CursorResult, delete, exists, or_, select, update

from plex_manager.models import (
    Download,
    DownloadCoverageClaim,
    DownloadScope,
    DownloadScopeStatus,
    MediaRequest,
    MediaType,
    SeasonRequest,
)
from plex_manager.ports.repositories import DownloadRecord, DownloadScopeRecord, QueueRecord

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["SqlDownloadRepository"]

# Downloads in one of these states are finished and excluded from the reconcile
# loop. Mirrors P4's terminal ``DownloadState`` values (string-compared because
# the column is a plain ``str`` and P4's enum is not a P2 dependency).
_TERMINAL_DOWNLOAD_STATUSES: frozenset[str] = frozenset(
    {"imported", "failed", "no_acceptable_release"}
)


class _NoReasonPredicate:
    """Sentinel type for :meth:`SqlDownloadRepository.update_status_if_in`'s
    ``require_failed_reason``: distinguishes "no predicate" (the default) from a
    real predicate value — which may legitimately be ``None`` ("the reason must
    currently be NULL")."""


NO_REASON_PREDICATE = _NoReasonPredicate()


def _as_utc(value: datetime | None) -> datetime | None:
    """Coerce a stored timestamp to tz-aware UTC.

    SQLite returns naive datetimes even for ``DateTime(timezone=True)`` columns;
    the app's stored values are always UTC (``datetime.now(timezone.utc)``), and
    the reconciler does aware-datetime arithmetic on ``first_seen_at``. Attaching
    UTC here keeps the DTO contract tz-aware regardless of backend.
    """
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


_ACTIVE_SCOPE_STATUSES: frozenset[str] = frozenset(
    {DownloadScopeStatus.active.value, DownloadScopeStatus.import_blocked.value}
)

# A physical-coverage claim (issue #456) is ``active`` while its torrent is live and
# ``released`` once the torrent terminates. Only ``active`` claims key the partial
# unique index and count for the guard; the release write matches only these.
_ACTIVE_CLAIM_STATUS = "active"
_RELEASED_CLAIM_STATUS = "released"


def _normalize_episodes(value: list[int] | None) -> list[int] | None:
    normalized = sorted({int(episode) for episode in value or []})
    return normalized or None


def _episodes_equal(left: list[int] | None, right: list[int] | None) -> bool:
    return _normalize_episodes(left) == _normalize_episodes(right)


def _scope_key(season: int | None, episodes: list[int] | None) -> str:
    episode_key = (
        json.dumps(_normalize_episodes(episodes), separators=(",", ":")) if episodes else "*"
    )
    return f"season:{season if season is not None else 'null'}|episodes:{episode_key}"


def _to_scope_record(row: DownloadScope) -> DownloadScopeRecord:
    return DownloadScopeRecord(
        id=row.id,
        download_id=row.download_id,
        media_request_id=row.media_request_id,
        season_request_id=row.season_request_id,
        season=row.season_number,
        episodes=row.episodes_json,
        status=row.status,
        completed_at=_as_utc(row.completed_at),
    )


def _to_record(row: Download, scopes: list[DownloadScopeRecord] | None = None) -> DownloadRecord:
    """Map a ``Download`` ORM row to its frozen read-model DTO."""
    return DownloadRecord(
        id=row.id,
        torrent_hash=row.torrent_hash,
        status=row.status,
        media_request_id=row.media_request_id,
        magnet_link=row.magnet_link,
        progress=row.progress,
        seed_ratio=row.seed_ratio,
        tmdb_id=row.tmdb_id,
        year=row.year,
        season=row.season,
        episodes=row.episodes_json,
        media_type=row.media_type.value if row.media_type is not None else None,
        failed_reason=row.failed_reason,
        retry_count=row.retry_count,
        first_seen_at=_as_utc(row.first_seen_at),
        added_at=_as_utc(row.added_at),
        timeout_at=_as_utc(row.timeout_at),
        download_path=row.download_path,
        release_title=row.release_title,
        scopes=tuple(scopes or ()),
    )


class SqlDownloadRepository:
    """Persist and read tracked downloads via SQLAlchemy."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _scopes_by_download(
        self, download_ids: list[int]
    ) -> dict[int, list[DownloadScopeRecord]]:
        if not download_ids:
            return {}
        stmt = (
            select(DownloadScope)
            .where(DownloadScope.download_id.in_(download_ids))
            .order_by(DownloadScope.download_id, DownloadScope.season_number, DownloadScope.id)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        grouped: dict[int, list[DownloadScopeRecord]] = {}
        for row in rows:
            grouped.setdefault(row.download_id, []).append(_to_scope_record(row))
        return grouped

    async def _to_record_with_scopes(self, row: Download) -> DownloadRecord:
        scopes = await self._scopes_by_download([row.id])
        return _to_record(row, scopes.get(row.id, []))

    async def _replace_scope_set(
        self,
        download_id: int,
        *,
        media_request_id: int | None,
        season: int | None,
        episodes: list[int] | None,
    ) -> None:
        await self._session.execute(
            delete(DownloadScope).where(
                DownloadScope.download_id == download_id,
                DownloadScope.status != "imported",
            )
        )
        if media_request_id is not None and season is not None:
            await self.ensure_scope(
                download_id,
                media_request_id=media_request_id,
                season=season,
                episodes=episodes,
            )

    async def get_by_hash(
        self, torrent_hash: str, *, populate_existing: bool = False
    ) -> DownloadRecord | None:
        """Return the download for ``torrent_hash``, or ``None``.

        ``populate_existing`` mirrors :meth:`list_active` (issue #77): with
        ``expire_on_commit=False`` a plain SELECT lets this session's identity map
        win over the DB row, so a status a DIFFERENT session committed mid-call
        (e.g. a superseding ``mark_failed`` completing the row while the original
        call yields) would be reported stale. ``True`` forces the fresh DB values.
        """
        stmt = select(Download).where(Download.torrent_hash == torrent_hash)
        if populate_existing:
            stmt = stmt.execution_options(populate_existing=True)
        row = (await self._session.execute(stmt)).scalars().first()
        return await self._to_record_with_scopes(row) if row is not None else None

    async def find_active_for_request(
        self, media_request_id: int, *, season: int | None = None
    ) -> DownloadRecord | None:
        # ``Download.season == season`` renders ``IS NULL`` when ``season`` is
        # ``None`` (SQLAlchemy's standard ``== None`` -> ``IS NULL`` translation),
        # so movie callers (``season=None``) keep matching only the NULL-season
        # rows they always create -- identical to the pre-widen behaviour, since a
        # movie never has a non-NULL ``season``. TV callers pass the season being
        # grabbed, scoping the guard to that season only.
        scoped_exists = exists().where(
            DownloadScope.download_id == Download.id,
            DownloadScope.media_request_id == media_request_id,
            DownloadScope.season_number == season,
            DownloadScope.status.in_(_ACTIVE_SCOPE_STATUSES),
        )
        matching_scope_exists = exists().where(
            DownloadScope.download_id == Download.id,
            DownloadScope.media_request_id == media_request_id,
            DownloadScope.season_number == season,
        )
        legacy_scalar_match = (
            (Download.media_request_id == media_request_id)
            & (Download.season == season)
            & ~matching_scope_exists
        )
        stmt = (
            select(Download)
            .where(
                Download.status.notin_(_TERMINAL_DOWNLOAD_STATUSES),
                or_(legacy_scalar_match, scoped_exists),
            )
            .order_by(Download.id)
        )
        row = (await self._session.execute(stmt)).scalars().first()
        return await self._to_record_with_scopes(row) if row is not None else None

    async def find_active_for_request_or_coverage(
        self, media_request_id: int, *, season: int | None = None
    ) -> DownloadRecord | None:
        """Return the active logical or physical owner for a request scope.

        A ride-along season has an active physical-coverage claim but intentionally
        no importable scope, so guards that must avoid work or state changes for an
        in-flight season need both ownership shapes (issue #462).
        """
        active = await self.find_active_for_request(media_request_id, season=season)
        if active is not None:
            return active
        return await self.find_active_coverage_owner(media_request_id, season)

    async def find_active_for_requests(
        self, keys: Sequence[tuple[int, int | None]]
    ) -> frozenset[tuple[int, int | None]]:
        """Batch :meth:`find_active_for_request` membership over MANY
        ``(media_request_id, season)`` keys (issue #138).

        Eviction candidate assembly probed the in-flight guard once PER candidate
        (one ``find_active_for_request`` SELECT per movie/season row); this answers
        the whole pool in exactly TWO queries regardless of pool size, replicating
        :meth:`find_active_for_request`'s per-key match semantics exactly (legacy
        scalar ownership superseded by a scope for the SAME key, or an
        active-status scope) evaluated in Python against the two result sets.

        1. Every non-terminal ``Download`` row that could match ANY key: either
           its legacy scalar ``(media_request_id, season)`` names one of the
           requested ids, or SOME ``DownloadScope`` (any status -- the negation
           check below needs to see a suppressed/terminal scope too, exactly like
           :meth:`find_active_for_request`'s own ``matching_scope_exists``) names
           one. This is a deliberately BROADER net than the final per-key
           decision -- it only bounds which download rows are worth examining.
        2. Every scope those rows carry for the requested ids, read once and
           reused two ways per row: negate a legacy match when a scope for the
           EXACT SAME ``(download, media_request_id, season)`` exists at all
           (regardless of status), and independently register a scoped match
           when the scope's OWN status is active.

        The returned membership is IDENTICAL to calling
        ``find_active_for_request(media_request_id, season=season) is not None``
        for each key one at a time.
        """
        if not keys:
            return frozenset()
        key_set = frozenset(keys)
        # ``request_ids`` bounds two ``IN (...)`` clauses below (and a third against
        # ``download_ids``, sized by however many rows the first query returns). A
        # single disk-pressure sweep's candidate pool is this app's whole "available"
        # movie/season set for one root -- large but, like every other batch reader in
        # this module (``list_for_requests``, ``get_many``), unchunked; SQLite's
        # historical 999-bound-parameter ceiling is a latent limit shared with those,
        # not a regression introduced here. Chunk all of them together if a real
        # library ever grows large enough to hit it.
        request_ids = list({media_request_id for media_request_id, _season in key_set})

        scope_exists_for_requests = exists().where(
            DownloadScope.download_id == Download.id,
            DownloadScope.media_request_id.in_(request_ids),
        )
        stmt = select(Download.id, Download.media_request_id, Download.season).where(
            Download.status.notin_(_TERMINAL_DOWNLOAD_STATUSES),
            or_(Download.media_request_id.in_(request_ids), scope_exists_for_requests),
        )
        rows = (await self._session.execute(stmt)).all()
        if not rows:
            return frozenset()
        download_ids = [row.id for row in rows]

        scope_stmt = select(
            DownloadScope.download_id,
            DownloadScope.media_request_id,
            DownloadScope.season_number,
            DownloadScope.status,
        ).where(
            DownloadScope.download_id.in_(download_ids),
            DownloadScope.media_request_id.in_(request_ids),
        )
        scopes_by_download: dict[int, list[tuple[int | None, int | None, str]]] = {}
        for download_id, scope_request_id, season_number, scope_status in (
            await self._session.execute(scope_stmt)
        ).all():
            scopes_by_download.setdefault(download_id, []).append(
                (scope_request_id, season_number, scope_status)
            )

        active: set[tuple[int, int | None]] = set()
        for row in rows:
            scopes = scopes_by_download.get(row.id, [])
            if row.media_request_id is not None:
                key = (row.media_request_id, row.season)
                matching_scope_exists = any(
                    scope_request_id == row.media_request_id and season_number == row.season
                    for scope_request_id, season_number, _status in scopes
                )
                if key in key_set and not matching_scope_exists:
                    active.add(key)
            for scope_request_id, season_number, scope_status in scopes:
                if scope_status in _ACTIVE_SCOPE_STATUSES and scope_request_id is not None:
                    scoped_key = (scope_request_id, season_number)
                    if scoped_key in key_set:
                        active.add(scoped_key)
        return frozenset(active)

    async def list_active_for_request(self, media_request_id: int) -> list[DownloadRecord]:
        """Every ACTIVE (non-terminal) download for a request, across all seasons.

        The cancel verb (ADR-0014) needs to remove EVERY in-flight torrent a
        request still owns -- a movie has at most one, but a whole-series TV
        request can have several seasons downloading at once. Terminal rows
        (imported/failed/no_acceptable_release) are excluded: they hold no live
        torrent to remove and re-failing them would be dishonest.
        """
        scoped_exists = exists().where(
            DownloadScope.download_id == Download.id,
            DownloadScope.media_request_id == media_request_id,
            DownloadScope.status.in_(_ACTIVE_SCOPE_STATUSES),
        )
        stmt = (
            select(Download)
            .where(
                Download.status.notin_(_TERMINAL_DOWNLOAD_STATUSES),
                or_(Download.media_request_id == media_request_id, scoped_exists),
            )
            .order_by(Download.id)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        scopes = await self._scopes_by_download([row.id for row in rows])
        return [_to_record(row, scopes.get(row.id, [])) for row in rows]

    async def list_active_for_requests(
        self, media_request_ids: list[int]
    ) -> dict[int, list[DownloadRecord]]:
        """Batch active physical downloads by each owning request id.

        ``Download.media_request_id`` is the legacy ownership link, while
        ``DownloadScope.media_request_id`` records every logical TV scope a shared
        physical torrent covers. Read both shapes in one download query, load the
        matching rows' scopes once, and group the resulting physical records by
        caller-supplied request id. Once a scope exists for the scalar owner it is
        authoritative: an imported/failed scope must not be resurrected by the
        stale compatibility link. A physical row is added at most once to each
        request even when both active ownership links name it; this is load-bearing
        for presenting one honest byte-progress value for a multi-scope pack.

        Terminal physical rows carry history, not live transfer progress, and are
        excluded just like :meth:`list_active_for_request`.  The caller controls
        the id set (the requests router supplies only rows visible to the actor),
        so this read never widens request visibility or exposes queue metadata.
        """
        request_ids = list(dict.fromkeys(media_request_ids))
        if not request_ids:
            return {}

        requested_ids = frozenset(request_ids)
        scoped_exists = exists().where(
            DownloadScope.download_id == Download.id,
            DownloadScope.media_request_id.in_(request_ids),
            DownloadScope.status.in_(_ACTIVE_SCOPE_STATUSES),
        )
        scalar_scope_exists = exists().where(
            DownloadScope.download_id == Download.id,
            DownloadScope.media_request_id == Download.media_request_id,
        )
        legacy_scalar_match = Download.media_request_id.in_(request_ids) & ~scalar_scope_exists
        stmt = (
            select(Download)
            .where(
                Download.status.notin_(_TERMINAL_DOWNLOAD_STATUSES),
                or_(legacy_scalar_match, scoped_exists),
            )
            .order_by(Download.id)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        scopes_by_download = await self._scopes_by_download([row.id for row in rows])

        grouped: dict[int, list[DownloadRecord]] = {request_id: [] for request_id in request_ids}
        for row in rows:
            scopes = scopes_by_download.get(row.id, [])
            owners: set[int] = set()
            scalar_has_scope = any(
                scope.media_request_id == row.media_request_id for scope in scopes
            )
            if row.media_request_id in requested_ids and not scalar_has_scope:
                owners.add(row.media_request_id)
            owners.update(
                scope.media_request_id
                for scope in scopes
                if scope.media_request_id in requested_ids
                and scope.status in _ACTIVE_SCOPE_STATUSES
            )
            record = _to_record(row, scopes)
            for owner_id in owners:
                grouped[owner_id].append(record)
        return grouped

    async def find_latest_for_request(
        self, media_request_id: int, *, season: int | None = None
    ) -> DownloadRecord | None:
        """The most recent download for ``(request, season)``, ANY status.

        The report-issue verb (ADR-0014) resolves the CULPRIT release this way:
        the file being reported was placed by the request's imported download, so
        the newest row (by id) for this ``(media_request_id, season)`` is the one
        whose release must be blocklisted and whose torrent must be removed. Unlike
        :meth:`find_active_for_request` this does NOT exclude terminal rows -- the
        imported download is terminal, and it is exactly the one we want. ``season``
        follows the same ``== None`` -> ``IS NULL`` translation as
        :meth:`find_active_for_request` (movies match the NULL-season rows only).
        """
        scoped_exists = exists().where(
            DownloadScope.download_id == Download.id,
            DownloadScope.media_request_id == media_request_id,
            DownloadScope.season_number == season,
        )
        stmt = (
            select(Download)
            .where(
                or_(
                    (Download.media_request_id == media_request_id) & (Download.season == season),
                    scoped_exists,
                ),
            )
            .order_by(Download.id.desc())
            .limit(1)
        )
        row = (await self._session.execute(stmt)).scalars().first()
        return await self._to_record_with_scopes(row) if row is not None else None

    async def find_latest_imported_for_request(
        self, media_request_id: int, *, season: int | None = None
    ) -> DownloadRecord | None:
        """The most recent IMPORTED download for ``(request, season)``, or ``None``.

        Where :meth:`find_latest_for_request` returns the newest row of ANY status,
        this returns the newest row that is actually ``imported`` -- the download that
        OWNS the placed library file (and whose torrent is still hardlink-seeding it).
        The correction verbs need this, not merely the newest attempt (ADR-0014):

        * report-issue must blocklist + remove the torrent that placed the file, not a
          LATER supplementary/failed attempt for the same ``(request, season)`` -- a
          season already ``available`` can be reopened by a supplementary per-episode
          grab that then fails, leaving a newer ``failed`` row over the older
          ``imported`` one; blocklisting/removing that failed row would leave the real
          seed (hardlinking the file) untouched, so the purge frees nothing.
        * cancel's imported-seed probe must detect an older imported torrent that is
          still seeding even when a newer non-imported row exists for the season,
          rather than missing it because the NEWEST row happens not to be imported.

        ``season`` follows the same ``== None`` -> ``IS NULL`` translation as the
        sibling lookups (movies match the NULL-season rows only).
        """
        scoped_exists = exists().where(
            DownloadScope.download_id == Download.id,
            DownloadScope.media_request_id == media_request_id,
            DownloadScope.season_number == season,
            DownloadScope.status == "imported",
        )
        stmt = (
            select(Download)
            .where(
                # Literal (not the P4 ``DownloadState`` enum) -- this layer duplicates
                # the state vocabulary as strings to stay decoupled (see module docstring
                # and ``_TERMINAL_DOWNLOAD_STATUSES``); ``imported`` is that enum's value.
                or_(
                    (Download.status == "imported")
                    & (Download.media_request_id == media_request_id)
                    & (Download.season == season),
                    scoped_exists,
                ),
            )
            .order_by(Download.id.desc())
            .limit(1)
        )
        row = (await self._session.execute(stmt)).scalars().first()
        return await self._to_record_with_scopes(row) if row is not None else None

    async def imported_unscoped_pack_candidates(
        self, media_request_id: int, season: int
    ) -> list[tuple[str | None, datetime]]:
        """``(release_title, added_at)`` for every IMPORTED episode-UNSCOPED
        download touching ``(request, season)``: the scalar shape
        (``downloads.season == season`` with ``episodes_json`` NULL) or a season
        scope of a multi-season download (``download_scopes`` row with NULL
        ``episodes_json`` and scope status ``imported``).

        Episode-unscoped is NECESSARY but not SUFFICIENT pack proof: a pre-#167
        single-episode grab for a season scope was ALSO recorded with
        ``episodes_json`` NULL (issue #230 -- the live apollo shape is a
        ``season=N`` row whose ``release_title`` names a single episode, e.g.
        ``...S04E07...``). The caller corroborates each candidate's
        ``release_title`` via :func:`plex_manager.domain.season_pack.
        classify_release_scope` before trusting it as pack proof + adoption
        cutoff -- this method only narrows on the PERSISTENCE shape.
        """
        # The pack test (``episodes_json is None``) is evaluated PYTHON-side, like
        # every other reader of this column: SQLAlchemy's JSON type stores a
        # Python ``None`` as the JSON literal ``'null'`` (not SQL NULL), so a
        # DB-side ``IS NULL`` silently misses rows written through the ORM.
        scalar_stmt = select(
            Download.release_title, Download.episodes_json, Download.added_at
        ).where(
            Download.media_request_id == media_request_id,
            Download.season == season,
            Download.status == "imported",
        )
        scoped_stmt = (
            select(Download.release_title, DownloadScope.episodes_json, Download.added_at)
            .select_from(DownloadScope)
            .join(Download, Download.id == DownloadScope.download_id)
            .where(
                DownloadScope.media_request_id == media_request_id,
                DownloadScope.season_number == season,
                DownloadScope.status == "imported",
            )
        )
        return [
            (release_title, added_at)
            for stmt in (scalar_stmt, scoped_stmt)
            for release_title, episodes_json, added_at in (await self._session.execute(stmt)).all()
            if episodes_json is None and added_at is not None
        ]

    async def list_active(self, *, populate_existing: bool = False) -> list[DownloadRecord]:
        """Active (non-terminal) downloads as read-model DTOs.

        ``populate_existing`` (issue #77) overwrites already-loaded identity-map
        rows with the freshly-SELECTed DB values instead of letting the identity
        map win. A row that LOST a status compare-and-swap earlier in the SAME
        session — e.g. ``reconcile_and_list`` computed a transition from a stale
        snapshot but a concurrent writer had already advanced the row to another
        NON-terminal status — otherwise keeps its stale in-memory status
        (``expire_on_commit=False``, so the intervening commit does not refresh it,
        and a plain SELECT does not overwrite a loaded instance). The terminal
        post-cycle read then reports a status the DB no longer holds. Refreshing on
        that read closes the honesty gap; the default stays ``False`` so ordinary
        callers keep the cheaper identity-map behaviour.
        """
        stmt = (
            select(Download)
            .where(Download.status.notin_(_TERMINAL_DOWNLOAD_STATUSES))
            .order_by(Download.id)
        )
        if populate_existing:
            stmt = stmt.execution_options(populate_existing=True)
        rows = (await self._session.execute(stmt)).scalars().all()
        scopes = await self._scopes_by_download([row.id for row in rows])
        return [_to_record(row, scopes.get(row.id, [])) for row in rows]

    async def list_active_for_queue(self) -> list[QueueRecord]:
        """Active (non-terminal) downloads enriched for the human-legible queue.

        Queue-specific (issue #134): LEFT OUTER JOINs ``MediaRequest`` to pull in
        ``title``/``poster_url`` without a per-row re-fetch. OUTER (not INNER)
        because ``media_request_id`` is nullable -- ``MediaRequest``'s
        ``ondelete="SET NULL"`` orphans a download whose owning request was
        deleted, and an orphan row must still render (honesty over silence),
        just with both fields ``None``. Deliberately separate from
        :meth:`list_active`, which stays untouched and passive-plain for the
        reconcile loop's domain contract.
        """
        stmt = (
            select(Download, MediaRequest.title, MediaRequest.poster_url)
            .outerjoin(MediaRequest, Download.media_request_id == MediaRequest.id)
            .where(Download.status.notin_(_TERMINAL_DOWNLOAD_STATUSES))
            .order_by(Download.id)
        )
        rows = (await self._session.execute(stmt)).all()
        scopes = await self._scopes_by_download([download.id for download, _title, _poster in rows])
        return [
            QueueRecord(
                **_to_record(download, scopes.get(download.id, [])).model_dump(),
                title=title,
                poster_url=poster_url,
            )
            for download, title, poster_url in rows
        ]

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
        timeout_at: datetime | None = None,
    ) -> DownloadRecord:
        row = Download(
            torrent_hash=torrent_hash,
            status=status,
            media_request_id=media_request_id,
            magnet_link=magnet_link,
            tmdb_id=tmdb_id,
            year=year,
            season=season,
            episodes_json=episodes,
            media_type=MediaType(media_type) if media_type is not None else None,
            release_title=release_title,
            timeout_at=timeout_at,
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        scopes: list[DownloadScopeRecord] = []
        if media_request_id is not None and season is not None:
            scopes.append(
                await self.ensure_scope(
                    row.id,
                    media_request_id=media_request_id,
                    season=season,
                    episodes=episodes,
                )
            )
        return _to_record(row, scopes)

    async def ensure_scope(
        self,
        download_id: int,
        *,
        media_request_id: int | None,
        season: int | None,
        episodes: list[int] | None = None,
    ) -> DownloadScopeRecord:
        """Attach ``(media_request_id, season, episodes)`` to ``download_id``.

        Idempotent for an already-attached equivalent scope. ``episodes=None`` is
        the whole-season sentinel and is distinct from a concrete episode list.
        """
        normalized_episodes = _normalize_episodes(episodes)
        stmt = select(DownloadScope).where(
            DownloadScope.download_id == download_id,
            DownloadScope.media_request_id == media_request_id,
            DownloadScope.season_number == season,
        )
        terminal_match: DownloadScope | None = None
        for row in (await self._session.execute(stmt)).scalars().all():
            if _episodes_equal(row.episodes_json, normalized_episodes):
                if row.status in _ACTIVE_SCOPE_STATUSES:
                    return _to_scope_record(row)
                terminal_match = row

        if terminal_match is not None:
            terminal_match.status = "active"
            terminal_match.completed_at = None
            await self._session.flush()
            await self._session.refresh(terminal_match)
            return _to_scope_record(terminal_match)

        season_request_id: int | None = None
        if media_request_id is not None and season is not None:
            season_request_id = await self._session.scalar(
                select(SeasonRequest.id).where(
                    SeasonRequest.media_request_id == media_request_id,
                    SeasonRequest.season_number == season,
                )
            )
        row = DownloadScope(
            download_id=download_id,
            media_request_id=media_request_id,
            season_request_id=season_request_id,
            season_number=season,
            episodes_json=normalized_episodes,
            scope_key=_scope_key(season, normalized_episodes),
            status="active",
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return _to_scope_record(row)

    async def list_scopes(self, download_id: int) -> list[DownloadScopeRecord]:
        return (await self._scopes_by_download([download_id])).get(download_id, [])

    async def ensure_coverage_claim(
        self,
        download_id: int,
        *,
        media_request_id: int,
        season: int,
    ) -> None:
        """Claim ``(media_request_id, season)`` as physically covered by ``download_id``
        (issue #456).

        Idempotent for a claim this same download already holds -- an already-active
        claim is a no-op, a ``released`` one is re-activated -- so re-grabbing a shared
        pack per season, or resurrecting a terminal row, never duplicates a claim.
        Raises :class:`~sqlalchemy.exc.IntegrityError` (via
        ``uq_download_coverage_claims_active``) when a DIFFERENT active download already
        covers the season: that is the atomic backstop the caller resolves to
        ``AlreadyDownloadingError``, closing the ride-along race durably rather than
        relying on the pre-add read guard alone. Unlike a scope this is season-granular
        (never episodes): a ride-along is always a whole season.
        """
        stmt = select(DownloadCoverageClaim).where(
            DownloadCoverageClaim.download_id == download_id,
            DownloadCoverageClaim.media_request_id == media_request_id,
            DownloadCoverageClaim.season_number == season,
        )
        existing = (await self._session.execute(stmt)).scalars().first()
        if existing is not None:
            if existing.status != _ACTIVE_CLAIM_STATUS:
                existing.status = _ACTIVE_CLAIM_STATUS
                existing.released_at = None
                await self._session.flush()
            return

        self._session.add(
            DownloadCoverageClaim(
                download_id=download_id,
                media_request_id=media_request_id,
                season_number=season,
                status=_ACTIVE_CLAIM_STATUS,
            )
        )
        await self._session.flush()

    async def release_coverage_claims(self, download_id: int) -> None:
        """Release (deactivate) every active physical-coverage claim of ``download_id``.

        Called the instant a download reaches a terminal status -- exactly when its own
        ``uq_downloads_active_request`` slot frees -- so a claim never outlives the
        torrent it guards. A leaked ``active`` claim would permanently block a season
        (north star #1: no failure mode without a correction path), so this is issued
        on the download's terminal-transition choke, not scattered per lifecycle edge.
        """
        await self._session.execute(
            update(DownloadCoverageClaim)
            .where(
                DownloadCoverageClaim.download_id == download_id,
                DownloadCoverageClaim.status == _ACTIVE_CLAIM_STATUS,
            )
            .values(status=_RELEASED_CLAIM_STATUS, released_at=datetime.now(UTC))
        )

    async def release_resolved_target_coverage_claims(self, download_id: int) -> None:
        """Release the coverage claim of every TARGET season this download has fully
        resolved, while a non-terminal sibling keeps the physical row live (#456).

        A TARGET season is one this download persists a :class:`DownloadScope` for; a
        ride-along season carries no scope. This releases a claim only for a season
        that (a) has at least one scope on this download and (b) has NO scope still in
        an active status (``active``/``import_blocked``) -- i.e. the season is settled
        (``imported``/``failed``/``cancelled``/``no_acceptable_release``). It never
        touches a ride-along claim (no scope for that season) or a season with an
        unresolved sibling scope, so those stay guarded until the whole download
        terminates and :meth:`release_coverage_claims` frees them.

        The motivating case is a partially imported multi-season pack: S1 imports
        (scope ``imported``) while S2 stays ``import_blocked`` and the physical row
        stays ``import_blocked`` (non-terminal). ``align_scalar_scope_with_active``
        already re-points the legacy scalar guard off S1 so an S1 replacement/upgrade
        can be grabbed; without this, S1's still-``active`` coverage claim would keep
        rejecting that grab until the unrelated S2 resolves, regressing the documented
        partial-pack behaviour. Idempotent -- a season already released is not
        re-matched by the ``active`` predicate.
        """
        claimed_seasons = set(
            (
                await self._session.execute(
                    select(DownloadCoverageClaim.season_number).where(
                        DownloadCoverageClaim.download_id == download_id,
                        DownloadCoverageClaim.status == _ACTIVE_CLAIM_STATUS,
                        DownloadCoverageClaim.season_number.is_not(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        if not claimed_seasons:
            return
        scope_rows = (
            await self._session.execute(
                select(DownloadScope.season_number, DownloadScope.status).where(
                    DownloadScope.download_id == download_id,
                    DownloadScope.season_number.is_not(None),
                )
            )
        ).all()
        seasons_with_scope: set[int] = set()
        seasons_with_active_scope: set[int] = set()
        for season_number, scope_status in scope_rows:
            if season_number is None:
                continue
            seasons_with_scope.add(season_number)
            if scope_status in _ACTIVE_SCOPE_STATUSES:
                seasons_with_active_scope.add(season_number)
        resolved = {
            season
            for season in claimed_seasons
            if season in seasons_with_scope and season not in seasons_with_active_scope
        }
        if not resolved:
            return
        await self._session.execute(
            update(DownloadCoverageClaim)
            .where(
                DownloadCoverageClaim.download_id == download_id,
                DownloadCoverageClaim.status == _ACTIVE_CLAIM_STATUS,
                DownloadCoverageClaim.season_number.in_(resolved),
            )
            .values(status=_RELEASED_CLAIM_STATUS, released_at=datetime.now(UTC))
        )

    async def find_active_coverage_owner(
        self, media_request_id: int, season: int | None
    ) -> DownloadRecord | None:
        """The NON-TERMINAL download holding an active physical-coverage claim over
        ``(media_request_id, season)``, or ``None`` (issue #456).

        Powers the belt-and-suspenders read side of the ride-along guard: a covered-
        but-untargeted season has no scope, so :meth:`find_active_for_request` cannot
        see a pack's coverage of it -- this can. Joined to ``downloads`` and gated on a
        non-terminal status so a claim that somehow outlived its torrent still cannot
        report a phantom conflict (the ``released`` write already handles the normal
        case). ``season is None`` (a movie) never has a claim, so this returns ``None``.
        """
        if season is None:
            return None
        stmt = (
            select(Download)
            .join(DownloadCoverageClaim, DownloadCoverageClaim.download_id == Download.id)
            .where(
                DownloadCoverageClaim.media_request_id == media_request_id,
                DownloadCoverageClaim.season_number == season,
                DownloadCoverageClaim.status == _ACTIVE_CLAIM_STATUS,
                Download.status.notin_(_TERMINAL_DOWNLOAD_STATUSES),
            )
            .order_by(Download.id)
        )
        row = (await self._session.execute(stmt)).scalars().first()
        return await self._to_record_with_scopes(row) if row is not None else None

    async def align_scalar_scope_with_active(self, download_id: int) -> None:
        """Keep the legacy scalar TV scope on an unresolved logical scope.

        ``downloads.season`` still backs the legacy one-active-per-season index.
        A shared pack can import that scalar season while a sibling scope remains
        ``import_blocked``, leaving the non-terminal physical row falsely claiming
        the imported season's slot. Repoint the compatibility fields to a remaining
        unresolved scope so a replacement for the imported season can be created
        while the sibling remains protected by the same database guard.

        Prefer an unresolved scope for the current scalar season (including a
        different episode subset), then fall back deterministically to the lowest
        season/id. A fully imported download has no unresolved scopes and needs no
        rewrite because its physical status will leave the active index.
        """
        download = await self._session.get(Download, download_id)
        if download is None:
            raise LookupError(f"download {download_id} does not exist")

        stmt = (
            select(DownloadScope)
            .where(
                DownloadScope.download_id == download_id,
                DownloadScope.status.in_(_ACTIVE_SCOPE_STATUSES),
            )
            .order_by(DownloadScope.season_number, DownloadScope.id)
        )
        scopes = (await self._session.execute(stmt)).scalars().all()
        if not scopes:
            return

        target = next(
            (scope for scope in scopes if scope.season_number == download.season),
            scopes[0],
        )
        if download.season == target.season_number and _episodes_equal(
            download.episodes_json, target.episodes_json
        ):
            return

        download.season = target.season_number
        download.episodes_json = target.episodes_json
        await self._session.flush()

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
        release_title: str | None = None,
    ) -> None:
        row = await self._session.get(Download, download_id)
        if row is None:
            raise LookupError(f"download {download_id} does not exist")
        row.status = status
        if progress is not None:
            row.progress = progress
        if seed_ratio is not None:
            row.seed_ratio = seed_ratio
        if media_request_id is not None:
            # Re-own a reused (terminal) row: a fresh grab from a different request
            # must point the row at the CURRENT request, not the stale prior owner.
            row.media_request_id = media_request_id
        if replace_grab_metadata:
            # Rewrite grab metadata UNCONDITIONALLY (not ``is not None`` gates):
            # terminal-row reuse must reflect the CURRENT grab, not stale
            # magnet/title/season/episode/media-type/release-title scope from the
            # previous owner. This also lets a movie reuse clear stale TV scope
            # back to NULL.
            row.magnet_link = magnet_link
            row.tmdb_id = tmdb_id
            row.year = year
            row.season = season
            row.episodes_json = episodes
            row.media_type = MediaType(media_type) if media_type is not None else None
            row.release_title = release_title
        if clear_failed_reason:
            # A terminal row being reused for a fresh grab must not carry a stale
            # failure reason (honesty over silence: a Downloading row claiming a
            # failure is a dishonest state).
            row.failed_reason = None
        elif failed_reason is not None:
            row.failed_reason = failed_reason
        if clear_download_path:
            # A rolled-back placement (scan-failure orphan): drop the breadcrumb so a
            # later retry's _resolve_content can't treat the now-deleted library path
            # as the torrent's content.
            row.download_path = None
        elif download_path is not None:
            row.download_path = download_path
        if clear_first_seen_at:
            # Explicit reset to NULL (a recovered ClientMissing torrent): distinct
            # from first_seen_at=None, which leaves the existing anchor unchanged.
            row.first_seen_at = None
        elif first_seen_at is not None:
            row.first_seen_at = first_seen_at
        await self._session.flush()
        if status in _TERMINAL_DOWNLOAD_STATUSES:
            # The torrent is done -- free its physical-coverage claims in the same
            # transition that frees its ``uq_downloads_active_request`` slot (#456).
            await self.release_coverage_claims(download_id)
        if replace_grab_metadata:
            await self._replace_scope_set(
                download_id,
                media_request_id=media_request_id,
                season=season,
                episodes=episodes,
            )

    async def update_status_if_in(
        self,
        download_id: int,
        status: str,
        allowed_from: frozenset[str],
        *,
        download_path: str | None = None,
        failed_reason: str | None = None,
        clear_download_path: bool = False,
        progress: float | None = None,
        seed_ratio: float | None = None,
        first_seen_at: datetime | None = None,
        clear_first_seen_at: bool = False,
        clear_failed_reason: bool = False,
        media_request_id: int | None = None,
        replace_grab_metadata: bool = False,
        magnet_link: str | None = None,
        tmdb_id: int | None = None,
        year: int | None = None,
        season: int | None = None,
        episodes: list[int] | None = None,
        media_type: str | None = None,
        release_title: str | None = None,
        added_at: datetime | None = None,
        timeout_at: datetime | None = None,
        clear_timeout_at: bool = False,
        retry_count: int | None = None,
        require_failed_reason: str | None | _NoReasonPredicate = NO_REASON_PREDICATE,
    ) -> bool:
        """Compare-and-swap the status: move to ``status`` only if the row's CURRENT
        persisted status is in ``allowed_from``. Returns whether a row was updated.

        ``update_status`` re-reads the row through the session identity map and issues
        an unconditional ``UPDATE ... WHERE id = ?``, so a status another session
        committed during a long async gap (e.g. an operator's mark_failed) would be
        silently overwritten. This issues a single ``UPDATE ... WHERE id = ? AND status
        IN (...)`` so the DATABASE — not stale in-memory state — decides whether the
        move still applies; ``False`` means the row moved out from under the caller and
        the transition must be abandoned, honoring whoever changed it.

        The optional fields mirror :meth:`update_status` so a CONDITIONAL transition
        can carry progress, missing-grace anchors, surfaced reasons, or breadcrumb
        cleanup in the SAME compare-and-swap — never overwriting a row that already
        left ``allowed_from``. ``clear_download_path`` / ``clear_first_seen_at`` /
        ``clear_failed_reason`` take precedence over their corresponding set values.

        ``added_at`` (issue #165 hardening finding): lets a caller re-anchor the
        stall-detection clock when resurrecting a terminal row for a fresh grab
        (``grab_service._reuse_terminal_row``) — without it the reused row would
        keep the ORIGINAL grab's timestamp, so :func:`domain.reconciler.detect_stalls`
        could immediately misjudge the brand-new grab as stalled. ``None`` (the
        default) leaves the column untouched, for every other CAS caller.

        ``timeout_at`` (issue: honest observability deadline, north star: honesty
        over silence) lets a caller stamp the download-phase stall deadline on the
        SAME CAS as the transition; ``None`` (the default) leaves the column
        untouched. ``clear_timeout_at`` (Codex P2: a live raw_state with no
        meaningful deadline — e.g. ``uploading``/``import_pending`` — must NULL
        the column, not merely skip the write) takes precedence over
        ``timeout_at`` and explicitly NULLs it; without a dedicated flag,
        overloading ``None`` to mean BOTH "leave unchanged" and "clear" would
        leave a stale 45m/3h deadline on a row that has already left every
        download phase, misleading the observability column it was added for.

        ``retry_count`` (issue #180) lets a caller re-anchor the probe-outage retry
        count on the SAME CAS as the transition -- ``grab_service._reuse_terminal_row``
        passes ``0`` so a terminal row resurrected for a fresh grab starts its own
        honest count instead of inheriting one from a prior, unrelated life of the
        same torrent-hash row. ``None`` (the default) leaves the column untouched.

        ``require_failed_reason`` (default: no predicate) additionally constrains the
        WHERE to rows whose CURRENT ``failed_reason`` exactly equals the given value
        (``None`` means "must be NULL"). This is queue_service's durable-ownership
        predicate: its nonce-carrying ``failed_reason`` marker identifies WHICH
        actor/call owns a ``failed_pending`` row, so gating a mutation on the exact
        observed/owned marker value makes the ownership decision and the write ONE
        atomic statement — a concurrent restamp changes ``failed_reason`` and this
        statement then matches 0 rows, with no check-then-act window.

        ``synchronize_session="fetch"`` keeps any already-loaded identity-map instance
        consistent with the DB result, so a later read returns the honest post-CAS
        status (and reason / cleared path).
        """
        values: dict[str, object] = {"status": status}
        if clear_download_path:
            values["download_path"] = None
        elif download_path is not None:
            values["download_path"] = download_path
        if clear_failed_reason:
            values["failed_reason"] = None
        elif failed_reason is not None:
            values["failed_reason"] = failed_reason
        if progress is not None:
            values["progress"] = progress
        if seed_ratio is not None:
            values["seed_ratio"] = seed_ratio
        if media_request_id is not None:
            values["media_request_id"] = media_request_id
        if replace_grab_metadata:
            values["magnet_link"] = magnet_link
            values["tmdb_id"] = tmdb_id
            values["year"] = year
            values["season"] = season
            values["episodes_json"] = episodes
            values["media_type"] = MediaType(media_type) if media_type is not None else None
            values["release_title"] = release_title
        if clear_first_seen_at:
            values["first_seen_at"] = None
        elif first_seen_at is not None:
            values["first_seen_at"] = first_seen_at
        if added_at is not None:
            values["added_at"] = added_at
        if clear_timeout_at:
            values["timeout_at"] = None
        elif timeout_at is not None:
            values["timeout_at"] = timeout_at
        if retry_count is not None:
            values["retry_count"] = retry_count
        stmt = (
            update(Download)
            .where(Download.id == download_id, Download.status.in_(allowed_from))
            .values(**values)
            .execution_options(synchronize_session="fetch")
        )
        if not isinstance(require_failed_reason, _NoReasonPredicate):
            # SQLAlchemy renders ``== None`` as ``IS NULL``, so one comparison
            # covers both the exact-marker and the must-be-unset predicates.
            stmt = stmt.where(Download.failed_reason == require_failed_reason)
        # A DML statement yields a ``CursorResult`` carrying ``rowcount`` (the base
        # ``Result`` that ``AsyncSession.execute`` is typed to does not expose it). The
        # cast target is referenced at runtime (not a string) so CodeQL does not read
        # ``CursorResult``/``Any`` as unused imports.
        result = cast(CursorResult[Any], await self._session.execute(stmt))
        updated = result.rowcount == 1
        if updated and status in _TERMINAL_DOWNLOAD_STATUSES:
            # Won the terminal transition -- free the torrent's physical-coverage
            # claims in the same CAS-gated step that frees its active slot (#456).
            await self.release_coverage_claims(download_id)
        if updated and replace_grab_metadata:
            await self._replace_scope_set(
                download_id,
                media_request_id=media_request_id,
                season=season,
                episodes=episodes,
            )
        return updated

    async def increment_retry_count_if_in(
        self, download_id: int, allowed_from: frozenset[str]
    ) -> bool:
        """Compare-and-swap bump of ``retry_count`` by 1 (issue #180).

        Gated on the row's CURRENT persisted status still being in
        ``allowed_from`` -- mirrors :meth:`update_status_if_in`'s CAS
        discipline: a row an operator moved elsewhere (e.g. ``mark_failed``
        committing in a separate session during the caller's async gap) is
        never touched. Returns whether the row was updated; the caller already
        holds the PRE-increment count from its own prior read, so it can
        derive the new count as ``previous + 1`` without a second read.
        """
        stmt = (
            update(Download)
            .where(Download.id == download_id, Download.status.in_(allowed_from))
            .values(retry_count=Download.retry_count + 1)
            .execution_options(synchronize_session="fetch")
        )
        result = cast(CursorResult[Any], await self._session.execute(stmt))
        return result.rowcount == 1

    async def update_scope_status_if_in(
        self,
        scope_id: int,
        status: str,
        allowed_from: frozenset[str],
    ) -> bool:
        """Compare-and-swap ONE :class:`DownloadScope` row's status: move to
        ``status`` only if its CURRENT persisted status is in ``allowed_from``.
        Returns whether a row was updated.

        The per-download :meth:`update_status_if_in` CAS decides whether a WHOLE
        physical torrent's row may transition; this is the same idiom scoped to a
        single logical scope row, needed because a shared multi-season pack can
        carry several independent scopes whose lifecycles diverge (one imports
        while a sibling is still active). A caller that snapshots a DTO's
        ``scopes`` tuple and then wants to act on one of them (e.g.
        ``correction_service._rescue_shared_pack_siblings`` re-arming a sibling
        season) must re-validate against the DATABASE, not the stale snapshot: a
        concurrent import retry can move that exact scope to ``imported`` in the
        gap between the read and the act. Issuing a single ``UPDATE ... WHERE id =
        ? AND status IN (...)`` makes that re-validation and the write one atomic
        statement -- ``False`` means the scope left ``allowed_from`` under the
        caller and the sibling-specific action must be skipped, never forced.

        ``synchronize_session="fetch"`` mirrors :meth:`update_status_if_in` so any
        already-loaded identity-map instance stays consistent with the DB result.
        """
        stmt = (
            update(DownloadScope)
            .where(DownloadScope.id == scope_id, DownloadScope.status.in_(allowed_from))
            .values(status=status)
            .execution_options(synchronize_session="fetch")
        )
        result = cast(CursorResult[Any], await self._session.execute(stmt))
        return result.rowcount == 1

    async def refresh_progress(
        self,
        download_id: int,
        *,
        progress: float | None = None,
        seed_ratio: float | None = None,
        timeout_at: datetime | None = None,
        clear_timeout_at: bool = False,
    ) -> None:
        """Update ONLY live progress / seed_ratio (+ the observability
        ``timeout_at`` deadline) — never status.

        The reconcile loop refreshes progress on rows with no state transition. It must
        NOT rewrite status: an operator's import retry (or the importer) may have
        CAS-claimed the row to ``importing`` between the loop's ``list_active`` snapshot
        and this write, and rewriting the stale snapshot status would clobber that claim
        (defeating the import finalize CAS and stranding the placed file). Touching only
        progress/seed_ratio/timeout_at leaves any concurrent status transition intact.

        ``clear_timeout_at`` (Codex P2, mirrors :meth:`update_status_if_in`) NULLs
        ``timeout_at`` when the caller has determined the live raw_state has no
        meaningful deadline (e.g. a live-tracked row that moved to ``uploading``/
        ``import_pending``) — ``timeout_at=None`` alone means "leave unchanged", so
        without this flag a stale 45m/3h deadline would survive on a row that has
        already left every download phase.
        """
        row = await self._session.get(Download, download_id)
        if row is None:
            return
        if progress is not None:
            row.progress = progress
        if seed_ratio is not None:
            row.seed_ratio = seed_ratio
        if clear_timeout_at:
            row.timeout_at = None
        elif timeout_at is not None:
            row.timeout_at = timeout_at
        await self._session.flush()
