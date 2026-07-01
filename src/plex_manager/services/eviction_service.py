"""Disk-pressure eviction sweep (ADR-0012, Component 3) — the execution side of
:mod:`plex_manager.domain.eviction`'s pure candidate selection.

One sweep (:func:`run_eviction_sweep`) is scoped to ONE configured root and ONE
media kind (movies -> ``movies_root``, TV -> ``tv_root``) — the caller (the
periodic task in ``web/app.py``, or the future manual ``POST /api/v1/ops/evict``
trigger) is expected to call it once per configured root. Per call:

0. Cheap PRE-CHECK for a non-proactive sweep: if the root's used% is already
   below ``threshold_pct``, return ``[]`` immediately WITHOUT assembling any
   candidate — ``select_evictions`` would reject every candidate anyway on this
   exact gate, so there is no reason to have already paid for step 1's Plex
   calls and directory walks first. A proactive sweep has no pressure gate and
   always needs the full set, so this pre-check is skipped for it.
1. Assemble every ``available`` movie / TV-season row into a pure
   :class:`~plex_manager.domain.eviction.EvictionCandidate` — resolving each
   one's watch state FRESH from Plex (:meth:`LibraryPort.watch_state`, always
   uncached — see its docstring on why a stale "watched" would be the wrong
   direction to ever be wrong in) and its on-disk footprint via a best-effort
   directory walk (:func:`_size_bytes`).
2. Hand the candidates to the pure domain: :func:`~plex_manager.domain.eviction.
   select_evictions` (pressure-triggered, target-seeking) normally, or
   :func:`~plex_manager.domain.eviction.rank_eviction_candidates` (every eligible
   candidate, no pressure gate) when ``proactive=True`` — the opt-in
   ``eviction_proactive_enabled`` setting.
3. For each SELECTED candidate: ``fs.delete(library_path)``, flip its status to
   the non-terminal, re-requestable ``evicted`` (per-season for TV, recomputing
   the parent show's rollup), and log to ``download_history`` + (via the
   ordinary ``logging`` capture pipeline, see ``services/log_capture_service.py``)
   ``log_events``. A candidate missing its ``library_path`` breadcrumb, or one
   the filesystem guard refuses, is skipped + logged — NEVER guessed at, never a
   silent no-op, and never lets one bad candidate abort the rest of the sweep.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal

from plex_manager.adapters.filesystem.local import LocalFileSystemError
from plex_manager.domain.disk_usage import used_percent
from plex_manager.domain.eviction import (
    EvictionCandidate,
    rank_eviction_candidates,
    select_evictions,
)
from plex_manager.models import DownloadHistory, DownloadHistoryEvent, RequestStatus
from plex_manager.repositories.downloads import SqlDownloadRepository
from plex_manager.repositories.requests import SqlRequestRepository
from plex_manager.repositories.season_requests import SqlSeasonRequestRepository
from plex_manager.services import season_request_service
from plex_manager.services.health_service import read_disk_usage

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from plex_manager.ports.filesystem import FileSystemPort
    from plex_manager.ports.library import LibraryPort
    from plex_manager.ports.repositories import RequestRecord

__all__ = ["EvictionOutcome", "preview_candidates", "run_eviction_sweep"]

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EvictionOutcome:
    """One candidate the sweep actually evicted — returned to the caller (the
    periodic loop / the manual trigger) for reporting."""

    request_id: int
    media_type: Literal["movie", "tv"]
    title: str
    season: int | None
    library_path: str
    freed_bytes: int | None


@dataclass(frozen=True)
class _MoviePending:
    """Everything :func:`_evict_one` needs to execute a MOVIE eviction, resolved
    once at candidate-assembly time and looked up again (by ``id(candidate)``)
    for whichever candidates the domain selects."""

    media_request_id: int
    tmdb_id: int
    size_bytes: int | None


@dataclass(frozen=True)
class _SeasonPending:
    """The TV counterpart of :class:`_MoviePending`."""

    media_request_id: int
    season_request_id: int
    season_number: int
    tmdb_id: int
    size_bytes: int | None


_Pending = _MoviePending | _SeasonPending


def _size_bytes(path: str) -> int | None:
    """Best-effort on-disk footprint of ``path`` (a file or a directory tree).

    ``None`` on any I/O error (missing path, permission denied) or when ``path``
    is neither a file nor a directory — the caller treats that as "unknown",
    the same honest fallback ``EvictionCandidate.size_percent`` documents for a
    missing breadcrumb (never a fabricated guess). A per-file error while
    walking a directory is skipped (best-effort partial total) rather than
    aborting the whole size lookup — a single unreadable episode file must not
    hide the fact that the OTHER nine are reclaimable.

    Synchronous, real disk I/O (``os.walk``/``os.path.getsize``) — a multi-GB
    library directory tree makes this genuinely slow. Every caller is an
    ``async def`` on the app's single event loop, so this is ALWAYS invoked as
    ``await asyncio.to_thread(_size_bytes, ...)``, never called inline — mirrors
    ``import_service``'s ``asyncio.to_thread``-wrapped copy verbatim (see its
    module docstring).
    """
    try:
        if os.path.isfile(path):
            return os.path.getsize(path)
        if not os.path.isdir(path):
            return None
        total = 0
        for dirpath, _dirnames, filenames in os.walk(path):
            for filename in filenames:
                with contextlib.suppress(OSError):
                    total += os.path.getsize(os.path.join(dirpath, filename))
        return total
    except OSError:
        return None


async def _movie_candidates(
    session: AsyncSession, library: LibraryPort, root_total_bytes: int
) -> list[tuple[EvictionCandidate, _Pending]]:
    """Every ``available`` movie request as an :class:`EvictionCandidate`.

    ``partially_available`` is deliberately NOT queried here: it is a TV-only
    rollup status a plain movie request can never reach (see ``RequestStatus``),
    so ``available`` is the complete "fully imported" set for movies.
    """
    request_repo = SqlRequestRepository(session)
    download_repo = SqlDownloadRepository(session)
    rows = [
        row
        for row in await request_repo.list_by_status(RequestStatus.available.value)
        if row.media_type == "movie"
    ]
    pairs: list[tuple[EvictionCandidate, _Pending]] = []
    for row in rows:
        watch = await library.watch_state(row.tmdb_id, "movie")
        in_flight = (await download_repo.find_active_for_request(row.id, season=None)) is not None
        size_bytes = (
            await asyncio.to_thread(_size_bytes, row.library_path) if row.library_path else None
        )
        size_percent = (
            (size_bytes / root_total_bytes) * 100.0
            if size_bytes is not None and root_total_bytes > 0
            else 0.0
        )
        candidate = EvictionCandidate(
            request_id=row.id,
            media_type="movie",
            title=row.title,
            season=None,
            status=row.status,
            watched=watch.watched,
            last_viewed_at=watch.last_viewed_at,
            keep_forever=row.keep_forever,
            in_flight=in_flight,
            library_path=row.library_path,
            size_percent=size_percent,
        )
        pending = _MoviePending(media_request_id=row.id, tmdb_id=row.tmdb_id, size_bytes=size_bytes)
        pairs.append((candidate, pending))
    return pairs


async def _season_candidates(
    session: AsyncSession, library: LibraryPort, root_total_bytes: int
) -> list[tuple[EvictionCandidate, _Pending]]:
    """Every ``available`` TV season as an :class:`EvictionCandidate`.

    The pin (``keep_forever``) and the title live on the PARENT show
    (``MediaRequest``), never on the season row itself, so pinning a series
    protects every one of its seasons — each parent is fetched once (cached in
    ``parents``) even when a show has several tracked seasons.
    """
    season_repo = SqlSeasonRequestRepository(session)
    request_repo = SqlRequestRepository(session)
    download_repo = SqlDownloadRepository(session)
    rows = await season_repo.list_by_status(RequestStatus.available.value)
    parents: dict[int, RequestRecord] = {}
    pairs: list[tuple[EvictionCandidate, _Pending]] = []
    for row in rows:
        parent = parents.get(row.media_request_id)
        if parent is None:
            fetched = await request_repo.get(row.media_request_id)
            if fetched is None:  # pragma: no cover - the FK guarantees the parent exists
                continue
            parent = fetched
            parents[row.media_request_id] = parent
        watch = await library.watch_state(row.tmdb_id, "tv", season=row.season_number)
        in_flight = (
            await download_repo.find_active_for_request(
                row.media_request_id, season=row.season_number
            )
        ) is not None
        size_bytes = (
            await asyncio.to_thread(_size_bytes, row.library_path) if row.library_path else None
        )
        size_percent = (
            (size_bytes / root_total_bytes) * 100.0
            if size_bytes is not None and root_total_bytes > 0
            else 0.0
        )
        candidate = EvictionCandidate(
            request_id=row.id,
            media_type="tv",
            title=parent.title,
            season=row.season_number,
            status=row.status,
            watched=watch.watched,
            last_viewed_at=watch.last_viewed_at,
            keep_forever=parent.keep_forever,
            in_flight=in_flight,
            library_path=row.library_path,
            size_percent=size_percent,
        )
        pending = _SeasonPending(
            media_request_id=row.media_request_id,
            season_request_id=row.id,
            season_number=row.season_number,
            tmdb_id=row.tmdb_id,
            size_bytes=size_bytes,
        )
        pairs.append((candidate, pending))
    return pairs


async def _evict_one(
    *,
    session: AsyncSession,
    fs: FileSystemPort,
    candidate: EvictionCandidate,
    pending: _Pending,
) -> EvictionOutcome | None:
    """Delete + flip + log ONE selected candidate. ``None`` means it was skipped
    (logged honestly), never a silent success."""
    library_path = candidate.library_path
    if library_path is None:
        # An eligible candidate predating the library_path breadcrumb (ADR-0012):
        # there is nothing to fs.delete() -- never guess a path, skip + log.
        _logger.warning(
            "skipping eviction of %r: no stored library_path breadcrumb",
            candidate.title,
            extra={"request_id": pending.media_request_id, "tmdb_id": pending.tmdb_id},
        )
        return None

    try:
        # A whole directory tree (``shutil.rmtree``) or a large file -- real,
        # synchronous disk I/O -- so this ALWAYS runs off the event loop
        # (mirrors ``import_service``'s ``asyncio.to_thread``-wrapped copy).
        await asyncio.to_thread(fs.delete, library_path)
    except LocalFileSystemError as exc:
        # The root-containment guard refused (a stale/misconfigured breadcrumb
        # pointing outside every currently-configured library root) -- never
        # silently skipped, never mis-deleted.
        _logger.warning(
            "eviction of %r refused by the filesystem guard (%s); skipping",
            candidate.title,
            exc,
            extra={"request_id": pending.media_request_id, "tmdb_id": pending.tmdb_id},
        )
        return None
    except OSError as exc:
        _logger.warning(
            "eviction of %r failed (%s); will retry next sweep",
            candidate.title,
            type(exc).__name__,
            extra={"request_id": pending.media_request_id, "tmdb_id": pending.tmdb_id},
        )
        return None

    if isinstance(pending, _SeasonPending):
        await season_request_service.set_status(
            session,
            media_request_id=pending.media_request_id,
            season_number=pending.season_number,
            status=RequestStatus.evicted.value,
        )
    else:
        await SqlRequestRepository(session).set_status(
            pending.media_request_id, RequestStatus.evicted.value
        )

    season_note = f" season {candidate.season}" if candidate.season is not None else ""
    session.add(
        DownloadHistory(
            tmdb_id=pending.tmdb_id,
            torrent_hash=None,  # not tied to any download -- see DownloadHistoryEvent.evicted
            event_type=DownloadHistoryEvent.evicted,
            source_title=candidate.title,
            message=(
                f"evicted{season_note}: watched, past grace period, "
                f"disk-pressure relief ({library_path})"
            ),
        )
    )
    await session.commit()

    _logger.info(
        "evicted %r%s: watched, past grace period, disk-pressure relief",
        candidate.title,
        season_note,
        extra={"request_id": pending.media_request_id, "tmdb_id": pending.tmdb_id},
    )
    return EvictionOutcome(
        request_id=candidate.request_id,
        media_type=candidate.media_type,
        title=candidate.title,
        season=candidate.season,
        library_path=library_path,
        freed_bytes=pending.size_bytes,
    )


async def preview_candidates(
    *,
    session: AsyncSession,
    library: LibraryPort,
    media_type: Literal["movie", "tv"],
    root_path: str,
    grace_days: int,
) -> list[EvictionCandidate]:
    """Read-only, ranked preview of one root's eviction candidates.

    Backs ``GET /api/v1/ops/disk``'s candidate preview: mirrors
    :func:`run_eviction_sweep`'s candidate-assembly steps (0-2 minus the
    pressure gate) but never deletes anything and never flips a status --
    exactly the "what WOULD a sweep pick" view
    :func:`~plex_manager.domain.eviction.rank_eviction_candidates`'s docstring
    describes. An unreadable ``root_path`` (missing mount, permission denied)
    returns an empty list (logged), the same honest fallback the sweep itself
    uses -- never a crash of the whole health/disk dashboard over one bad root.
    """
    try:
        # ``shutil.disk_usage`` (a ``statvfs`` syscall) can stall on a hung
        # NFS/SMB mount -- offload it, mirroring every other blocking FS
        # primitive in this module (see ``_evict_one``/``_movie_candidates``).
        disk = await asyncio.to_thread(read_disk_usage, root_path)
    except OSError as exc:
        _logger.warning(
            "eviction candidate preview skipped for %s root %s (%s)",
            media_type,
            root_path,
            type(exc).__name__,
        )
        return []

    pairs = (
        await _movie_candidates(session, library, disk.total_bytes)
        if media_type == "movie"
        else await _season_candidates(session, library, disk.total_bytes)
    )
    candidates = [candidate for candidate, _pending in pairs]
    grace_cutoff = datetime.now(UTC) - timedelta(days=grace_days)
    return rank_eviction_candidates(candidates, grace_cutoff)


async def run_eviction_sweep(
    *,
    session: AsyncSession,
    library: LibraryPort,
    fs: FileSystemPort,
    media_type: Literal["movie", "tv"],
    root_path: str,
    threshold_pct: float,
    target_pct: float,
    grace_days: int,
    proactive: bool = False,
) -> list[EvictionOutcome]:
    """One sweep pass for ONE configured root/media-kind.

    Pressure-triggered (default, ``proactive=False``): nothing is evicted below
    ``threshold_pct`` used, even if eligible candidates exist; at/above it,
    stalest-``last_viewed_at``-first candidates are evicted down towards
    ``target_pct`` (:func:`~plex_manager.domain.eviction.select_evictions`).

    Proactive (``proactive=True``, the opt-in ``eviction_proactive_enabled``
    setting): evicts EVERY past-grace, watched, un-pinned, not-in-flight
    candidate regardless of the root's current usage
    (:func:`~plex_manager.domain.eviction.rank_eviction_candidates` — no
    pressure gate, no target-based early stop). A caller running BOTH modes for
    the same root (pressure-triggered, then proactive) sees a naturally shrunk
    candidate set the second time: anything the first pass already evicted no
    longer reads ``available``.

    An unreadable ``root_path`` (missing mount, permission denied) skips the
    WHOLE sweep for this root — logged, never a crash — since there is nothing
    to assess pressure against. One candidate failing never aborts the rest
    (see :func:`_evict_one`); each successful eviction commits independently so
    a mid-sweep crash only loses progress on the one candidate in flight.
    """
    try:
        # Offloaded for the same reason as ``preview_candidates`` above: a
        # hung/unresponsive mount must never freeze the whole event loop.
        disk = await asyncio.to_thread(read_disk_usage, root_path)
    except OSError as exc:
        _logger.warning(
            "eviction sweep skipped for %s root %s (%s)",
            media_type,
            root_path,
            type(exc).__name__,
        )
        return []

    disk_used_pct = used_percent(disk)
    if not proactive and disk_used_pct < threshold_pct:
        # Cheap pre-check, BEFORE assembling candidates: select_evictions applies
        # this exact gate internally and would return [] anyway, but only after
        # every available movie/season already paid for a fresh Plex watch_state
        # call AND a full os.walk size computation (see _movie_candidates /
        # _season_candidates). At the default 30-min interval that is real,
        # repeated cost for a large library on every tick the disk ISN'T under
        # pressure -- the common case. A proactive sweep has no pressure gate, so
        # this check is skipped for it (it always needs the full candidate set).
        return []

    pairs = (
        await _movie_candidates(session, library, disk.total_bytes)
        if media_type == "movie"
        else await _season_candidates(session, library, disk.total_bytes)
    )
    if not pairs:
        return []

    pending_by_id: dict[int, _Pending] = {id(candidate): pending for candidate, pending in pairs}
    candidates = [candidate for candidate, _pending in pairs]
    grace_cutoff = datetime.now(UTC) - timedelta(days=grace_days)

    selected = (
        rank_eviction_candidates(candidates, grace_cutoff)
        if proactive
        else select_evictions(candidates, disk_used_pct, threshold_pct, target_pct, grace_cutoff)
    )

    outcomes: list[EvictionOutcome] = []
    for candidate in selected:
        try:
            outcome = await _evict_one(
                session=session, fs=fs, candidate=candidate, pending=pending_by_id[id(candidate)]
            )
        except Exception:
            # Mirrors import_service.run_import_cycle / run_availability_cycle:
            # one candidate's unexpected failure (e.g. a DB error mid-transition)
            # must never abort the rest of the sweep, nor leave a half-written
            # transaction poisoning the NEXT candidate's commit.
            await session.rollback()
            _logger.exception(
                "eviction of %r failed unexpectedly; will retry next sweep", candidate.title
            )
            continue
        if outcome is not None:
            outcomes.append(outcome)
    return outcomes
