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
3. For each SELECTED candidate, in this exact order (#67): first CLAIM the row
   with an atomic compare-and-swap that flips its status to the non-terminal,
   re-requestable ``evicted`` (per-season for TV, recomputing the parent show's
   rollup) ONLY if it is still ``available`` and un-pinned -- the pin folded into
   the compared predicate so a ``keep_forever`` landing after assembly makes the
   claim match no row. THEN, and only for a winning claim, ``fs.delete(
   library_path)`` and FINALIZE: log to ``download_history`` + (via the ordinary
   ``logging`` capture pipeline, see ``services/log_capture_service.py``)
   ``log_events``, clearing the ``library_path`` breadcrumb in the same commit --
   ``evicted`` + a still-set breadcrumb thereby always means "claimed but not
   finalized", which is what step 0.5's crash recovery keys on. Claiming BEFORE
   deleting is what stops a concurrent pin/keep from ever losing a kept file to a
   delete that had already run; if the delete then fails, the claim is restored
   to ``available`` (and any in-window re-grab it spawned is reconciled away) so
   a failed unlink can never strand an ``evicted`` row over a still-watchable
   file. A candidate missing its ``library_path`` breadcrumb, or one the
   filesystem guard refuses, is skipped + logged — NEVER guessed at, never a
   silent no-op, and never lets one bad candidate abort the rest of the sweep.

Step 0.5 (before the pressure pre-check, after the root's disk stat): recover
claimed-but-not-finalized evictions -- :func:`_resume_interrupted_evictions` --
so a crash between the claim commit and the finalize can never permanently
strand a live file invisible to every later sweep. See :func:`_evict_one`'s
docstring for the full invariant set + lifecycle state table.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal

from plex_manager.domain.disk_usage import used_percent
from plex_manager.domain.eviction import (
    EvictionCandidate,
    pressure_relieved,
    rank_eviction_candidates,
    select_evictions,
)
from plex_manager.models import DownloadHistory, DownloadHistoryEvent, RequestStatus
from plex_manager.repositories.downloads import SqlDownloadRepository
from plex_manager.repositories.requests import SqlRequestRepository
from plex_manager.repositories.season_requests import SqlSeasonRequestRepository
from plex_manager.services import purge_service, season_request_service
from plex_manager.services.health_service import read_disk_usage
from plex_manager.services.library_roots import deepest_containing_root
from plex_manager.services.purge_service import PurgeOutcome

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from plex_manager.ports.filesystem import FileSystemPort
    from plex_manager.ports.library import LibraryPort
    from plex_manager.ports.repositories import RequestRecord

__all__ = [
    "EvictionOutcome",
    "assemble_candidates",
    "preview_candidates",
    "run_eviction_sweep",
]

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

# Request/season statuses at which an in-window RE-REQUEST has not yet grabbed
# anything: nothing was added to the download client, no import ran. These are
# the ONLY statuses the failed-delete restore may reconcile away (cancel / fold
# back) -- see :func:`_restore_after_failed_delete`. ``no_acceptable_release``
# belongs here: it is a parked pre-grab dead-end, and leaving it standing over a
# restored-``available`` file would show (and dedup-block on) a dishonest
# "nothing found" for content that is watchable right now. Anything that DID
# grab (``downloading``/``import_blocked``/``completed``/...) is left alone --
# the reconciler / import dedup owns an in-flight duplicate download.
_PRE_GRAB_STATUSES: frozenset[str] = frozenset(
    {
        RequestStatus.pending.value,
        RequestStatus.searching.value,
        RequestStatus.no_acceptable_release.value,
    }
)

# The statuses the recovery pass's re-armed (breadcrumb-keyed) enumeration
# scans: the pre-grab set PLUS ``cancelled``. A crash-window re-arm can be
# CANCELLED before recovery runs (re-arm -> the same-release re-grab resurrects
# the old imported download row, unblocking cancel's imported-row probe -> user
# cancels -> crash before the finalize), leaving ``cancelled`` + a stale
# breadcrumb -- a shape BOTH enumerations would otherwise miss and that
# ``evicted_seasons`` (rightly cancelled-blind) cannot subtract, so stale Plex
# presence could mint ``available`` over a deleted file. Season-only, like the
# rest of the re-armed shape: a movie cannot reach ``cancelled`` + breadcrumb
# through the eviction lifecycle (its re-grab is a SEPARATE row with no
# breadcrumb, and the claimed ``evicted`` movie row is not cancellable at all);
# the one movie shape that CAN carry it -- report-issue's failed-purge re-arm
# later cancelled -- is a deliberately settled correction whose kept breadcrumb
# is the orphan-reclaim handle, not an interrupted eviction, and is left alone.
_REARMED_RECOVERY_STATUSES: frozenset[str] = _PRE_GRAB_STATUSES | {RequestStatus.cancelled.value}

# Statuses in which a season breadcrumb is still the stale eviction/recovery
# breadcrumb and can be cleared after the file is gone. Imported-content states
# (``completed``/``available``) are deliberately absent: a same-row TV import
# stamps the same deterministic season directory, so path equality alone cannot
# distinguish a stale breadcrumb from a fresh replacement-import breadcrumb.
_STALE_SEASON_BREADCRUMB_CLEAR_STATUSES: frozenset[str] = _REARMED_RECOVERY_STATUSES | {
    RequestStatus.evicted.value,
    RequestStatus.downloading.value,
    RequestStatus.import_blocked.value,
    RequestStatus.failed.value,
}

_STALE_MOVIE_BREADCRUMB_CLEAR_STATUSES: frozenset[str] = frozenset({RequestStatus.evicted.value})

# In-PROCESS sweep serialization latch: :func:`run_eviction_sweep` no-ops (with
# a log line) when another sweep is still running. Exactly two actors ever call
# it -- the periodic tick and the manual ``POST /ops/evict`` button -- and
# overlapping sweeps have ZERO value: both would select from the same candidate
# pool toward the same target, and every cross-sweep race class this module ever
# had to defend against (double-claim, resume-vs-mid-purge) exists ONLY when
# sweeps overlap. Serializing them deletes that permutation class outright; the
# claim CAS in :func:`_evict_one` remains the database-enforced backstop for
# anything outside this process. A plain module bool, not an ``asyncio.Lock``:
# the check-and-set below has no ``await`` between them, so it is atomic on the
# single event loop; a plain mutable holder has no loop binding (asyncio
# primitives cache their first loop, which breaks under per-test event loops);
# and queueing a second sweep would only make it re-scan what the first is
# already relieving. The deployment is one app process over single-writer
# SQLite, so in-process is the honest scope -- stated here rather than assumed
# silently. (A one-slot dict instead of a module bool: mutation needs no
# ``global`` statement, which static scanners misread as an unused global.)
_sweep_latch: dict[str, bool] = {"busy": False}


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


def _owned_by_root(library_path: str | None, root_path: str, all_roots: Sequence[str]) -> bool:
    """Whether ``library_path`` BELONGS to ``root_path``'s sweep: ``root_path`` is
    its DEEPEST containing configured root (see :func:`~plex_manager.services.
    library_roots.deepest_containing_root`).

    Scopes a per-root sweep to content actually OWNED by that root. A sweep runs
    for one ``(media_type, root_path)``; a stale/moved row whose ``library_path`` sits
    under a DIFFERENT configured root (or an old, since-changed root) must not be a
    candidate for THIS root: it would either consume the projected target here without
    being deletable (starving valid candidates for the pressured root), or — worse —
    delete space from the wrong filesystem while THIS root's pressure is measured.

    Nested configured roots (e.g. ``anime_movie_root=/media/movies/anime`` inside
    ``movies_root=/media/movies``) are why plain lexical containment against
    ``root_path`` alone is NOT enough: the parent root's sweep would also match every
    breadcrumb under the nested child — evicting the child mount's content under the
    PARENT's disk pressure, even though the child is its own filesystem with its own
    usage and its own sweep iteration. Deepest-match assignment gives every breadcrumb
    exactly ONE owning sweep. ``all_roots`` is every configured root (``root_path``
    itself is always included below, so a caller-supplied scope can never silently
    orphan the very root being swept). The ``fs.delete`` escape guard is the
    symlink-safe filesystem check; this is the cheaper candidate-selection scope,
    applied BEFORE the per-row Plex/os.walk work. A ``None`` breadcrumb is a SEPARATE
    concern (a row predating the breadcrumb, or not yet imported): callers keep those
    as candidates so ``_evict_one`` skips them with its honest "no breadcrumb" log
    rather than dropping them silently here.
    """
    if library_path is None:
        return False
    scope = tuple(all_roots) if root_path in all_roots else (*all_roots, root_path)
    return deepest_containing_root(library_path, scope) == root_path


async def _movie_candidates(
    session: AsyncSession,
    library: LibraryPort,
    root_total_bytes: int,
    root_path: str,
    all_roots: Sequence[str],
) -> list[tuple[EvictionCandidate, _Pending]]:
    """Every ``available`` movie request OWNED by ``root_path`` as an
    :class:`EvictionCandidate`.

    ``partially_available`` is deliberately NOT queried here: it is a TV-only
    rollup status a plain movie request can never reach (see ``RequestStatus``),
    so ``available`` is the complete "fully imported" set for movies. Rows whose
    ``library_path`` is not owned by ``root_path`` — under a different configured
    root, a NESTED more-specific root, or no root at all — are skipped (see
    :func:`_owned_by_root`).
    """
    request_repo = SqlRequestRepository(session)
    download_repo = SqlDownloadRepository(session)
    rows = [
        row
        for row in await request_repo.list_by_status(RequestStatus.available.value)
        if row.media_type == "movie"
        and (row.library_path is None or _owned_by_root(row.library_path, root_path, all_roots))
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
    session: AsyncSession,
    library: LibraryPort,
    root_total_bytes: int,
    root_path: str,
    all_roots: Sequence[str],
) -> list[tuple[EvictionCandidate, _Pending]]:
    """Every ``available`` TV season OWNED by ``root_path`` as an
    :class:`EvictionCandidate`.

    The pin (``keep_forever``) and the title live on the PARENT show
    (``MediaRequest``), never on the season row itself, so pinning a series
    protects every one of its seasons — each parent is fetched once (cached in
    ``parents``) even when a show has several tracked seasons. Season rows whose
    ``library_path`` is not owned by ``root_path`` (see :func:`_owned_by_root`)
    are skipped.
    """
    season_repo = SqlSeasonRequestRepository(session)
    request_repo = SqlRequestRepository(session)
    download_repo = SqlDownloadRepository(session)
    rows = [
        row
        for row in await season_repo.list_by_status(RequestStatus.available.value)
        if row.library_path is None or _owned_by_root(row.library_path, root_path, all_roots)
    ]
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


async def _still_evictable(session: AsyncSession, pending: _Pending) -> bool:
    """Re-read the pin + status for ``pending`` right now, bypassing this session's
    identity-map staleness (:meth:`SqlRequestRepository.get_fresh` /
    :meth:`SqlSeasonRequestRepository.get_fresh`).

    The TOCTOU close for C7 (a ``keep_forever`` pin landing after candidate
    assembly): candidate assembly (:func:`_movie_candidates`/
    :func:`_season_candidates`) ran several AWAITED Plex/FS calls before the
    caller ever reaches here, during which an operator may have committed a
    fresh ``keep_forever`` pin in a SEPARATE session/transaction.
    ``get_fresh``'s ``populate_existing=True`` forces this session to actually
    see that commit rather than silently re-reading its own stale snapshot (or
    this candidate's own now-stale in-memory fields). This check ALSO covers
    ``in_flight`` (an active download racing in) and an already-evicted row, so
    it is a genuinely useful early filter for C6 (overlapping sweeps) too — but
    it is a plain READ, not a compare-and-swap: two genuinely concurrent sweeps
    can both pass it in their own uncommitted transactions before either
    commits. It is NOT what makes C6/C7 safe; the eviction CLAIM in
    :func:`_evict_one` -- :meth:`SqlRequestRepository.set_status_if_in`
    (``require_unpinned``) / :meth:`SqlSeasonRequestRepository.set_status_if_in`
    (``require_parent_unpinned``), a real, DATABASE-enforced compare-and-swap that
    flips the status BEFORE any delete and folds the pin into the compared
    predicate -- is the actual double-count AND pin guard. This function only
    means fewer wasted claims/deletes reach that point; it uniquely still covers
    ``in_flight`` (an active download), which the status/pin claim does not
    compare.

    Movies: the request itself must still be ``available``, unpinned, and have
    no active download. TV: the pin lives on the PARENT (fetched separately),
    and the SEASON row itself must still be ``available`` with no active
    download for ``(request, season)``. Either side missing entirely (deleted
    out from under the sweep) is honestly ``False`` — nothing left to evict.
    """
    download_repo = SqlDownloadRepository(session)
    if isinstance(pending, _SeasonPending):
        parent = await SqlRequestRepository(session).get_fresh(pending.media_request_id)
        season = await SqlSeasonRequestRepository(session).get_fresh(pending.season_request_id)
        if parent is None or season is None:
            return False
        in_flight = (
            await download_repo.find_active_for_request(
                pending.media_request_id, season=pending.season_number
            )
        ) is not None
        return (
            not parent.keep_forever
            and season.status == RequestStatus.available.value
            and not in_flight
        )

    request = await SqlRequestRepository(session).get_fresh(pending.media_request_id)
    if request is None:
        return False
    in_flight = (
        await download_repo.find_active_for_request(pending.media_request_id, season=None)
    ) is not None
    return (
        not request.keep_forever
        and request.status == RequestStatus.available.value
        and not in_flight
    )


async def _restore_after_failed_delete(session: AsyncSession, pending: _Pending) -> None:
    """Undo a committed eviction CLAIM whose filesystem delete then failed --
    or never ran (a crash resumed by :func:`_resume_interrupted_evictions`) (#67).

    :func:`_evict_one` flips the row to ``evicted`` and COMMITS it before deleting
    (so a genuinely concurrent sweep sees the claim and stands down). When the
    delete then refuses/errors -- or a crash left the claim committed with the
    file still on disk -- the file is still watchable: the row must NOT be left
    saying ``evicted``, which would strand an evicted-status row over a live file
    (a re-request would re-grab content that never actually left). This
    compare-and-swaps the row back ``evicted`` -> ``available`` (recomputing the
    TV parent rollup) and commits, so the next sweep can honestly retry.

    The restore is itself a CAS guarded on the still-``evicted`` precondition:
    if some other writer moved the row on, that is honored rather than
    clobbered. ``tolerate_active_conflict=True`` mirrors the claim: the
    parent-rollup recompute this triggers can collide with a newer active request
    for the same show, and the season-level restore must survive that collision.

    RE-GRAB RECONCILIATION: the committed ``evicted`` claim is exactly what let
    an in-window re-request re-grab this media (``latest_request_evicted`` /
    ``evicted_seasons`` steered it to ``pending`` instead of a stale-Plex
    ``available`` mint). Once this restore commits ``available``, that re-grab's
    reason has EVAPORATED -- the file never left -- and leaving it standing would
    let the app download a duplicate of on-disk content (and, for a movie, leave
    a live ``available`` row AND an active re-grab row coexisting). So, only when
    the restore CAS actually WON:

    * movie -- every OTHER row for the same ``(tmdb_id, media_type)`` still in a
      :data:`_PRE_GRAB_STATUSES` status is CAS-cancelled (per-row
      ``set_status_if_in``, so a row that concurrently advanced is never
      clobbered), each with a ``cancelled`` history row (honesty over silence: a
      user-visible request never just vanishes).
    * tv -- same, per duplicate season row for this ``(tmdb_id, season_number)``
      under OTHER requests (the wholly-evicted re-request shape), recomputing
      each affected parent's rollup.

    When the SEASON restore CAS LOSES, the cause is the OTHER re-request shape:
    a mixed-show re-request re-arms this SAME row (``ensure_seasons``,
    ``evicted`` -> ``pending``). The row then isn't a duplicate to cancel -- it
    IS the season's tracking record and its file never left -- so it is folded
    straight back to ``available`` (CAS from :data:`_PRE_GRAB_STATUSES` only).
    A row that advanced past pre-grab (``downloading``, ...) is left alone in
    every branch: a grab already happened, and the reconciler / import dedup
    own resolving that duplicate download (the import simply re-places the
    file); cancelling underneath it would orphan a live torrent.
    """
    if isinstance(pending, _SeasonPending):
        restored = await season_request_service.set_status_if_in(
            session,
            media_request_id=pending.media_request_id,
            season_request_id=pending.season_request_id,
            status=RequestStatus.available.value,
            allowed_from=frozenset({RequestStatus.evicted.value}),
            tolerate_active_conflict=True,
        )
        if restored:
            await _cancel_redundant_season_regrabs(session, pending)
        else:
            # The row already left 'evicted' -- an in-window re-request re-armed
            # this SAME row (the mixed-show shape). Fold it back to 'available'
            # if it is still pre-grab: the file never left, so re-downloading it
            # would be a duplicate of on-disk content.
            folded = await season_request_service.set_status_if_in(
                session,
                media_request_id=pending.media_request_id,
                season_request_id=pending.season_request_id,
                status=RequestStatus.available.value,
                allowed_from=_PRE_GRAB_STATUSES,
                tolerate_active_conflict=True,
            )
            if folded:
                _logger.info(
                    "folded a re-armed re-request for season %s back to 'available': "
                    "the eviction's delete failed, so the file never left disk",
                    pending.season_number,
                    extra={"request_id": pending.media_request_id, "tmdb_id": pending.tmdb_id},
                )
            else:
                _logger.info(
                    "eviction restore for season %s found the row advanced past "
                    "pre-grab (or otherwise moved on); leaving it to the "
                    "reconciler/import dedup",
                    pending.season_number,
                    extra={"request_id": pending.media_request_id, "tmdb_id": pending.tmdb_id},
                )
    else:
        restored = await SqlRequestRepository(session).set_status_if_in(
            pending.media_request_id,
            RequestStatus.available.value,
            frozenset({RequestStatus.evicted.value}),
        )
        if restored:
            await _cancel_redundant_movie_regrabs(session, pending)
    await session.commit()


async def _cancel_redundant_movie_regrabs(session: AsyncSession, pending: _MoviePending) -> None:
    """Cancel every OTHER pre-grab request row for this movie (see
    :func:`_restore_after_failed_delete`'s re-grab reconciliation). Flush-only;
    the caller owns the commit, so restore + reconciliation land atomically."""
    repo = SqlRequestRepository(session)
    for row in await repo.list_for_media(pending.tmdb_id, "movie", _PRE_GRAB_STATUSES):
        if row.id == pending.media_request_id:
            continue  # the restored row itself (defensive; it is 'available' now)
        cancelled = await repo.set_status_if_in(
            row.id, RequestStatus.cancelled.value, _PRE_GRAB_STATUSES
        )
        if not cancelled:
            continue  # advanced concurrently -- the reconciler/import dedup owns it
        session.add(
            DownloadHistory(
                tmdb_id=pending.tmdb_id,
                torrent_hash=None,
                event_type=DownloadHistoryEvent.cancelled,
                source_title=row.title,
                message=(
                    "re-request cancelled: the eviction that prompted it failed and "
                    "the original copy was restored to 'available' (file never left disk)"
                ),
            )
        )
        _logger.info(
            "cancelled a redundant in-window re-request: the eviction it answered "
            "failed and the original copy is 'available' again",
            extra={"request_id": row.id, "tmdb_id": pending.tmdb_id},
        )


async def _cancel_redundant_season_regrabs(session: AsyncSession, pending: _SeasonPending) -> None:
    """Cancel this season's pre-grab duplicates under OTHER requests (see
    :func:`_restore_after_failed_delete`'s re-grab reconciliation), recomputing
    each affected parent's rollup. Flush-only; the caller owns the commit."""
    season_repo = SqlSeasonRequestRepository(session)
    request_repo = SqlRequestRepository(session)
    siblings = await season_repo.list_sibling_seasons(
        pending.tmdb_id,
        pending.season_number,
        _PRE_GRAB_STATUSES,
        exclude_id=pending.season_request_id,
    )
    for sibling in siblings:
        cancelled = await season_request_service.set_status_if_in(
            session,
            media_request_id=sibling.media_request_id,
            season_request_id=sibling.id,
            status=RequestStatus.cancelled.value,
            allowed_from=_PRE_GRAB_STATUSES,
            tolerate_active_conflict=True,
        )
        if not cancelled:
            continue  # advanced concurrently -- the reconciler/import dedup owns it
        parent = await request_repo.get(sibling.media_request_id)
        session.add(
            DownloadHistory(
                tmdb_id=pending.tmdb_id,
                torrent_hash=None,
                event_type=DownloadHistoryEvent.cancelled,
                source_title=parent.title if parent is not None else None,
                message=(
                    f"re-request cancelled for season {pending.season_number}: the "
                    "eviction that prompted it failed and the original copy was "
                    "restored to 'available' (file never left disk)"
                ),
            )
        )
        _logger.info(
            "cancelled a redundant in-window re-request for season %s: the eviction "
            "it answered failed and the original copy is 'available' again",
            pending.season_number,
            extra={"request_id": sibling.media_request_id, "tmdb_id": pending.tmdb_id},
        )


async def _path_claimed_by_another_row(
    session: AsyncSession, library_path: str, pending: _Pending
) -> bool:
    """Whether any OTHER live row (movie or season) currently claims
    ``library_path`` -- the recovery pass's finalized-vs-interrupted
    discriminator.

    A breadcrumb-bearing row whose path another live row also carries is NOT an
    interrupted eviction to restore: the media was RE-IMPORTED to the same
    canonical path under a newer request (a legacy eviction from before the
    finalize cleared breadcrumbs, or a crash window whose re-grab already
    re-imported in place). Restoring it would create two rows sharing one file,
    and a later sweep evicting either would delete the file out from under the
    other. ``evicted``/``cancelled`` rows do not count as claims (their content
    claim is dead by definition -- which also lets two stale breadcrumb-sharing
    rows converge: the first recovered becomes the single live owner, the second
    then reads as superseded). Both tables are checked regardless of kind: an
    exact path collision across the movie/season tables is unexpected but must
    not slip through on a taxonomy assumption.
    """
    request_repo = SqlRequestRepository(session)
    season_repo = SqlSeasonRequestRepository(session)
    if isinstance(pending, _SeasonPending):
        return await season_repo.other_row_claims_path(
            library_path, exclude_season_request_id=pending.season_request_id
        ) or await request_repo.other_row_claims_path(library_path)
    return await request_repo.other_row_claims_path(
        library_path, exclude_request_id=pending.media_request_id
    ) or await season_repo.other_row_claims_path(library_path)


async def _release_stale_breadcrumb(
    session: AsyncSession,
    pending: _Pending,
    *,
    expected_path: str,
    expected_statuses: frozenset[str],
) -> bool:
    """CAS-clear ``pending``'s breadcrumb and commit; ``False`` (rolled back) when
    a concurrent pass already cleared it -- or when the breadcrumb no longer
    holds ``expected_path`` (a replacement import stamped a FRESH one onto the
    row mid-recovery; that import owns the row now, and wiping its breadcrumb
    would cost the row its eviction/report handle). No history row and no status
    change: releasing a superseded/stale breadcrumb records that nothing was
    evicted NOW (a legacy row's original eviction already wrote its history back
    then; a crash-window row's eviction never actually happened -- its copy was
    simply replaced in place by the re-grab's import)."""
    if isinstance(pending, _SeasonPending):
        cleared = await SqlSeasonRequestRepository(session).clear_library_path_if_set(
            pending.season_request_id,
            expected_path=expected_path,
            expected_statuses=expected_statuses,
        )
    else:
        cleared = await SqlRequestRepository(session).clear_library_path_if_set(
            pending.media_request_id,
            expected_path=expected_path,
            expected_statuses=expected_statuses,
        )
    if not cleared:
        await session.rollback()
        return False
    await session.commit()
    return True


async def _resume_interrupted_evictions(
    *,
    session: AsyncSession,
    library: LibraryPort,
    media_type: Literal["movie", "tv"],
    root_path: str,
    all_roots: Sequence[str],
) -> None:
    """Recover every CLAIMED-BUT-NOT-FINALIZED eviction owned by this root
    (ADR-0012 #67, crash resumability). Keyed on the BREADCRUMB, not only on the
    ``evicted`` status.

    The claim commits ``evicted`` BEFORE the purge, and the finalize clears the
    ``library_path`` breadcrumb (same commit as the history row) AFTER it -- so
    a still-set breadcrumb on a row the claim published is an eviction that
    never finished. That signature has TWO shapes, and both are enumerated,
    because a crash-window re-request can REWRITE the status out from under the
    first one:

    * ``evicted`` + breadcrumb -- the claim as the crash left it. File still on
      disk -> :func:`_restore_after_failed_delete` (back to ``available``, incl.
      the re-grab reconciliation; THIS sweep's candidate assembly then re-decides
      the eviction fresh through the normal claim -> purge path). File gone ->
      finalize now (CAS-clear breadcrumb as the single-winner gate, history row,
      the Plex refresh the crash swallowed).
    * PRE-GRAB-or-CANCELLED + breadcrumb (TV only; every status in
      :data:`_REARMED_RECOVERY_STATUSES`) -- an in-window re-request re-armed the
      claimed season row (``ensure_seasons``, ``evicted`` -> ``pending``) before
      recovery ran, so the ``evicted`` enumeration alone would MISS it and let
      the re-grab download a duplicate of a file that never left. The re-arm
      lands on ``pending``, but auto-grab (or a manual search) can promote it to
      ``searching`` and park it ``no_acceptable_release`` before this sweep
      runs, so ALL THREE pre-grab statuses are enumerated -- a parked
      breadcrumb-bearing season would otherwise show a dishonest "nothing
      found" over a playable file that no sweep could ever reclaim (evicted
      rows are not candidates; parked rows aren't either). Recovery folds the
      row back to ``available`` when the file is present (a per-status-honest
      CAS from EXACTLY the status that was read, so an advance mid-recovery
      loses cleanly), or releases the stale breadcrumb (the re-grab/search is
      then legitimate) when it is gone -- see :func:`_recover_rearmed_season`,
      including the one deliberate tradeoff this breadth buys. A re-arm the
      user then CANCELLED (crash before the finalize) is enumerated too: file
      gone -> the disk-truth flip (``cancelled`` -> ``evicted``, so
      ``evicted_seasons`` -- rightly cancelled-blind -- keeps subtracting);
      file present -> the fold to ``available`` (the aborted re-grab left a
      live file). Movies have no same-row re-arm (a movie re-request always
      creates a NEW row; a movie ``searching``/``cancelled`` + breadcrumb row
      is report-issue's failed-purge redo -- a deliberately settled correction
      whose kept breadcrumb is the orphan-reclaim handle -- and must never be
      touched), so this shape is season-only.

    Before restoring/folding ANY file-present row, :func:`_path_claimed_by_another_row`
    distinguishes interrupted from FINALIZED-BUT-SUPERSEDED: if another live row
    now claims the same path (a re-import under a newer request -- the legacy
    upgraded-install shape), the stale breadcrumb is released instead of
    restoring, so a later sweep can never delete the shared path out from under
    the row that actually owns it.

    Runs at the START of every sweep, BEFORE the pressure pre-check (recovery
    must not wait for pressure) and only after the root's disk stat succeeded
    (an unmounted root must never make its files read as "gone"). Sweeps are
    serialized in-process (:data:`_sweep_latch`), so no eviction can be
    mid-purge in a concurrent sweep while this runs -- every matched row really
    is a leftover. A transient stat error skips the row (never guess "gone" off
    an I/O error); one row's failure never aborts the rest.
    """
    pairs: list[tuple[str, str | None, _Pending]] = []  # (library_path, title, pending)
    rearmed: list[tuple[str, str | None, str, _SeasonPending]] = []
    if media_type == "movie":
        request_repo = SqlRequestRepository(session)
        for row in await request_repo.list_by_status(RequestStatus.evicted.value):
            if (
                row.media_type != "movie"
                or row.library_path is None
                or not _owned_by_root(row.library_path, root_path, all_roots)
            ):
                continue
            pending: _Pending = _MoviePending(
                media_request_id=row.id, tmdb_id=row.tmdb_id, size_bytes=None
            )
            pairs.append((row.library_path, row.title, pending))
    else:
        season_repo = SqlSeasonRequestRepository(session)
        request_repo = SqlRequestRepository(session)
        for season in await season_repo.list_by_status(RequestStatus.evicted.value):
            if season.library_path is None or not _owned_by_root(
                season.library_path, root_path, all_roots
            ):
                continue
            parent = await request_repo.get(season.media_request_id)
            pending = _SeasonPending(
                media_request_id=season.media_request_id,
                season_request_id=season.id,
                season_number=season.season_number,
                tmdb_id=season.tmdb_id,
                size_bytes=None,
            )
            pairs.append((season.library_path, parent.title if parent else None, pending))
        # The re-armed shape: PRE-GRAB-or-CANCELLED + breadcrumb (see the
        # docstring) -- the crash-window re-request already rewrote the status,
        # so the 'evicted' enumeration above cannot see it; auto-grab can have
        # promoted the re-arm past 'pending' (searching, or parked
        # no_acceptable_release) before this sweep ran; and the user can have
        # CANCELLED the re-arm outright (see _REARMED_RECOVERY_STATUSES). The
        # status each row was READ at travels with it: the fold CAS compares
        # against exactly that status, never the whole set.
        for pre_grab_status in sorted(_REARMED_RECOVERY_STATUSES):
            for season in await season_repo.list_by_status(pre_grab_status):
                if season.library_path is None or not _owned_by_root(
                    season.library_path, root_path, all_roots
                ):
                    continue
                parent = await request_repo.get(season.media_request_id)
                rearmed.append(
                    (
                        season.library_path,
                        parent.title if parent else None,
                        pre_grab_status,
                        _SeasonPending(
                            media_request_id=season.media_request_id,
                            season_request_id=season.id,
                            season_number=season.season_number,
                            tmdb_id=season.tmdb_id,
                            size_bytes=None,
                        ),
                    )
                )

    for library_path, title, pending in pairs:
        try:
            await _resume_one(
                session=session,
                library=library,
                media_type=media_type,
                library_path=library_path,
                title=title,
                pending=pending,
            )
        except Exception:
            # One interrupted eviction's recovery failing must never abort the
            # rest (nor poison the next row's transaction) -- same posture as the
            # sweep's per-candidate guard.
            await session.rollback()
            _logger.exception(
                "resuming an interrupted eviction failed unexpectedly; will retry next sweep",
                extra={"request_id": pending.media_request_id, "tmdb_id": pending.tmdb_id},
            )
    for library_path, title, observed_status, season_pending in rearmed:
        try:
            await _recover_rearmed_season(
                session=session,
                library=library,
                library_path=library_path,
                title=title,
                observed_status=observed_status,
                pending=season_pending,
            )
        except Exception:
            await session.rollback()
            _logger.exception(
                "recovering a re-armed interrupted eviction failed unexpectedly; "
                "will retry next sweep",
                extra={
                    "request_id": season_pending.media_request_id,
                    "tmdb_id": season_pending.tmdb_id,
                },
            )


async def _recover_rearmed_season(
    *,
    session: AsyncSession,
    library: LibraryPort,
    library_path: str,
    title: str | None,
    observed_status: str,
    pending: _SeasonPending,
) -> None:
    """Recover ONE re-armed (pre-grab-or-cancelled + breadcrumb) crash-window
    season -- the second enumeration shape of
    :func:`_resume_interrupted_evictions`.

    File present and unclaimed elsewhere -> fold back to ``available``, CAS'd
    from EXACTLY ``observed_status`` (the status the enumeration read), never
    the whole set -- a row that advances mid-recovery (auto-grab promoting
    ``pending`` -> ``searching``, or a grab landing ``downloading``) loses the
    swap cleanly and is retried/left next sweep. The file never left, so the
    re-grab's reason evaporated; a parked row in particular must not keep
    showing "nothing found" over a playable file no sweep can reclaim. For an
    observed ``cancelled`` row this is DISK-TRUTH-OVER-INTENT again, in the
    other direction from the file-gone flip: the cancellation aborted a re-grab
    INTENT, but the interrupted purge never actually deleted anything -- the
    file is live and watchable, so ``available`` is what disk truth reads, and
    it returns the season to a state every subsystem can manage (evictable,
    re-reportable) instead of a cancelled row over a playable file that no
    sweep could ever reclaim.

    DELIBERATE TRADEOFF (stated, not hidden): ``searching``/parked + breadcrumb
    is ambiguous between (a) this crash-window re-arm promoted by auto-grab and
    (b) report-issue's failed-purge redo (``reset_for_research(clear_library_
    path=False)`` keeps the breadcrumb over the REPORTED-BAD file; its inline
    re-search can park it). No stored discriminator separates them, and both
    fold. For shape (b) the fold is still FACTUALLY honest -- the purge failed,
    so the reported file is on disk and in Plex, i.e. genuinely watchable --
    and it returns the row to a state every subsystem can manage (re-reportable,
    evictable), but it does cancel the pending redo: the operator re-reports if
    the bad copy still matters. Folding was chosen over leaving shape (a)
    stranded because (a) leaks disk FOREVER (a parked/evicted-less row is
    invisible to candidate assembly), while (b)'s cost is one repeated, fully
    supported operator action on a rare double-failure (purge failed AND no
    replacement found).

    File present but another live row claims the path -> release the stale
    breadcrumb and leave the row as it is (the re-grab/search is legitimate; its
    import will stamp a fresh breadcrumb). File gone -> the interrupted purge
    actually completed: record the eviction history it never got, release the
    breadcrumb (single-winner CAS), refresh Plex, and leave the status alone --
    a re-grab/search over a truly-gone file is exactly right.
    """
    try:
        await asyncio.to_thread(os.stat, library_path)
        file_present = True
    except (FileNotFoundError, NotADirectoryError):
        file_present = False
    except OSError as exc:
        _logger.warning(
            "cannot stat a re-armed eviction's path (%s); leaving it for the "
            "next sweep rather than guessing",
            type(exc).__name__,
            extra={"request_id": pending.media_request_id, "tmdb_id": pending.tmdb_id},
        )
        return

    if file_present:
        if await _path_claimed_by_another_row(session, library_path, pending):
            if await _release_stale_breadcrumb(
                session,
                pending,
                expected_path=library_path,
                expected_statuses=_REARMED_RECOVERY_STATUSES,
            ):
                _logger.info(
                    "released a stale eviction breadcrumb of %r season %s: a newer "
                    "row owns the same path; the re-request proceeds normally",
                    title,
                    pending.season_number,
                    extra={"request_id": pending.media_request_id, "tmdb_id": pending.tmdb_id},
                )
            return
        folded = await season_request_service.set_status_if_in(
            session,
            media_request_id=pending.media_request_id,
            season_request_id=pending.season_request_id,
            status=RequestStatus.available.value,
            allowed_from=frozenset({observed_status}),
            tolerate_active_conflict=True,
        )
        if folded:
            await session.commit()
            _logger.info(
                "recovered a re-armed re-request of %r season %s: the interrupted "
                "eviction's file never left disk, folded back to 'available'",
                title,
                pending.season_number,
                extra={"request_id": pending.media_request_id, "tmdb_id": pending.tmdb_id},
            )
        else:
            await session.rollback()
            _logger.info(
                "re-armed season %s moved on before recovery (advanced past the "
                "status it was read at); leaving it to auto-grab/the reconciler",
                pending.season_number,
                extra={"request_id": pending.media_request_id, "tmdb_id": pending.tmdb_id},
            )
        return

    # File gone: the interrupted purge completed before the crash, so the
    # re-grab/search is legitimate -- record the eviction it never got to log,
    # release the breadcrumb, refresh Plex, and leave the status alone. The
    # clear is VALUE-predicated on the exact stale path recovery observed: a
    # replacement import can commit between the stat above and this write,
    # stamping a FRESH breadcrumb (and its imported content) onto this very row
    # -- an unconditional clear would wipe that fresh breadcrumb, leaving a
    # playing season with no eviction/report handle. A mismatch means the
    # import owns the row now: leave everything, log, done.
    cleared = await SqlSeasonRequestRepository(session).clear_library_path_if_set(
        pending.season_request_id,
        expected_path=library_path,
        expected_statuses=_STALE_SEASON_BREADCRUMB_CLEAR_STATUSES,
    )
    if not cleared:
        await session.rollback()
        _logger.info(
            "leaving season %s untouched: its breadcrumb no longer matches the stale "
            "path recovery observed (a replacement import re-stamped the row "
            "mid-recovery, or a concurrent pass already released it)",
            pending.season_number,
            extra={"request_id": pending.media_request_id, "tmdb_id": pending.tmdb_id},
        )
        return
    # DISK-TRUTH-OVER-INTENT: if the re-armed row was CANCELLED before this
    # finalize (re-arm -> user cancel -> finalize), the cancellation described a
    # re-grab intent that was aborted -- not what is on disk. The file is
    # GENUINELY gone, and a 'cancelled' row is invisible to ``evicted_seasons``
    # (which rightly ignores cancelled rows), so leaving it would let a
    # re-request mint 'available' off stale Plex over the just-deleted file.
    # Flip it to the state that reflects disk truth -- 'evicted' -- which every
    # downstream guard and the first-class evicted-re-request flow already
    # handle. CAS from {cancelled} only: any other status is the legitimate
    # re-grab/search and stays.
    await season_request_service.set_status_if_in(
        session,
        media_request_id=pending.media_request_id,
        season_request_id=pending.season_request_id,
        status=RequestStatus.evicted.value,
        allowed_from=frozenset({RequestStatus.cancelled.value}),
        tolerate_active_conflict=True,
    )
    session.add(
        DownloadHistory(
            tmdb_id=pending.tmdb_id,
            torrent_hash=None,
            event_type=DownloadHistoryEvent.evicted,
            source_title=title,
            message=(
                f"eviction finalized season {pending.season_number}: recovered after "
                f"an interrupted sweep, the file was already gone and the season was "
                f"re-requested ({library_path})"
            ),
        )
    )
    await session.commit()
    await purge_service.trigger_library_scan(
        library,
        library_path=library_path,
        media_type="tv",
        context="eviction",
        extra={"request_id": pending.media_request_id, "tmdb_id": pending.tmdb_id},
    )
    _logger.info(
        "finalized an interrupted eviction of %r season %s: the file was already "
        "gone; the re-requested season re-grabs normally",
        title,
        pending.season_number,
        extra={"request_id": pending.media_request_id, "tmdb_id": pending.tmdb_id},
    )


async def _resume_one(
    *,
    session: AsyncSession,
    library: LibraryPort,
    media_type: Literal["movie", "tv"],
    library_path: str,
    title: str | None,
    pending: _Pending,
) -> None:
    """Recover ONE claimed-but-not-finalized eviction — see
    :func:`_resume_interrupted_evictions` for the three-way decision."""
    season_note = f" season {pending.season_number}" if isinstance(pending, _SeasonPending) else ""
    try:
        await asyncio.to_thread(os.stat, library_path)
        file_present = True
    except (FileNotFoundError, NotADirectoryError):
        file_present = False
    except OSError as exc:
        # A transient stat failure (permission flap, I/O error, hung submount) is
        # NOT evidence the file is gone -- finalizing off it would orphan a live
        # file with its only breadcrumb erased. Skip honestly; retry next sweep.
        _logger.warning(
            "cannot stat the interrupted eviction's path (%s); leaving it for the "
            "next sweep rather than guessing",
            type(exc).__name__,
            extra={"request_id": pending.media_request_id, "tmdb_id": pending.tmdb_id},
        )
        return

    if file_present:
        if await _path_claimed_by_another_row(session, library_path, pending):
            # FINALIZED-BUT-SUPERSEDED, not interrupted: another live row now
            # claims this exact path (the media was re-imported in place under a
            # newer request -- the legacy upgraded-install shape, or a crash
            # window whose re-grab already completed). Restoring this row to
            # 'available' would put two rows over one file, and a later sweep
            # evicting either would delete the path out from under the actual
            # owner. Release the stale breadcrumb and leave the row 'evicted'.
            if await _release_stale_breadcrumb(
                session,
                pending,
                expected_path=library_path,
                expected_statuses=(
                    _STALE_SEASON_BREADCRUMB_CLEAR_STATUSES
                    if isinstance(pending, _SeasonPending)
                    else _STALE_MOVIE_BREADCRUMB_CLEAR_STATUSES
                ),
            ):
                _logger.info(
                    "released a stale eviction breadcrumb of %r%s: a newer row "
                    "owns the same path (finalized, not interrupted); nothing restored",
                    title,
                    season_note,
                    extra={"request_id": pending.media_request_id, "tmdb_id": pending.tmdb_id},
                )
            return
        # The claim committed but the purge never completed: the file is still
        # watchable, so restore 'available' (+ the re-grab reconciliation) and
        # let THIS sweep's normal candidate assembly re-decide the eviction
        # fresh -- through the standard claim -> purge path, or not at all if
        # the pressure that justified it is gone.
        _logger.info(
            "resuming an interrupted eviction of %r%s: the file is still on disk; "
            "restored to 'available' for a fresh sweep decision",
            title,
            season_note,
            extra={"request_id": pending.media_request_id, "tmdb_id": pending.tmdb_id},
        )
        await _restore_after_failed_delete(session, pending)
        return

    # The purge completed but the finalize never ran (crash after the delete, or
    # a legacy eviction predating breadcrumb-clearing): finalize now. The
    # CAS-clear is the single-winner gate -- only the pass that actually clears
    # the breadcrumb writes the history row, so two concurrent resumes (or a
    # resume racing the sweep that just finalized) never double-record -- and it
    # is VALUE-predicated on the exact stale path observed, so a replacement
    # import re-stamping this row mid-recovery keeps its fresh breadcrumb.
    if isinstance(pending, _SeasonPending):
        cleared = await SqlSeasonRequestRepository(session).clear_library_path_if_set(
            pending.season_request_id,
            expected_path=library_path,
            expected_statuses=_STALE_SEASON_BREADCRUMB_CLEAR_STATUSES,
        )
    else:
        cleared = await SqlRequestRepository(session).clear_library_path_if_set(
            pending.media_request_id,
            expected_path=library_path,
            expected_statuses=_STALE_MOVIE_BREADCRUMB_CLEAR_STATUSES,
        )
    if not cleared:
        # A concurrent pass finalized first, or a replacement import owns the
        # row's breadcrumb now -- honor it either way.
        await session.rollback()
        return
    if isinstance(pending, _SeasonPending):
        # DISK-TRUTH-OVER-INTENT (see _recover_rearmed_season's twin): a season
        # re-armed then CANCELLED between this row's enumeration and now would
        # otherwise finalize as 'cancelled' -- invisible to ``evicted_seasons``,
        # letting a re-request mint 'available' off stale Plex over the
        # just-deleted file. The file is genuinely gone; flip to 'evicted' (CAS
        # from {cancelled} only -- every other status is left exactly as the
        # normal finalize leaves it). Movies never need this: their re-grabs are
        # SEPARATE rows, so the original row stays 'evicted' and the
        # newest-non-cancelled guard already holds (and an 'evicted' movie row
        # itself is not cancellable at all).
        await season_request_service.set_status_if_in(
            session,
            media_request_id=pending.media_request_id,
            season_request_id=pending.season_request_id,
            status=RequestStatus.evicted.value,
            allowed_from=frozenset({RequestStatus.cancelled.value}),
            tolerate_active_conflict=True,
        )
    session.add(
        DownloadHistory(
            tmdb_id=pending.tmdb_id,
            torrent_hash=None,
            event_type=DownloadHistoryEvent.evicted,
            source_title=title,
            message=(
                f"eviction finalized{season_note}: recovered after an interrupted "
                f"sweep, the file was already gone ({library_path})"
            ),
        )
    )
    await session.commit()
    # The Plex refresh the interrupted sweep never got to -- same best-effort
    # posture as the normal finalize (Plex catches up on its next scheduled scan).
    await purge_service.trigger_library_scan(
        library,
        library_path=library_path,
        media_type=media_type,
        context="eviction",
        extra={"request_id": pending.media_request_id, "tmdb_id": pending.tmdb_id},
    )
    _logger.info(
        "finalized an interrupted eviction of %r%s: the file was already gone",
        title,
        season_note,
        extra={"request_id": pending.media_request_id, "tmdb_id": pending.tmdb_id},
    )


async def _evict_one(
    *,
    session: AsyncSession,
    fs: FileSystemPort,
    library: LibraryPort,
    candidate: EvictionCandidate,
    pending: _Pending,
) -> EvictionOutcome | None:
    """Claim (status CAS) + delete + log ONE selected candidate, in that order
    (#67): the atomic claim runs BEFORE the delete, so nothing is deleted until
    the row is won, and a failed delete restores the claim. ``None`` means it was
    skipped (logged honestly), never a silent success.

    The invariant set this ordering + the request-side guard together uphold:

    1. A pinned file is NEVER deleted -- the claim CAS folds the ``keep_forever``
       pin into its predicate (``require_unpinned`` / ``require_parent_unpinned``)
       and runs before any ``fs.delete``, so a pin landing after candidate
       assembly makes the claim match zero rows.
    2. A failed/refused delete NEVER strands a terminal-status row over a live
       file -- ``_restore_after_failed_delete`` compare-and-swaps the row back
       ``evicted`` -> ``available`` (recomputing the TV parent rollup).
    3. Concurrent sweep ticks NEVER double-process -- sweeps are serialized
       in-process (:data:`_sweep_latch`: a second invocation no-ops with a
       log), and the claim CAS (only the winning ``rowcount == 1`` proceeds to
       delete/history) remains the database-enforced backstop for anything
       outside this process.
    4. A re-request during the delete window NEVER produces an ``available`` row
       (movie) / ``available`` season (TV) whose file the sweep then removes. The
       committed ``evicted`` status is published BEFORE the delete (needed for #3),
       which is exactly the window the request side must not trust Plex over: the
       in-library short-circuit consults ``RequestRepository.latest_request_evicted``
       / ``SeasonRequestRepository.evicted_seasons`` -- both keyed on the newest
       NON-``cancelled`` row, so cancelling an in-window re-grab cannot reset the
       guard mid-window -- and re-grabs (``pending``) rather than minting
       availability off a STALE 'present' reading (see
       ``request_service.create_request`` / ``season_request_service.ensure_seasons``).
    5. A crash mid-eviction NEVER strands the claim -- recovery is keyed on the
       BREADCRUMB (the finalize clears it in the same commit as the history row),
       covering both the ``evicted``+breadcrumb shape AND the season re-armed to
       ``pending``+breadcrumb by a crash-window re-request; and a breadcrumb
       whose path another live row now claims (a re-import -- the legacy
       upgraded-install shape) is RELEASED, never restored over the actual
       owner's file (:func:`_resume_interrupted_evictions`).
    6. A restore (failed delete OR resumed crash) NEVER leaves a redundant
       in-window re-grab standing -- the file never left, so its reason
       evaporated: pre-grab re-requests are CAS-cancelled / folded back to
       ``available``; anything that already grabbed is left to the
       reconciler/import dedup (see :func:`_restore_after_failed_delete`). The
       REVERSE race -- the cancel CAS winning against a row whose grab is
       mid-``qbt.add`` -- is closed on the GRAB side: ``grab_service``'s
       post-add status move is itself a CAS that refuses a cancelled row,
       removes the just-added torrent, and raises the honest
       ``RequestNotActiveError`` (see ``_GRABBABLE_*_STATUS_VALUES`` there).

    Full lifecycle state table, from the moment the claim COMMITS (row
    ``evicted``, breadcrumb set, file on disk); every permutation of
    [crash | re-request | cancel | purge-fail | purge-ok] lands in one row:

    ========================================  =======================================
    event after the claim commit              end state
    ========================================  =======================================
    purge ok                                  ``evicted``, breadcrumb CLEARED,
                                              history row, Plex refreshed (finalized)
    purge refused / error                     restored ``available`` (+ TV rollup);
                                              breadcrumb kept; retried next sweep
    crash, file still present                 resumed next sweep: restored
                                              ``available``, re-decided fresh
    crash, file already purged                resumed next sweep: finalized
                                              (breadcrumb cleared, history, refresh)
    re-request in-window                      lands ``pending`` (invariant #4), the
                                              old row's outcome per the rows above
    re-request in-window, then purge fails    old row ``available``; the pre-grab
    / crash-with-file                         re-grab CANCELLED (movie / sibling
                                              season) or folded back ``available``
                                              (same-row TV re-arm) -- invariant #6
    re-request advanced to a grab, then       old row ``available``; the in-flight
    purge fails                               download is LEFT (reconciler/import
                                              dedup re-places the file in place)
    re-request in-window, user CANCELS it,    guards ignore ``cancelled`` rows, so
    another re-request follows                it still lands ``pending`` (never a
                                              stale-Plex ``available`` mint)
    crash + season re-armed to ANY pre-grab   recovered next sweep off the
    status, incl. auto-grab promoting it to   BREADCRUMB: folded back ``available``
    searching / parking it (file present)     via a per-status CAS (file never left)
    crash + season re-armed, any pre-grab     breadcrumb released + the missing
    status (file gone)                        history/Plex refresh; the re-grab
                                              or parked search proceeds (legitimate)
    cancel (restore reconciliation or user)   closed grab-side: the post-add CAS
    lands while a grab's qbt.add is in        loses on the cancelled row, the
    flight                                    just-added torrent is removed,
                                              RequestNotActiveError raised
    same-row re-arm CANCELLED while the       the finalize flips cancelled ->
    purge deletes / before recovery's         evicted (disk truth over intent:
    file-gone finalize                        the file IS gone; the cancel only
                                              aborted the re-grab), so the guards
                                              keep subtracting the season and a
                                              re-request mints ``pending``
    file present but ANOTHER live row now     finalized-but-superseded (legacy /
    claims the same path                      completed re-import): breadcrumb
                                              released, NOTHING restored -- the
                                              newer row keeps sole path ownership
    ========================================  =======================================
    """
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

    season_note = f" season {candidate.season}" if candidate.season is not None else ""

    # Cheap EARLY filter (see _still_evictable's docstring): closes the obvious
    # TOCTOU cases -- a keep_forever pin, an active download racing in, or an
    # already-evicted row -- with an honest log BEFORE paying for the claim +
    # delete. It is a plain read, NOT the authority: the claim CAS below is what
    # actually makes the pin/status decision race-safe. It uniquely still covers
    # ``in_flight`` (an active download), which the status/pin claim does not
    # compare.
    if not await _still_evictable(session, pending):
        _logger.info(
            "skipping eviction of %r%s: now pinned/keep_forever, already evicted, "
            "or no longer an eviction candidate (re-checked immediately before the claim)",
            candidate.title,
            season_note,
            extra={"request_id": pending.media_request_id, "tmdb_id": pending.tmdb_id},
        )
        return None

    # THE eviction CLAIM (#67, ADR-0012 C6/C7): a real, database-enforced
    # compare-and-swap that flips the row to ``evicted`` and runs BEFORE any
    # filesystem delete -- a genuine REORDER of the old "delete, then flip" flow.
    # Nothing is deleted until this claim has atomically WON the row. The pin is
    # FOLDED into the compared predicate (``keep_forever = false`` for a movie; a
    # parent-pin subquery for a TV season, via ``require_parent_unpinned``), so a
    # ``keep_forever`` pin -- or a concurrent status transition -- that commits in
    # the window between candidate assembly and here makes the UPDATE match zero
    # rows: the DATABASE itself refuses to delete a freshly-pinned/moved title,
    # not a read-then-act check a racing writer could slip past. Only the winning
    # claim (``rowcount == 1``) is allowed to touch the filesystem; a loser skips
    # entirely (never deleting a file the DB still says is kept/available), which
    # is ALSO the C6 double-count guard: two concurrent sweeps cannot both claim
    # the same row.
    if isinstance(pending, _SeasonPending):
        # tolerate_active_conflict=True: an OLD, already-settled parent (rollup
        # 'available') can legitimately coexist with a NEWER active request for
        # the same show (see season_request_service._recompute_parent's
        # docstring) -- evicting one of the old parent's remaining seasons folds
        # its rollup back to the active 'partially_available', which can collide
        # with that newer row's slot in uq_media_requests_active. The season CAS
        # (+ the history row/commit after the delete) is the source of truth for
        # "the file is gone" and MUST survive that collision; only the coarser
        # parent-rollup write is allowed to fail softly.
        claimed = await season_request_service.set_status_if_in(
            session,
            media_request_id=pending.media_request_id,
            season_request_id=pending.season_request_id,
            status=RequestStatus.evicted.value,
            allowed_from=frozenset({RequestStatus.available.value}),
            require_parent_unpinned=True,
            tolerate_active_conflict=True,
        )
    else:
        claimed = await SqlRequestRepository(session).set_status_if_in(
            pending.media_request_id,
            RequestStatus.evicted.value,
            frozenset({RequestStatus.available.value}),
            require_unpinned=True,
        )

    if not claimed:
        # The claim matched no row: a keep_forever pin landed, the row already
        # left 'available' (a concurrent sweep claimed it first), or it vanished.
        # Nothing has been deleted -- honor it, never overwrite it.
        await session.rollback()
        _logger.info(
            "did not claim %r%s for eviction: now pinned/keep_forever, already "
            "evicted, or a concurrent writer moved it out of 'available' first "
            "(the pre-delete compare-and-swap matched no row); nothing deleted",
            candidate.title,
            season_note,
            extra={"request_id": pending.media_request_id, "tmdb_id": pending.tmdb_id},
        )
        return None

    # Persist the claim BEFORE deleting. The committed 'evicted' status is exactly
    # what makes a genuinely concurrent sweep's own claim above match zero rows
    # (it sees the committed 'evicted' and stands down, rather than racing us to a
    # second delete of the same file). A crash between here and the finalize below
    # leaves an 'evicted' row whose ``library_path`` breadcrumb is STILL SET --
    # exactly the "claimed but not finalized" signature the next sweep's
    # :func:`_resume_interrupted_evictions` picks up: it restores the row to
    # 'available' if the file is still on disk (so the eviction is re-decided
    # fresh), or finalizes the eviction if the purge had already completed. The
    # row is never stranded invisible to every sweep (evicted rows are not
    # candidates) over a live file that keeps holding the pressured disk.
    #
    # This same pre-delete publish opens a window where the file is still on disk
    # (Plex still lists it, until the post-delete refresh below) yet the row reads
    # 'evicted' (invisible to find_active / find_in_library). A re-request landing
    # here must NOT trust Plex's stale 'present' and mint an 'available' row over
    # the doomed file -- the request side closes that window by consulting
    # ``latest_request_evicted`` / ``evicted_seasons`` and re-grabbing instead (see
    # invariant #4 above; ``request_service`` / ``season_request_service``).
    await session.commit()

    # Hardlink-aware reclaimable-bytes measurement + the root-guarded delete are
    # both done by the shared ``purge_service.purge_library_path`` primitive
    # (ADR-0014, "reuse don't duplicate") -- same accounting-before-delete order
    # (a file's link count is only readable while it still exists, R4-6, ADR-0012),
    # same containment guard, same idempotent already-gone no-op. Eviction keeps
    # its OWN log message + logger for each outcome (the primitive classifies, the
    # caller logs).
    purge_held = False
    purge = await purge_service.purge_library_path(fs, library_path, hold_purge_registration=True)
    if purge.outcome is PurgeOutcome.deleted:
        purge_held = True
    if purge.outcome is not PurgeOutcome.deleted:
        if purge.outcome is PurgeOutcome.deferred:
            _logger.info(
                "eviction of %r%s deferred because an import is placing into the "
                "same path (%s); leaving the eviction claim for recovery after "
                "the import settles",
                candidate.title,
                season_note,
                purge.detail,
                extra={"request_id": pending.media_request_id, "tmdb_id": pending.tmdb_id},
            )
            return None
        # The delete refused (a stale/misconfigured breadcrumb pointing outside
        # every currently-configured library root) or errored (permission/I/O).
        # The file is STILL on disk and still watchable -- so RESTORE the row to
        # 'available' (#67): a failed unlink must never strand an 'evicted' status
        # over a live file (a re-request would then re-grab content that never
        # left). Never silently skipped, never mis-deleted; a later sweep retries.
        await _restore_after_failed_delete(session, pending)
        if purge.outcome is PurgeOutcome.refused:
            _logger.warning(
                "eviction of %r%s refused by the filesystem guard (%s); "
                "restored to 'available' (nothing deleted)",
                candidate.title,
                season_note,
                purge.detail,
                extra={"request_id": pending.media_request_id, "tmdb_id": pending.tmdb_id},
            )
        else:
            _logger.warning(
                "eviction of %r%s failed (%s); restored to 'available', will retry next sweep",
                candidate.title,
                season_note,
                purge.detail,
                extra={"request_id": pending.media_request_id, "tmdb_id": pending.tmdb_id},
            )
        return None
    freed_bytes = purge.freed_bytes

    # FINALIZE: the claim already flipped + committed the status; record the
    # history row for the now-completed delete AND clear the ``library_path``
    # breadcrumb in the SAME commit. The cleared breadcrumb is what marks this
    # eviction FINALIZED: 'evicted' + a non-NULL breadcrumb always means "claimed
    # but not finalized" (a crash window), which is exactly the signature
    # :func:`_resume_interrupted_evictions` keys on -- so a finalized eviction
    # must never keep looking like an interrupted one.
    try:
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
        if isinstance(pending, _SeasonPending):
            # The clear is value-predicated on the path this sweep just purged: if a
            # replacement import somehow re-stamped a FRESH breadcrumb mid-purge,
            # that import owns the row and its breadcrumb survives.
            await SqlSeasonRequestRepository(session).clear_library_path_if_set(
                pending.season_request_id,
                expected_path=library_path,
                expected_statuses=_STALE_SEASON_BREADCRUMB_CLEAR_STATUSES,
            )
            # DISK-TRUTH-OVER-INTENT: the claim committed 'evicted', but an
            # in-window re-request can re-arm this SAME row to 'pending' and the
            # user can CANCEL it, all while the purge above was deleting -- leaving
            # the row 'cancelled' at this finalize. A cancellation describes an
            # aborted re-grab INTENT, not what is on disk: the file is genuinely
            # gone now, and a 'cancelled' row is invisible to ``evicted_seasons``
            # (which rightly ignores cancelled rows), so leaving it would let the
            # next re-request mint 'available' off stale Plex over the just-deleted
            # file. Flip it back to the disk truth -- 'evicted' (CAS from
            # {cancelled} only; a re-arm still in flight keeps its pre-grab status
            # and is the recovery pass's/restore's business, exactly as before).
            # Re-requesting an evicted season is already a first-class path, so
            # every downstream flow reads correctly. Movies never need this flip:
            # a movie re-grab is a SEPARATE row, so cancelling it leaves the
            # ORIGINAL row 'evicted' and the newest-non-cancelled guard already
            # holds -- and the claimed movie row itself ('evicted') is not even
            # cancellable (outside CANCELLABLE_REQUEST_STATUS_VALUES).
            await season_request_service.set_status_if_in(
                session,
                media_request_id=pending.media_request_id,
                season_request_id=pending.season_request_id,
                status=RequestStatus.evicted.value,
                allowed_from=frozenset({RequestStatus.cancelled.value}),
                tolerate_active_conflict=True,
            )
        else:
            await SqlRequestRepository(session).clear_library_path_if_set(
                pending.media_request_id,
                expected_path=library_path,
                expected_statuses=_STALE_MOVIE_BREADCRUMB_CLEAR_STATUSES,
            )
        await session.commit()
    finally:
        if purge_held:
            purge_service.end_purge(library_path)

    # Tell Plex the media is gone, so a subsequent "Request again" sees it as ABSENT
    # (a fresh pending request that re-grabs), not a stale in-library 'available'. A
    # Plex install keeps a removed item in metadata until a library refresh, and
    # create_request's is_available / present_seasons (use_cache=False) would
    # otherwise trust that stale item and record the re-request as 'available' -> the
    # deleted files are never re-fetched. Best-effort + symmetric with the import
    # pipeline's post-place scan (shared ``purge_service.trigger_library_scan``,
    # ADR-0014): the eviction itself already committed, so a Plex outage here is
    # logged (Plex will catch up on its next scheduled scan), never a failure that
    # undoes a completed eviction.
    await purge_service.trigger_library_scan(
        library,
        library_path=library_path,
        media_type=candidate.media_type,
        context="eviction",
        extra={"request_id": pending.media_request_id, "tmdb_id": pending.tmdb_id},
    )

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
        freed_bytes=freed_bytes,
    )


async def assemble_candidates(
    *,
    session: AsyncSession,
    library: LibraryPort,
    media_type: Literal["movie", "tv"],
    root_path: str,
    root_total_bytes: int,
    all_roots: Sequence[str] | None = None,
) -> list[EvictionCandidate]:
    """Assemble every ``available`` movie / TV-season under ``root_path`` into a
    RAW :class:`~plex_manager.domain.eviction.EvictionCandidate` list -- fresh
    Plex watch state + a walked on-disk size per row -- with NO grace or
    eligibility filtering applied. The caller decides what to filter.

    This is the raw superset behind BOTH :func:`preview_candidates` (which simply
    ranks it) and the retention-telemetry sweep (which needs the ranked /
    would-evict subsets AND every row with any recorded view -- including a
    started-but-unfinished season the eligibility filter drops -- from one read).
    Read-only: never deletes, never flips a status, never writes history.

    ``root_total_bytes`` is threaded in (rather than read here) so each
    candidate's ``size_percent`` is measured against the right root capacity from
    the SAME :func:`~plex_manager.services.health_service.read_disk_usage` the
    caller already performed for its own pressure/skip decision -- one disk-usage
    syscall per sweep, not two. A caller that has not read it can pass any
    consistent total; ``0`` yields ``size_percent=0.0`` for every candidate (an
    honest "unknown share", never a fabricated guess).

    ``all_roots`` is EVERY configured library root, so nested-root ownership can
    be assigned to the most specific root (see :func:`_owned_by_root`) -- a
    breadcrumb under a nested child root is NEVER a candidate for the parent's
    sweep. ``None`` (the single-root default, for tests / a caller with one
    root) scopes against ``root_path`` alone, which is exactly the pre-nesting
    behavior; every production caller (the periodic tick, the manual evict
    trigger, the disk preview, retention telemetry) passes the full set.
    """
    scope: Sequence[str] = all_roots if all_roots is not None else (root_path,)
    pairs = (
        await _movie_candidates(session, library, root_total_bytes, root_path, scope)
        if media_type == "movie"
        else await _season_candidates(session, library, root_total_bytes, root_path, scope)
    )
    return [candidate for candidate, _pending in pairs]


async def preview_candidates(
    *,
    session: AsyncSession,
    library: LibraryPort,
    media_type: Literal["movie", "tv"],
    root_path: str,
    grace_days: int,
    all_roots: Sequence[str] | None = None,
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

    candidates = await assemble_candidates(
        session=session,
        library=library,
        media_type=media_type,
        root_path=root_path,
        root_total_bytes=disk.total_bytes,
        all_roots=all_roots,
    )
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
    all_roots: Sequence[str] | None = None,
) -> list[EvictionOutcome]:
    """One sweep pass for ONE configured root/media-kind.

    Pressure-triggered (default, ``proactive=False``): nothing is evicted below
    ``threshold_pct`` used, even if eligible candidates exist; at/above it,
    stalest-``last_viewed_at``-first candidates are evicted down towards
    ``target_pct`` (:func:`~plex_manager.domain.eviction.select_evictions`).
    That initial selection is only an ESTIMATE (built from each candidate's
    walked-on-disk size, before anything is actually deleted); if the ACTUAL
    (hardlink-aware, see :meth:`~plex_manager.ports.filesystem.FileSystemPort.
    reclaimable_bytes`) bytes freed so far fall short of what the estimate
    projected — same-filesystem hardlinked imports commonly reclaim far less
    than their nominal size — this keeps drawing MORE candidates, in the SAME
    stalest-first order, from beyond that initial cut until the real, running
    freed total actually closes the gap to ``target_pct`` or every eligible
    candidate is exhausted (R4-6, ADR-0012).

    Proactive (``proactive=True``, the opt-in ``eviction_proactive_enabled``
    setting): evicts EVERY past-grace, watched, un-pinned, not-in-flight
    candidate regardless of the root's current usage
    (:func:`~plex_manager.domain.eviction.rank_eviction_candidates` — no
    pressure gate, no target-based early stop, so there is nothing to "extend"
    beyond). A caller running BOTH modes for the same root (pressure-triggered,
    then proactive) sees a naturally shrunk candidate set the second time:
    anything the first pass already evicted no longer reads ``available``.

    An unreadable ``root_path`` (missing mount, permission denied) skips the
    WHOLE sweep for this root — logged, never a crash — since there is nothing
    to assess pressure against. One candidate failing never aborts the rest
    (see :func:`_evict_one`); each successful eviction commits independently so
    a mid-sweep crash only loses progress on the one candidate in flight — and
    even THAT candidate is recovered by the next sweep's
    :func:`_resume_interrupted_evictions` pass (step 0.5), which runs before the
    pressure pre-check so recovery never waits for disk pressure.

    SERIALIZED in-process (:data:`_sweep_latch`): only one sweep runs at a
    time. A second invocation while one is in flight — the manual
    ``POST /ops/evict`` button landing mid-tick, or vice versa — no-ops with a
    log line and returns ``[]``: overlapping sweeps would only re-select from
    the same candidate pool toward the same target, and every cross-sweep race
    class (double-claim, recovery racing a mid-purge claim) exists ONLY when
    they overlap. The skipped invocation's work is not lost — the in-flight
    sweep is already doing it, and the periodic tick retries every interval.
    """
    if _sweep_latch["busy"]:
        _logger.info(
            "eviction sweep skipped for %s root %s: another sweep is already in "
            "progress (sweeps are serialized; the running one is doing this work)",
            media_type,
            root_path,
        )
        return []
    _sweep_latch["busy"] = True
    try:
        return await _run_sweep(
            session=session,
            library=library,
            fs=fs,
            media_type=media_type,
            root_path=root_path,
            threshold_pct=threshold_pct,
            target_pct=target_pct,
            grace_days=grace_days,
            proactive=proactive,
            all_roots=all_roots,
        )
    finally:
        _sweep_latch["busy"] = False


async def _run_sweep(
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
    all_roots: Sequence[str] | None = None,
) -> list[EvictionOutcome]:
    """The sweep body, entered only under :func:`run_eviction_sweep`'s
    serialization latch — see the public docstring."""
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

    # Nested-root ownership scope: see ``assemble_candidates``'s ``all_roots``
    # docstring — a breadcrumb owned by a nested more-specific root is never a
    # candidate for this (parent) root's pressure. Computed BEFORE the pressure
    # pre-check because the crash-recovery pass below needs it too.
    scope: Sequence[str] = all_roots if all_roots is not None else (root_path,)

    # Crash recovery FIRST, and deliberately BEFORE the pressure pre-check: a
    # claimed-but-not-finalized eviction (see _resume_interrupted_evictions) is
    # invisible to candidate assembly (its status is 'evicted', not 'available'),
    # so no amount of ordinary sweeping would ever retry its live file -- and its
    # recovery must not wait for disk pressure either (a stranded 'evicted' row
    # over a live file is dishonest at ANY pressure). Placed AFTER the disk stat
    # above so an unmounted/unreadable root can never make its files read as
    # "gone" and be wrongly finalized. A row restored here (file still present)
    # is 'available' again by the time candidates are assembled below, so THIS
    # same sweep re-decides its eviction fresh through the normal claim path.
    await _resume_interrupted_evictions(
        session=session,
        library=library,
        media_type=media_type,
        root_path=root_path,
        all_roots=scope,
    )

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
        await _movie_candidates(session, library, disk.total_bytes, root_path, scope)
        if media_type == "movie"
        else await _season_candidates(session, library, disk.total_bytes, root_path, scope)
    )
    if not pairs:
        return []

    pending_by_id: dict[int, _Pending] = {id(candidate): pending for candidate, pending in pairs}
    candidates = [candidate for candidate, _pending in pairs]
    grace_cutoff = datetime.now(UTC) - timedelta(days=grace_days)

    # The full stalest-first ranking, computed once: `select_evictions`'s result
    # is always a PREFIX of this same ordering (it ranks internally via this
    # exact function, then takes a prefix) -- so whatever it does NOT select is
    # exactly the pool available to draw from below if the actual freed bytes
    # come up short of the estimate.
    ranked_all = rank_eviction_candidates(candidates, grace_cutoff)
    selected = (
        ranked_all
        if proactive
        else select_evictions(candidates, disk_used_pct, threshold_pct, target_pct, grace_cutoff)
    )
    extra_pool = [] if proactive else ranked_all[len(selected) :]

    outcomes: list[EvictionOutcome] = []
    freed_bytes_total = 0

    async def _attempt(candidate: EvictionCandidate) -> None:
        nonlocal freed_bytes_total
        try:
            outcome = await _evict_one(
                session=session,
                fs=fs,
                library=library,
                candidate=candidate,
                pending=pending_by_id[id(candidate)],
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
            return
        if outcome is not None:
            outcomes.append(outcome)
            if outcome.freed_bytes is not None:
                freed_bytes_total += outcome.freed_bytes

    for candidate in selected:
        await _attempt(candidate)

    # R4-6: the estimate-based selection above can under-deliver (a hardlinked
    # candidate frees far less than its nominal size) -- keep going, stalest
    # first, until the REAL running freed total actually reaches target_pct or
    # every remaining eligible candidate has been tried. A no-op when the
    # estimate matched reality (the common case): `extra_pool` is empty, or the
    # very first check below already finds `projected <= target_pct`.
    if extra_pool and disk.total_bytes > 0:
        for candidate in extra_pool:
            if pressure_relieved(disk_used_pct, freed_bytes_total, disk.total_bytes, target_pct):
                break
            await _attempt(candidate)

    return outcomes
