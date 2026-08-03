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
   delete that had already run; if the delete then fails WITHOUT having removed
   anything, the claim is restored to ``available`` (and any in-window re-grab it
   spawned is reconciled away) so a failed unlink can never strand an ``evicted``
   row over a still-watchable file. A delete that removed PART of a tree before
   failing (issue #482) is the opposite case and keeps its claim: the media is no
   longer complete, so restoring it would publish an unplayable ``available``.
   Which of the two happened is recorded DURABLY on the claim row itself
   (``partial_delete_path``, issues #482 / #485): armed by the purge primitive at
   the delete-start boundary, after its containment/accounting preflight, and
   cleared only by an outcome that proves the tree intact-or-gone. A crash before
   that boundary therefore restores and re-decides a merely authorized delete;
   a crash, cancellation, or restart after it still leaves the honest answer where
   step 0.5 can read it.
   A candidate missing its ``library_path`` breadcrumb, or one the filesystem
   guard refuses, is skipped + logged — NEVER guessed at, never a silent no-op,
   and never lets one bad candidate abort the rest of the sweep.
   Between the claim commit and the delete, a shared-breadcrumb TWIN (#155) --
   another live row also carrying this exact ``library_path`` -- is likewise
   released back to ``available`` rather than deleted out from under it (the
   same finalized-vs-interrupted discriminator step 0.5's crash recovery uses,
   reused here before any crash is even in the picture).

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
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal

from plex_manager.adapters.plex.library import PlexAuthError, PlexLibraryError
from plex_manager.domain.disk_usage import used_percent
from plex_manager.domain.eviction import (
    EvictionCandidate,
    is_evictable,
    pressure_relieved,
    rank_eviction_candidates,
    select_evictions,
)
from plex_manager.domain.state_machine import DownloadState
from plex_manager.logsafe import safe_int, safe_text
from plex_manager.models import DownloadHistory, DownloadHistoryEvent, RequestStatus
from plex_manager.ports.library import WatchStateQuery
from plex_manager.repositories.downloads import SqlDownloadRepository
from plex_manager.repositories.requests import SqlRequestRepository
from plex_manager.repositories.season_requests import SqlSeasonRequestRepository
from plex_manager.services import purge_service, season_request_service, watchlist_service
from plex_manager.services.health_service import read_disk_usage
from plex_manager.services.library_roots import deepest_containing_root
from plex_manager.services.purge_service import PurgeOutcome

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from plex_manager.domain.disk_usage import DiskUsage
    from plex_manager.ports.download_client import DownloadClientPort
    from plex_manager.ports.filesystem import FileSystemPort
    from plex_manager.ports.library import LibraryPort

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
    #: ``True`` when the delete removed only PART of the tree before failing
    #: (issue #482): the row is committed ``evicted`` and files really did leave,
    #: but reclaimable remains are still on disk pending a recovery retry, and
    #: ``freed_bytes`` is unknowable. Callers still publish/refresh on it (the
    #: state change is real); the periodic loop's summary names it separately so
    #: "evicted 3 titles" never silently means "…and one is half-deleted".
    partial: bool = False


@dataclass(frozen=True)
class _MoviePending:
    """Everything :func:`_evict_one` needs to execute a MOVIE eviction, resolved
    once at candidate-assembly time and looked up again (by ``id(candidate)``)
    for whichever candidates the domain selects."""

    media_request_id: int
    tmdb_id: int
    size_bytes: int | None
    correction_recovery: bool = False


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
# scans. Pre-grab/cancelled rows are inherently tied to the crash-window re-arm;
# ``downloading``/``failed`` are included only when the durable marker gates them,
# because the replacement can advance before the next sweep. A genuinely active
# scoped download still defers the destructive retry below. A crash-window re-arm
# can be CANCELLED before recovery runs (re-arm -> the same-release re-grab resurrects
# the old imported download row, unblocking cancel's imported-row probe -> user
# cancels -> crash before the finalize), leaving ``cancelled`` + a stale
# breadcrumb -- a shape BOTH enumerations would otherwise miss and that
# ``evicted_seasons`` (rightly cancelled-blind) cannot subtract, so stale Plex
# presence could mint ``available`` over a deleted file. Season-only, like the
# rest of the re-armed shape: a movie cannot reach ``cancelled`` + breadcrumb
# through the eviction lifecycle (its re-grab is a SEPARATE row with no
# breadcrumb, and the claimed ``evicted`` movie row is not cancellable at all);
# the one movie shape that CAN carry it -- report-issue's failed-purge re-arm
# later cancelled -- is marker-owned recovery ONLY while its incomplete-delete
# marker is armed. Without that marker it is a deliberately settled correction
# whose kept breadcrumb is the orphan-reclaim handle and is left alone (#513).
_MARKER_OWNED_ADVANCED_STATUSES: frozenset[str] = frozenset(
    {RequestStatus.downloading.value, RequestStatus.failed.value}
)
_REARMED_RECOVERY_STATUSES: frozenset[str] = (
    _PRE_GRAB_STATUSES | _MARKER_OWNED_ADVANCED_STATUSES | {RequestStatus.cancelled.value}
)

# The MARKER-GATED extension of the enumeration above (Codex round-4 P1): a
# season whose replacement import was BLOCKED by the very remnants an incomplete
# purge left behind. ``LocalFileSystem.hardlink_or_copy`` refuses to overwrite a
# different file at a deterministic destination, so a season directory still
# holding an old episode file makes the replacement import fail with
# ``FileExistsError`` -> ``import_blocked`` -- a status the re-armed enumeration
# does not cover, leaving the row stuck forever with remains on disk and every
# operator retry hitting the same conflict (a terminal, exactly what north star 1
# forbids). Enumerated ONLY when the incomplete-delete marker is ARMED over the
# row's own breadcrumb, so a plain ``import_blocked`` season (an import conflict
# of its own, nothing to do with an interrupted purge) is never touched by this
# pass -- it is emphatically NOT folded to ``available`` or finalized.
_BLOCKED_BY_REMNANTS_STATUS: str = RequestStatus.import_blocked.value

# Every status the re-armed pass can adjudicate, for the breadcrumb-release CAS
# (a subset of ``_STALE_SEASON_BREADCRUMB_CLEAR_STATUSES`` below). Seasons may
# enter the pre-grab subset without a marker through eviction's same-row re-arm;
# movies join this enumeration ONLY when their marker is armed, which identifies
# report-issue's incomplete purge rather than an interrupted eviction (#513).
_REARMED_OR_BLOCKED_STATUSES: frozenset[str] = _REARMED_RECOVERY_STATUSES | {
    _BLOCKED_BY_REMNANTS_STATUS
}

# Why the recovery pass finished an incomplete purge, per the status it observed
# -- the history row's ``recovery_note``. Only the audit wording differs; the
# remains are cleared identically for all of them.
_REMNANT_PURGE_NOTES: dict[str, str] = {
    RequestStatus.cancelled.value: (
        "its re-grab was cancelled and the incomplete purge was finished here"
    ),
    _BLOCKED_BY_REMNANTS_STATUS: (
        "its replacement import was blocked by the remains, so the incomplete "
        "purge was finished here"
    ),
}

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

_STALE_MOVIE_BREADCRUMB_CLEAR_STATUSES: frozenset[str] = (
    frozenset({RequestStatus.evicted.value}) | _REARMED_OR_BLOCKED_STATUSES
)

# In-PROCESS sweep serialization latch: :func:`run_eviction_sweep` no-ops (with
# a log line) when another sweep is still running. Exactly two actors ever call
# it -- the periodic tick and the manual ``POST /ops/evict`` button -- and
# overlapping sweeps have ZERO value: both would select from the same candidate
# pool toward the same target, and every cross-sweep race class this module ever
# had to defend against (double-claim, resume-vs-mid-purge) exists ONLY when
# sweeps overlap. Serializing them deletes that permutation class outright; the
# claim CAS in :func:`_evict_one` remains the database-enforced backstop for
# anything outside this process. "Still running" covers CANCELLATION unwinding
# too, not only a normal in-progress tick (issue #128): a cancelled sweep does
# not release this latch until any in-flight purge delete has genuinely
# settled (``purge_service._delete_to_settlement`` shields exactly that wait),
# so ``_resume_interrupted_evictions`` on a LATER sweep can never observe a
# path mid-delete and restore its row to 'available' out from under a
# still-running ``rmtree``. A plain module bool, not an ``asyncio.Lock``:
# the check-and-set below has no ``await`` between them, so it is atomic on the
# single event loop; a plain mutable holder has no loop binding (asyncio
# primitives cache their first loop, which breaks under per-test event loops);
# and queueing a second sweep would only make it re-scan what the first is
# already relieving. The deployment is one app process over single-writer
# SQLite, so in-process is the honest scope -- stated here rather than assumed
# silently. (A one-slot dict instead of a module bool: mutation needs no
# ``global`` statement, which static scanners misread as an unused global.)
_sweep_latch: dict[str, bool] = {"busy": False}


def _partial_delete_outstanding(partial_delete_path: str | None, library_path: str) -> bool:
    """Whether the row's DURABLE incomplete-delete marker still covers
    ``library_path`` (issues #482 / #485).

    The marker (``MediaRequest``/``SeasonRequest.partial_delete_path``) is armed on
    the claim row BEFORE any destructive delete and disarmed only once the outcome
    PROVES the tree needs no further reclaiming -- see those columns' docstrings.
    ``True`` here therefore means "a delete of exactly this path began and nothing
    has established that the media at it is still complete", which covers all of:
    a classified ``PurgeOutcome.partial``, a crash mid-``rmtree``, a cancelled
    sweep whose worker settled unobserved, and a process restart in any of those
    windows. Recovery must never restore such a row to ``available`` off a bare
    ``os.stat`` -- a gutted directory stats exactly like an intact one.

    The equality against the row's CURRENT breadcrumb is the second half of the
    guard: a replacement import that stamps a DIFFERENT path has superseded the
    marker's subject even if the clear in ``set_library_path`` were somehow
    bypassed, and a NULL breadcrumb names no tree left to reclaim at all.
    """
    return partial_delete_path is not None and partial_delete_path == library_path


async def _arm_partial_delete(session: AsyncSession, pending: _Pending, library_path: str) -> None:
    """ARM the row's durable incomplete-delete marker over ``library_path``.

    Called at the purge primitive's delete-start boundary, after every eligibility
    guard plus containment/accounting preflight and immediately before destructive
    work is handed to the delete worker. Flushes only -- the caller owns the commit
    (issues #482 / #485 / #515).
    """
    if isinstance(pending, _SeasonPending):
        await SqlSeasonRequestRepository(session).set_partial_delete_path(
            pending.season_request_id, library_path
        )
    else:
        await SqlRequestRepository(session).set_partial_delete_path(
            pending.media_request_id, library_path
        )


async def _disarm_partial_delete(session: AsyncSession, pending: _Pending) -> None:
    """DISARM the row's durable incomplete-delete marker (issues #482 / #485).

    Called ONLY where the delete's outcome proves the tree needs no further
    reclaiming: it completed, it provably removed nothing, or it never started.
    Idempotent; flushes only, so it joins whatever commit the caller is building.
    """
    if isinstance(pending, _SeasonPending):
        await SqlSeasonRequestRepository(session).clear_partial_delete_path(
            pending.season_request_id
        )
    else:
        await SqlRequestRepository(session).clear_partial_delete_path(pending.media_request_id)


def _safe_title(title: str | None) -> str | None:
    """:func:`~plex_manager.logsafe.safe_text` for an OPTIONAL stored title.

    Titles come from request/TMDB metadata, so they can carry line boundaries that
    would forge a second log record (AGENTS.md's logging rule). ``None`` -- a
    parent row that vanished mid-recovery -- passes through as ``None`` so the
    log still says so honestly rather than printing an invented empty title.
    """
    return safe_text(title) if title is not None else None


async def _partial_delete_still_armed(
    session: AsyncSession, pending: _Pending, library_path: str
) -> bool:
    """Re-read the claim row and confirm its incomplete-delete marker STILL
    covers ``library_path`` (issues #482 / #485).

    The enumeration's snapshot can be stale by the time a destructive retry runs:
    a replacement import committing in between re-stamps ``library_path`` and
    retires the marker in that SAME commit, and purging then would delete the
    content that import just placed. ``get_fresh``'s ``populate_existing=True``
    forces this session to see that commit rather than its own snapshot -- the
    same discipline :func:`_still_evictable` uses before a claim.

    This closes the window AFTER an import commits; the window DURING one is
    closed by the placement registration (``purge_library_path`` defers on a
    conflicting path), and the registration is held until that commit completes,
    so the two leave no gap between them.

    Movies take the same read (issue #515): a movie's re-request is a SEPARATE
    row so it cannot re-stamp this one, but the operator-facing correction paths
    can retire the marker on this very row, and the delete-boundary hook must be
    able to see that for either shape.
    """
    if isinstance(pending, _SeasonPending):
        fresh = await SqlSeasonRequestRepository(session).get_fresh(pending.season_request_id)
    else:
        fresh = await SqlRequestRepository(session).get_fresh(pending.media_request_id)
    return (
        fresh is not None
        and fresh.library_path == library_path
        and _partial_delete_outstanding(fresh.partial_delete_path, library_path)
    )


async def _coverage_claim_active(session: AsyncSession, pending: _Pending) -> bool:
    """Whether a LIVE pack holds an active ride-along coverage claim over this
    season (issue #465).

    A pack's ``download_coverage_claims`` row is scopeless, so neither the status
    CAS nor ``find_active_for_request`` can see it; every destructive path in this
    module therefore consults it directly off the committed snapshot immediately
    before deleting. Movies never hold a claim, so a movie is honestly ``False``.
    """
    if not isinstance(pending, _SeasonPending):
        return False
    return (
        await SqlDownloadRepository(session).find_active_coverage_title(
            pending.tmdb_id, pending.season_number
        )
    ) is not None


# Download states in which a replacement download is still physically MOVING
# BYTES for this scope. Only these veto a marker-owned recovery purge (issue
# #515): the incoming payload lands at the same deterministic destination the
# remnants occupy, so deleting under a live transfer would eat what that import
# is about to place.
#
# The states deliberately ABSENT are the whole point of the distinction. An
# ``import_pending``/``importing``/``import_blocked`` download has settled its
# bytes in the client and is waiting on PLACEMENT, which is exactly the step the
# remnants block. ``import_blocked`` most of all: that is the marker owner's OWN
# blocked import, the single row this recovery pass exists to unblock.
# ``repositories/downloads.py``'s ``_ACTIVE_SCOPE_STATUSES`` deliberately counts
# an ``import_blocked`` SCOPE as active (it still owns the scope for dedup), so a
# blanket ``find_active_for_request(...) is not None`` eligibility test rejects
# precisely the row recovery must clean -- the remains stay, the import stays
# blocked, and every operator retry hits the same conflict forever. That is a
# terminal, which north star 1 forbids, and it is deterministic rather than racy:
# the enumeration that FINDS the row and the predicate that REJECTS it read the
# same fact in opposite directions.
_TRANSFER_ACTIVE_DOWNLOAD_STATES: frozenset[str] = frozenset(
    {
        DownloadState.Downloading.value,
        DownloadState.MetadataFetching.value,
        DownloadState.FailedPending.value,
        DownloadState.ClientMissing.value,
    }
)


class _RecoveryPurgeRevoked(Exception):
    """A marker-owned recovery purge was revoked AT the delete boundary.

    Raised by :func:`_recovery_delete_boundary` from inside the purge primitive's
    ``before_delete`` hook, which runs after the delete-worker permit is held and
    immediately before the destructive worker starts. The primitive propagates a
    hook failure unchanged and starts no delete, so the caller catching this has a
    hard guarantee that nothing left disk: the claim, the breadcrumb and the
    marker all still stand for the next sweep.

    ``reason`` is a STATIC literal chosen from the checks below -- never a
    request-derived string -- so it is safe to interpolate into a log message
    directly (AGENTS.md's logging rule).
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# RECOVERY POLICY for a marker-owned purge (issue #515, superseding this branch's
# first attempt). Finishing an incomplete delete is CONVERGENCE, not a fresh
# eviction decision: the marker says a destructive delete of exactly this path
# began and nothing has shown the tree survived it, so the media is already
# unwatchable. Re-gating that convergence on the conditions that AUTHORIZED the
# original eviction -- disk pressure and Plex watch state -- does not save the
# title; it strands gutted media forever. Pressure can drop the moment the
# interrupted delete freed part of the tree (the delete itself relieves it), and
# a "rewatch" reading over a half-deleted season is Plex reporting a title that
# cannot play. Neither fact can restore what is already gone, and an ``evicted``
# row is invisible to candidate assembly, so deferring on them means no pass ever
# retries: the remains leak disk forever and, for TV, permanently block the
# replacement import at its deterministic destination.
#
# So pressure and watch state are NOT re-checked here. What IS re-checked is the
# set of hard, mutable SAFETY facts -- the ones that say deleting the remaining
# bytes would destroy something that is not ours to destroy:
#
#   * the operator's ``keep_forever`` pin and an active watchlist entry -- INTENT
#     that outranks convergence. ``POST /requests/{id}/keep-forever`` has no
#     status restriction, so a pin can land on an ``evicted`` row mid-outage, and
#     ``_evict_one``'s invariant #1 ("a pinned title is NEVER deleted") is a
#     whole-module contract, not a claim-path one;
#   * a genuinely COMPETING active transfer for this scope (see
#     :data:`_TRANSFER_ACTIVE_DOWNLOAD_STATES`) -- its bytes are still landing;
#   * a live pack's ride-along coverage claim over this season (#465);
#   * the marker still ARMED over this exact path, and no OTHER live row claiming
#     it -- a replacement import that committed re-stamps the breadcrumb and
#     retires the marker in one commit, and its content is not the remains.
#
# Every one of those is re-read at the delete boundary itself, not merely before
# the awaited preflight -- see :func:`_recovery_delete_boundary`.
async def _recovery_intent_blocked_reason(session: AsyncSession, pending: _Pending) -> str | None:
    """The operator-INTENT half for retention-owned eviction recovery.

    Split out of the ownership half so the delete boundary can read it LAST, in
    the same write transaction that stamps the marker -- see
    :func:`_recovery_delete_boundary`. Intent is the fact most likely to change
    under a human's hand mid-sweep and the one whose violation is least
    recoverable (the pinned bytes are simply gone), so it is the one that must
    have no awaited query after it.

    A movie correction marker belongs to an operator-requested destructive redo,
    not a disk-pressure eviction. A pin/watchlist added before or during that redo
    must not strand the reported-bad remnants forever; correction recovery still
    observes transfer, placement, path-owner, and marker safety below.

    Reads through ``get_fresh``'s ``populate_existing=True``, so a pin committed
    by another session since this sweep began is visible rather than served from
    this session's identity map.
    """
    if isinstance(pending, _MoviePending) and pending.correction_recovery:
        return None
    if isinstance(pending, _SeasonPending):
        parent = await SqlRequestRepository(session).get_fresh(pending.media_request_id)
        season = await SqlSeasonRequestRepository(session).get_fresh(pending.season_request_id)
        if parent is None or season is None:
            return "its request row vanished"
        if parent.keep_forever:
            return "the operator pinned the show (keep_forever)"
        media_type: Literal["movie", "tv"] = "tv"
    else:
        request = await SqlRequestRepository(session).get_fresh(pending.media_request_id)
        if request is None:
            return "its request row vanished"
        if request.keep_forever:
            return "the operator pinned the title (keep_forever)"
        media_type = "movie"
    if await watchlist_service.is_watchlisted(session, pending.tmdb_id, media_type):
        return "the title is on an active watchlist"
    return None


async def _recovery_ownership_blocked_reason(
    session: AsyncSession, pending: _Pending
) -> str | None:
    """The OWNERSHIP half: another actor is actively working this scope's bytes."""
    if await _competing_active_transfer(session, pending):
        return "a replacement download is still transferring into this scope"
    if await _coverage_claim_active(session, pending):
        return "a live pack holds an active ride-along coverage claim over it"
    return None


async def _recovery_purge_blocked_reason(session: AsyncSession, pending: _Pending) -> str | None:
    """The reason a marker-owned recovery purge must NOT run, or ``None``.

    A CHEAP EARLY-OUT ONLY -- explicitly NOT load-bearing. It runs before the
    purge primitive is entered at all, so an already-revoked row skips the two
    filesystem preflight probes and the delete-permit wait and gets an honest log
    instead. But its snapshot is stale by construction: those probes and that wait
    are an unbounded stretch of awaits, and everything this reads can change
    inside it. Deleting this call would cost log quality and wasted probes, never
    safety.

    The AUTHORITY is :func:`_recovery_delete_boundary`, which re-reads every one
    of these facts after its writer-locking CAS -- the same division of labour as
    ``_still_evictable`` (cheap filter) versus the claim CAS (authority) in
    :func:`_evict_one`.

    A static, log-safe phrase rather than a bare bool so every deferral log says
    which safety fact revoked it (honesty over silence).
    """
    return await _recovery_intent_blocked_reason(
        session, pending
    ) or await _recovery_ownership_blocked_reason(session, pending)


async def _disarm_unstarted_delete(
    session: AsyncSession, pending: _Pending, title: str | None, season_note: str
) -> None:
    """COMPENSATE an incomplete-delete marker whose delete never started.

    The marker is armed in the purge primitive's delete-boundary hook, and the
    only step left after that hook is ``threading.Thread.start()`` -- which can
    still fail (thread exhaustion). Nothing was unlinked, so leaving the marker
    armed would tell the next sweep's recovery that a destructive delete began on
    an intact tree, and recovery deliberately CONVERGES such a row rather than
    restoring it (issue #515's policy). Take the marker back off in its own
    follow-up commit.

    Best-effort by construction: this runs while a failure is already propagating,
    so a compensation that itself fails must not replace the original exception.
    It is logged in full instead of swallowed -- the row is then left in the state
    the ambiguous-commit sliver documented at the arm site describes, which
    recovery's boundary re-checks contain.
    """
    try:
        await session.rollback()
        await _disarm_partial_delete(session, pending)
        await session.commit()
    except Exception:
        with contextlib.suppress(Exception):
            await session.rollback()
        _logger.exception(
            "could not disarm the incomplete-delete marker of %r%s after its delete "
            "worker failed to start; the row will read as an outstanding delete over "
            "a tree nothing touched until a later sweep's recovery adjudicates it",
            _safe_title(title),
            season_note,
            extra={
                "request_id": safe_int(pending.media_request_id),
                "tmdb_id": safe_int(pending.tmdb_id),
            },
        )


async def _competing_active_transfer(session: AsyncSession, pending: _Pending) -> bool:
    """Whether a download that is still MOVING BYTES owns this scope.

    Deliberately narrower than ``find_active_for_request(...) is not None``: that
    predicate answers "does anything own this scope", and an ``import_blocked``
    row owns it while being the very thing the remnants are blocking. See
    :data:`_TRANSFER_ACTIVE_DOWNLOAD_STATES` for why the difference is a hard
    deadlock rather than a tuning choice.
    """
    active = await SqlDownloadRepository(session).find_active_for_request(
        pending.media_request_id,
        season=pending.season_number if isinstance(pending, _SeasonPending) else None,
    )
    return active is not None and active.status in _TRANSFER_ACTIVE_DOWNLOAD_STATES


def _recovery_delete_boundary(
    session: AsyncSession, pending: _Pending, library_path: str
) -> Callable[[], Awaitable[None]]:
    """Build the purge primitive's ``before_delete`` hook for a recovery purge.

    THE fix for issue #515's delete-boundary race. The caller's eligibility read
    happens before ``purge_library_path``'s two awaited preflight probes and
    before the wait for one of the four delete-worker permits -- a window an
    operator pin, a fresh grab or a coverage claim can easily land inside. Every
    check is therefore re-run HERE: after the permit is held, with nothing but a
    synchronous worker start left between this hook returning and the first
    unlinked byte. A revocation raises, the primitive starts no delete, and the
    claim/breadcrumb/marker survive intact for the next sweep to re-decide.

    ORDER INSIDE THE HOOK IS LOAD-BEARING, and the shape is WRITE-FIRST with
    EVERY safety read after the write (rounds 3 and 4 of review, which found the
    read-then-restamp and then the reads-straddling-the-CAS versions unsound):

    1. ``session.rollback()`` FIRST, to end whatever read transaction the caller's
       cheap pre-check left open. That snapshot predates ``purge_library_path``'s
       two filesystem probes AND the wait for a delete permit -- an unbounded
       stretch of awaits -- so anything read under it is exactly as stale as the
       pre-check this hook exists to backstop.
    2. Then ONE atomic conditional ``UPDATE`` (:meth:`SqlRequestRepository.
       confirm_partial_delete_marker` and its season twin) as the FIRST statement
       of the fresh transaction: re-stamp the marker WHERE the breadcrumb and the
       marker still hold this exact path AND the row (or, for a season, its
       parent) is NOT ``keep_forever``-pinned. ``rowcount == 0`` is the database
       itself refusing -- a pin landed, or the marker was retired -- and revokes.
    3. Only THEN, inside that same transaction and under the writer lock the
       statement above took, EVERY remaining safety read:
       * ownership -- a competing active transfer, a ride-along coverage claim;
       * the path fact -- another live row now claiming this exact breadcrumb;
       * intent that cannot be folded into the CAS's ``WHERE`` -- the watchlist
         row, then a final fresh pin re-read (see below).
       Ownership and path belong here, not before the CAS (round-4 P1): a
       replacement grab or a new path owner that commits during those probe/permit
       awaits is invisible to a pre-CAS snapshot, and the CAS itself validates only
       pin and marker -- so reading them early let a delete proceed against
       ownership that had already changed. Revocation rolls the whole transaction
       back, leaving the row exactly as found -- the re-stamp was value-identical
       anyway.
    4. Commit. Only after that does the primitive start the delete worker.

    WHY WRITE-FIRST, precisely:

    * A value-identical ORM attribute assignment emits NO SQL at all. The unit of
      work sees no attribute change, so ``session.commit()`` sends nothing --
      verified, not assumed: the transaction contained only the two SELECTs. A
      "re-stamp" written that way is not a write, takes no lock, and serializes
      against nothing. It must be a query-level ``update()``.
    * The same real ``UPDATE`` placed AFTER the reads would still not help. In WAL
      the transaction's read snapshot is fixed at its FIRST statement, so reads
      taken before the write see a snapshot that predates the lock; worse, the
      read-to-write upgrade can fail outright with ``SQLITE_BUSY_SNAPSHOT`` when
      another writer committed in between. Taking the lock with the transaction's
      first statement makes the snapshot postdate every previously committed pin
      and makes every racing pin queue behind this transaction.

    This leans on SQLite's single-writer model (ADR-0007) exactly as ADR-0022
    step 7 does, and inherits the same engine dependence: a future multi-writer
    database needs ``SELECT … FOR UPDATE`` or a serializable transaction here,
    not this statement's implicit lock.

    Step 3's pin re-read is deliberately redundant WITH the CAS on an engine that
    really does exclude concurrent writers -- there, a pin committing after the
    CAS has queued behind it and lands only once the delete is already underway,
    which is the same irreducible micro-window every other post-claim/pre-delete
    guard in this module accepts. It is kept because it costs one SELECT and is
    the only guard left on any deployment where that exclusion does not hold
    (a shared connection, a future engine). It is read LAST, with no awaited query
    after it, for the same reason the whole hook exists.
    """

    async def _revoke_if_no_longer_safe() -> None:
        # End whatever read transaction the caller's cheap pre-check left open, so
        # the CAS below is the FIRST statement of the final one and its writer lock
        # is what fixes that transaction's snapshot. Nothing is read before this.
        await session.rollback()
        try:
            if not await _confirm_delete_marker_cas(session, pending, library_path):
                raise _RecoveryPurgeRevoked(
                    "the database refused the delete-boundary re-stamp: the title was "
                    "pinned, or its breadcrumb/incomplete-delete marker was retired"
                )
            reason = await _recovery_ownership_blocked_reason(session, pending)
            if reason is None and await _path_claimed_by_another_row(
                session, library_path, pending
            ):
                reason = "another live row now claims its path"
            if reason is None:
                reason = await _post_cas_intent_blocked_reason(session, pending)
            if reason is not None:
                raise _RecoveryPurgeRevoked(reason)
            await session.commit()
        except BaseException:
            # Includes the revocations above: nothing this hook staged may survive
            # a boundary that decided not to delete (nor a cancellation landing
            # mid-transaction). The re-stamp is value-identical, so the rollback
            # leaves the row byte-for-byte as it was found either way.
            await session.rollback()
            raise

    return _revoke_if_no_longer_safe


async def _confirm_delete_marker_cas(
    session: AsyncSession, pending: _Pending, library_path: str
) -> bool:
    """The delete boundary's atomic conditional re-stamp -- see
    :func:`_recovery_delete_boundary` step 2. ``False`` means the DATABASE refused.
    """
    if isinstance(pending, _SeasonPending):
        return await SqlSeasonRequestRepository(session).confirm_partial_delete_marker(
            pending.season_request_id, expected_path=library_path
        )
    return await SqlRequestRepository(session).confirm_partial_delete_marker(
        pending.media_request_id,
        expected_path=library_path,
        honor_keep_forever=not pending.correction_recovery,
    )


async def _post_cas_intent_blocked_reason(session: AsyncSession, pending: _Pending) -> str | None:
    """The intent facts read AFTER the boundary's CAS, under its writer lock.

    The watchlist row cannot be folded into the CAS's ``WHERE`` (a different table
    on a different key), and the pin re-read is the defence-in-depth step 3 of
    :func:`_recovery_delete_boundary` explains. The pin is read LAST so no awaited
    query follows the most safety-critical fact.

    Correction-owned movie recovery intentionally ignores retention intent; see
    :func:`_recovery_intent_blocked_reason`.
    """
    if isinstance(pending, _MoviePending) and pending.correction_recovery:
        return None
    media_type: Literal["movie", "tv"] = "tv" if isinstance(pending, _SeasonPending) else "movie"
    if await watchlist_service.is_watchlisted(session, pending.tmdb_id, media_type):
        return "the title is on an active watchlist"
    parent = await SqlRequestRepository(session).get_fresh(pending.media_request_id)
    if parent is None:
        return "its request row vanished"
    if parent.keep_forever:
        return "the operator pinned the title (keep_forever)"
    return None


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


async def _sized_path(library_path: str | None, cache: dict[str, int | None]) -> int | None:
    """On-disk footprint of ``library_path``, walking each DISTINCT path at most
    once per candidate-assembly pass (issue #213, "size only once per distinct
    physical path").

    A falsy breadcrumb (``None`` or empty) -> ``None`` (unknown share), exactly as
    the replaced ``... if row.library_path else None`` did. Two rows
    legitimately sharing one ``library_path`` (the #155 shared-breadcrumb "twins"
    shape) would otherwise each ``os.walk`` the same tree; ``cache`` memoizes the
    (identical) result so the tree is walked once. The walk itself is unchanged --
    still offloaded via ``asyncio.to_thread`` because it is real, potentially slow
    disk I/O (see :func:`_size_bytes`).
    """
    if not library_path:
        return None
    if library_path in cache:
        return cache[library_path]
    size = await asyncio.to_thread(_size_bytes, library_path)
    cache[library_path] = size
    return size


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
    grace_cutoff: datetime | None = None,
) -> list[tuple[EvictionCandidate, _Pending]]:
    """Every ``available`` movie request OWNED by ``root_path`` as an
    :class:`EvictionCandidate`.

    ``partially_available`` is deliberately NOT queried here: it is a TV-only
    rollup status a plain movie request can never reach (see ``RequestStatus``),
    so ``available`` is the complete "fully imported" set for movies. Rows whose
    ``library_path`` is not owned by ``root_path`` — under a different configured
    root, a NESTED more-specific root, or no root at all — are skipped (see
    :func:`_owned_by_root`).

    ``grace_cutoff`` (issue #304), when given, lets this skip the ``os.walk``
    disk-size lookup for a row that :func:`~plex_manager.domain.eviction.
    is_evictable` (the SAME pure predicate ``rank_eviction_candidates``/
    ``select_evictions`` apply downstream, reused rather than re-derived so the
    two can never drift) already says can never be evicted at this cutoff --
    pinned, in-flight, unwatched, or still within grace. The row is still
    returned (the RAW-superset contract every caller of :func:`assemble_candidates`
    depends on is never narrowed here — see its docstring), just with the honest
    ``size_percent=0.0`` fallback the module already uses for an unknown size,
    never a real walk. ``None`` (the default) walks every row exactly like
    before this optimization existed -- a caller with no grace policy in hand
    yet (or a test proving the walk itself) gets the old, unconditional behavior.
    """
    request_repo = SqlRequestRepository(session)
    download_repo = SqlDownloadRepository(session)
    rows = [
        row
        for row in await request_repo.list_by_status(RequestStatus.available.value)
        if row.media_type == "movie"
        and (row.library_path is None or _owned_by_root(row.library_path, root_path, all_roots))
    ]
    # Batch the Plex watch-state discovery for the WHOLE candidate pool into ONE
    # section crawl (issue #213/#238) rather than one full re-crawl per candidate.
    # The Nth result is byte-for-byte what a per-candidate ``watch_state`` call
    # would return -- semantics are preserved exactly (see the port docstring).
    watch_states = await library.resolve_watch_states(
        [
            WatchStateQuery(tmdb_id=row.tmdb_id, media_type="movie", library_path=row.library_path)
            for row in rows
        ]
    )
    # Batch the active-download in-flight probe for the WHOLE candidate pool into
    # ONE query pair (issue #138) rather than one ``find_active_for_request`` SELECT
    # per candidate -- membership is byte-for-byte what a per-candidate call would
    # report (see the repository method's docstring).
    active_keys = await download_repo.find_active_for_requests([(row.id, None) for row in rows])
    size_cache: dict[str, int | None] = {}
    pairs: list[tuple[EvictionCandidate, _Pending]] = []
    for row, watch in zip(rows, watch_states, strict=True):
        in_flight = (row.id, None) in active_keys
        # Every field eligibility depends on is resolved BEFORE the size walk
        # (issue #304): size never enters the predicate, so a candidate built
        # with the honest ``size_percent=0.0`` placeholder is evaluated exactly
        # like the real one would be.
        prospective = EvictionCandidate(
            request_id=row.id,
            media_type="movie",
            title=row.title,
            season=None,
            status=row.status,
            watched=watch.watched,
            last_viewed_at=watch.last_viewed_at,
            keep_forever=row.keep_forever,
            watchlisted=await watchlist_service.is_watchlisted(session, row.tmdb_id, "movie"),
            in_flight=in_flight,
            library_path=row.library_path,
            size_percent=0.0,
        )
        if grace_cutoff is not None and not is_evictable(prospective, grace_cutoff):
            size_bytes = None
            candidate = prospective
        else:
            size_bytes = await _sized_path(row.library_path, size_cache)
            size_percent = (
                (size_bytes / root_total_bytes) * 100.0
                if size_bytes is not None and root_total_bytes > 0
                else 0.0
            )
            candidate = replace(prospective, size_percent=size_percent)
        pending = _MoviePending(media_request_id=row.id, tmdb_id=row.tmdb_id, size_bytes=size_bytes)
        pairs.append((candidate, pending))
    return pairs


async def _season_candidates(
    session: AsyncSession,
    library: LibraryPort,
    root_total_bytes: int,
    root_path: str,
    all_roots: Sequence[str],
    grace_cutoff: datetime | None = None,
) -> list[tuple[EvictionCandidate, _Pending]]:
    """Every ``available`` TV season OWNED by ``root_path`` as an
    :class:`EvictionCandidate`.

    The pin (``keep_forever``) and the title live on the PARENT show
    (``MediaRequest``), never on the season row itself, so pinning a series
    protects every one of its seasons — every distinct parent is resolved in ONE
    batched :meth:`~plex_manager.ports.repositories.RequestRepository.get_many`
    call (issue #137/#138) rather than one :meth:`get` per distinct show. Season
    rows whose ``library_path`` is not owned by ``root_path`` (see
    :func:`_owned_by_root`) are skipped.

    ``grace_cutoff`` (issue #304): see :func:`_movie_candidates`'s twin
    docstring paragraph -- identical walk-skip optimization, same
    :func:`~plex_manager.domain.eviction.is_evictable` predicate, same RAW
    superset contract.
    """
    season_repo = SqlSeasonRequestRepository(session)
    request_repo = SqlRequestRepository(session)
    download_repo = SqlDownloadRepository(session)
    rows = [
        row
        for row in await season_repo.list_by_status(RequestStatus.available.value)
        if row.library_path is None or _owned_by_root(row.library_path, root_path, all_roots)
    ]
    # Batch the Plex watch-state discovery for every season candidate into ONE
    # show-section crawl plus one ``/children`` read per distinct show (issue
    # #213/#238), instead of re-crawling the section and re-reading the show's
    # children once per season. The Nth result is byte-for-byte what a
    # per-candidate ``watch_state`` call would return.
    watch_states = await library.resolve_watch_states(
        [
            WatchStateQuery(
                tmdb_id=row.tmdb_id,
                media_type="tv",
                season=row.season_number,
                library_path=row.library_path,
            )
            for row in rows
        ]
    )
    # One query for every distinct parent show (issue #137/#138) instead of one
    # ``get()`` per distinct show.
    parents = await request_repo.get_many(list({row.media_request_id for row in rows}))
    # Batch the active-download in-flight probe for the WHOLE candidate pool into
    # ONE query pair (issue #138) rather than one ``find_active_for_request``
    # SELECT per candidate -- membership is byte-for-byte what a per-candidate
    # call would report (see the repository method's docstring).
    active_keys = await download_repo.find_active_for_requests(
        [(row.media_request_id, row.season_number) for row in rows]
    )
    # A pack's ride-along season carries no DownloadScope and matches no scalar
    # download row, so ``find_active_for_requests`` above is blind to it (issue
    # #465). Claims are owned by the grabbing request, but an older settled row
    # and newer active sibling can coexist for one title; probe title/season keys
    # once for the whole pool so either sibling's claim protects the available row.
    coverage_titles = await download_repo.find_active_coverage_titles(
        [(row.tmdb_id, row.season_number) for row in rows]
    )
    size_cache: dict[str, int | None] = {}
    pairs: list[tuple[EvictionCandidate, _Pending]] = []
    for row, watch in zip(rows, watch_states, strict=True):
        parent = parents.get(row.media_request_id)
        if parent is None:  # pragma: no cover - the FK guarantees the parent exists
            continue
        key = (row.media_request_id, row.season_number)
        in_flight = key in active_keys or (row.tmdb_id, row.season_number) in coverage_titles
        prospective = EvictionCandidate(
            request_id=row.id,
            media_type="tv",
            title=parent.title,
            season=row.season_number,
            status=row.status,
            watched=watch.watched,
            last_viewed_at=watch.last_viewed_at,
            keep_forever=parent.keep_forever,
            watchlisted=await watchlist_service.is_watchlisted(session, row.tmdb_id, "tv"),
            in_flight=in_flight,
            library_path=row.library_path,
            size_percent=0.0,
        )
        if grace_cutoff is not None and not is_evictable(prospective, grace_cutoff):
            size_bytes = None
            candidate = prospective
        else:
            size_bytes = await _sized_path(row.library_path, size_cache)
            size_percent = (
                (size_bytes / root_total_bytes) * 100.0
                if size_bytes is not None and root_total_bytes > 0
                else 0.0
            )
            candidate = replace(prospective, size_percent=size_percent)
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
    download for ``(request, season)`` AND no active ride-along coverage claim
    over it (issue #465 -- a scopeless pack coverage ``find_active_for_request``
    cannot see). Either side missing entirely (deleted out from under the sweep)
    is honestly ``False`` — nothing left to evict.
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
        # A ride-along coverage claim (issue #465) can land AFTER assembly (a pack
        # starting mid-sweep). It has no scope, so ``find_active_for_request``
        # cannot see it -- the non-terminal-gated coverage owner does.
        covered = (
            await download_repo.find_active_coverage_title(pending.tmdb_id, pending.season_number)
        ) is not None
        return (
            not parent.keep_forever
            and not await watchlist_service.is_watchlisted(session, pending.tmdb_id, "tv")
            and season.status == RequestStatus.available.value
            and not in_flight
            and not covered
        )

    request = await SqlRequestRepository(session).get_fresh(pending.media_request_id)
    if request is None:
        return False
    in_flight = (
        await download_repo.find_active_for_request(pending.media_request_id, season=None)
    ) is not None
    return (
        not request.keep_forever
        and not await watchlist_service.is_watchlisted(session, pending.tmdb_id, "movie")
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

    Every caller of this function has ESTABLISHED that the tree is intact -- the
    delete was refused, errored with its up-front inventory provably untouched, or
    never ran at all -- so the durable incomplete-delete marker (issues #482 /
    #485) is disarmed in the same transaction. That is the whole precondition of
    restoring: a tree a delete already ate into is NOT a live file, and this
    function must never be reached for one (see :func:`_evict_one`'s ``partial``
    branch, which keeps its claim instead).
    """
    await _disarm_partial_delete(session, pending)
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
    """Cancel every OTHER pre-grab request row for this movie THIS APP'S OWN
    eviction guard re-grabbed (see :func:`_restore_after_failed_delete`'s re-grab
    reconciliation). Flush-only; the caller owns the commit, so restore +
    reconciliation land atomically.

    Issue #156: scoped to :attr:`~plex_manager.ports.repositories.RequestRecord.
    eviction_regrab` rows ONLY -- a pre-grab row that exists for some OTHER reason
    (most notably a deliberate #148 forced re-acquire, which explicitly bypasses
    the eviction guard that stamps this marker) must never be silently cancelled
    just because it happens to share this movie and a pre-grab status. Skipping a
    non-eviction-regrab row here is not a leak: it is exactly the operator's own
    request, left standing precisely because the restored copy never left disk.
    """
    repo = SqlRequestRepository(session)
    for row in await repo.list_for_media(pending.tmdb_id, "movie", _PRE_GRAB_STATUSES):
        if row.id == pending.media_request_id:
            continue  # the restored row itself (defensive; it is 'available' now)
        if not row.eviction_regrab:
            continue  # not THIS eviction's own re-grab -- e.g. an operator re-acquire
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
    """Cancel this season's pre-grab duplicates under OTHER requests THIS APP'S
    OWN eviction guard re-grabbed (see :func:`_restore_after_failed_delete`'s
    re-grab reconciliation), recomputing each affected parent's rollup.
    Flush-only; the caller owns the commit.

    Issue #156: scoped to :attr:`~plex_manager.ports.repositories.
    SeasonRequestRecord.eviction_regrab` siblings ONLY -- see the movie twin's
    docstring (:func:`_cancel_redundant_movie_regrabs`) for the full rationale.
    """
    season_repo = SqlSeasonRequestRepository(session)
    request_repo = SqlRequestRepository(session)
    siblings = await season_repo.list_sibling_seasons(
        pending.tmdb_id,
        pending.season_number,
        _PRE_GRAB_STATUSES,
        exclude_id=pending.season_request_id,
    )
    for sibling in siblings:
        if not sibling.eviction_regrab:
            continue  # not THIS eviction's own re-grab -- e.g. an operator re-acquire
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
    fs: FileSystemPort,
    qbt: DownloadClientPort | None,
    media_type: Literal["movie", "tv"],
    root_path: str,
    all_roots: Sequence[str],
    correction_only: bool = False,
) -> bool:
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
    * PRE-GRAB-or-CANCELLED + breadcrumb (TV's same-row eviction re-arm, or a
      MARKER-GATED movie correction; every status in
      :data:`_REARMED_RECOVERY_STATUSES`) -- an in-window TV re-request re-armed the
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
      live file). A movie has no same-row EVICTION re-arm (a re-request creates
      a new row), but report-issue does re-arm the same movie row. Movie rows
      join this recovery shape ONLY with an armed marker over their breadcrumb:
      that durable fact proves correction's delete began and was not shown to
      finish. The pass then uses :func:`_resume_one`'s marker-owned convergence
      without folding the correction or changing its status; an unmarked
      ``searching``/``cancelled`` movie breadcrumb remains the deliberately
      settled orphan-reclaim handle and is left alone (#513).

    Before restoring/folding ANY file-present row, :func:`_path_claimed_by_another_row`
    distinguishes interrupted from FINALIZED-BUT-SUPERSEDED: if another live row
    now claims the same path (a re-import under a newer request -- the legacy
    upgraded-install shape), the stale breadcrumb is released instead of
    restoring, so a later sweep can never delete the shared path out from under
    the row that actually owns it.

    "File still on disk" is decided by a bare ``os.stat`` of the breadcrumb, which
    cannot tell a COMPLETE tree from one a previous delete already ate into
    (issues #482 / #485) -- a gutted directory stats exactly like an intact one.
    So the row's DURABLE incomplete-delete marker (``partial_delete_path``, armed
    with the eviction claim BEFORE the delete and cleared only by an outcome that
    proves the tree intact-or-gone) is read alongside the stat and OVERRIDES it:
    a row still carrying it is routed through the retry/converge path, never the
    restore. Because the marker lives on the row rather than in process memory,
    this holds across a restart, a cancelled sweep, and a crash mid-``rmtree``
    identically -- the boundaries where an in-process record silently evaporates.
    Retrying is also the only retry such a row can ever get: an ``evicted`` row is
    invisible to candidate assembly.

    ``correction_only`` narrows the enumeration to the shape an OPERATOR's
    report-issue purge owns -- a movie row whose durable marker still covers its
    own breadcrumb -- and drops everything retention claimed for itself (the
    ``evicted`` shapes, and TV entirely; see the branches below). It is what
    :func:`run_correction_recovery_sweep` runs while ``eviction_enabled`` is off,
    so the switch that stops the retention bot never strands a correction the
    operator already asked for.

    Returns whether recovery destroyed data the caller's disk snapshot (taken
    BEFORE this pass runs) does not reflect, so the sweep can re-baseline its
    pressure accounting rather than pick further victims against a stale reading.

    Runs at the START of every sweep, BEFORE the pressure pre-check (recovery
    must not wait for pressure) and only after the root's disk stat succeeded
    (an unmounted root must never make its files read as "gone"). Sweeps are
    serialized in-process (:data:`_sweep_latch`), so no eviction can be
    mid-purge in a concurrent sweep while this runs -- every matched row really
    is a leftover. A transient stat error skips the row (never guess "gone" off
    an I/O error); one row's failure never aborts the rest.
    """
    # (library_path, title, partial_delete_path, pending)
    pairs: list[tuple[str, str | None, str | None, _Pending]] = []
    rearmed: list[tuple[str, str | None, str, str | None, _SeasonPending]] = []
    if media_type == "movie":
        request_repo = SqlRequestRepository(session)
        # The claimed-eviction shape is RETENTION's own interrupted work, so it is
        # the enumeration ``correction_only`` drops: that mode runs while the
        # retention bot is switched off (see :func:`run_correction_recovery_sweep`)
        # and must not finish a delete the operator turned the bot off to stop.
        if not correction_only:
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
                pairs.append((row.library_path, row.title, row.partial_delete_path, pending))
        # Movie report-issue re-arms the SAME row and preserves its breadcrumb when
        # the purge is incomplete. Unlike TV's same-row eviction re-arm, status plus
        # breadcrumb alone is therefore not a recovery signature: a failed but
        # provably untouched correction deliberately keeps that orphan-reclaim
        # handle. The durable marker is the discriminator (#513). Enumerate every
        # status the replacement can reach, including import_blocked (the
        # deterministic destination deadlock), but only while the marker still
        # covers this row's own breadcrumb.
        for recovery_status in sorted(_REARMED_OR_BLOCKED_STATUSES):
            for row in await request_repo.list_by_status(recovery_status):
                if (
                    row.media_type != "movie"
                    or row.library_path is None
                    or not _owned_by_root(row.library_path, root_path, all_roots)
                    or not _partial_delete_outstanding(row.partial_delete_path, row.library_path)
                ):
                    continue
                pairs.append(
                    (
                        row.library_path,
                        row.title,
                        row.partial_delete_path,
                        _MoviePending(
                            media_request_id=row.id,
                            tmdb_id=row.tmdb_id,
                            size_bytes=None,
                            correction_recovery=True,
                        ),
                    )
                )
    elif not correction_only:
        # TV is absent from ``correction_only`` deliberately. A season's marker can
        # sit over EITHER a correction purge or retention's own same-row eviction
        # re-arm (``ensure_seasons`` rewrites the claimed row's status out from
        # under it), so nothing on the row distinguishes the two -- and with the
        # retention bot switched off, the conservative reading is the only honest
        # one. Movies carry no such ambiguity: an eviction re-request lands on a NEW
        # row, so a marker over a non-``evicted`` movie breadcrumb is report-issue's
        # purge by construction (#513). A TV remnant still converges on the next
        # enabled sweep, and the manual free-space button (which ignores the switch)
        # reaches it at any time.
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
            pairs.append(
                (
                    season.library_path,
                    parent.title if parent else None,
                    season.partial_delete_path,
                    pending,
                )
            )
        # The re-armed shape: breadcrumb-bearing recovery statuses (see the
        # docstring). Pre-grab/cancelled rows are lifecycle-specific; advanced
        # downloading/failed and import-blocked rows are marker-gated below.
        for pre_grab_status in [*sorted(_REARMED_RECOVERY_STATUSES), _BLOCKED_BY_REMNANTS_STATUS]:
            for season in await season_repo.list_by_status(pre_grab_status):
                if season.library_path is None or not _owned_by_root(
                    season.library_path, root_path, all_roots
                ):
                    continue
                if pre_grab_status in (
                    _MARKER_OWNED_ADVANCED_STATUSES | {_BLOCKED_BY_REMNANTS_STATUS}
                ) and not _partial_delete_outstanding(
                    season.partial_delete_path, season.library_path
                ):
                    # A plain import conflict of its own, with no interrupted purge
                    # behind it -- none of this pass's business (see
                    # :data:`_BLOCKED_BY_REMNANTS_STATUS`).
                    continue
                parent = await request_repo.get(season.media_request_id)
                rearmed.append(
                    (
                        season.library_path,
                        parent.title if parent else None,
                        pre_grab_status,
                        season.partial_delete_path,
                        _SeasonPending(
                            media_request_id=season.media_request_id,
                            season_request_id=season.id,
                            season_number=season.season_number,
                            tmdb_id=season.tmdb_id,
                            size_bytes=None,
                        ),
                    )
                )

    destroyed_unmeasured = False
    for library_path, title, partial_delete_path, pending in pairs:
        try:
            destroyed_unmeasured |= await _resume_one(
                session=session,
                library=library,
                fs=fs,
                media_type=media_type,
                library_path=library_path,
                title=title,
                partial_delete_path=partial_delete_path,
                pending=pending,
                qbt=qbt,
            )
        except Exception:
            # One interrupted eviction's recovery failing must never abort the
            # rest (nor poison the next row's transaction) -- same posture as the
            # sweep's per-candidate guard. The disk snapshot is conservatively
            # treated as stale: a failure mid-recovery may well have deleted first.
            destroyed_unmeasured = True
            await session.rollback()
            _logger.exception(
                "resuming an interrupted eviction failed unexpectedly; will retry next sweep",
                extra={"request_id": pending.media_request_id, "tmdb_id": pending.tmdb_id},
            )
    for library_path, title, observed_status, partial_delete_path, season_pending in rearmed:
        try:
            destroyed_unmeasured |= await _recover_rearmed_season(
                session=session,
                library=library,
                fs=fs,
                library_path=library_path,
                title=title,
                observed_status=observed_status,
                partial_delete_path=partial_delete_path,
                pending=season_pending,
            )
        except Exception:
            destroyed_unmeasured = True
            await session.rollback()
            _logger.exception(
                "recovering a re-armed interrupted eviction failed unexpectedly; "
                "will retry next sweep",
                extra={
                    "request_id": season_pending.media_request_id,
                    "tmdb_id": season_pending.tmdb_id,
                },
            )
    return destroyed_unmeasured


async def _recover_rearmed_season(
    *,
    session: AsyncSession,
    library: LibraryPort,
    fs: FileSystemPort,
    library_path: str,
    title: str | None,
    observed_status: str,
    partial_delete_path: str | None,
    pending: _SeasonPending,
) -> bool:
    """Acquire the path claim atomically, then recover one re-armed season."""
    if not purge_service.begin_purge(library_path):
        _logger.info(
            "deferring recovery of %r season %s: another purge owns its path",
            _safe_title(title),
            safe_int(pending.season_number),
            extra={
                "request_id": safe_int(pending.media_request_id),
                "tmdb_id": safe_int(pending.tmdb_id),
            },
        )
        return False
    try:
        return await _recover_rearmed_season_claimed(
            session=session,
            library=library,
            fs=fs,
            library_path=library_path,
            title=title,
            observed_status=observed_status,
            partial_delete_path=partial_delete_path,
            pending=pending,
        )
    finally:
        purge_service.end_purge(library_path)


async def _recover_rearmed_season_claimed(
    *,
    session: AsyncSession,
    library: LibraryPort,
    fs: FileSystemPort,
    library_path: str,
    title: str | None,
    observed_status: str,
    partial_delete_path: str | None,
    pending: _SeasonPending,
) -> bool:
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

    File present but the row carries an OUTSTANDING incomplete-delete marker over
    exactly this path (``partial_delete_path``, issues #482 / #485) -> no fold:
    a destructive delete began here and nothing proved it left the season intact,
    so the season is not watchable and the re-grab a fold would cancel is fetching
    files that really did leave. Instead the purge is FINISHED here, for every
    enumerated status (Codex round-4 P1):

    * the re-arm is still PRE-GRAB -- this pass used to leave the remains for the
      replacement import to overwrite, but an import cannot do that: TV
      destinations are deterministic and ``hardlink_or_copy`` REFUSES an existing
      different-content file, so a surviving episode makes the import fail
      (``import_blocked``) and every retry hit the same conflict forever. Clearing
      the remains first is what lets the replacement land at all.
    * the re-arm was CANCELLED -- no import is coming either way, and waiting would
      strand the remains forever: an ``evicted``-less, breadcrumb-bearing row is
      invisible to candidate assembly and no other pass retries it.
    * the replacement import ALREADY hit the remains (``import_blocked`` + an armed
      marker, :data:`_BLOCKED_BY_REMNANTS_STATUS`) -- the deadlock, observed. The
      remains are cleared and the row keeps its surfaced, retryable status, now
      over a clean directory.

    All three converge on the SAME file-gone finalize below, which only flips
    ``cancelled`` -> ``evicted`` (disk truth over an aborted intent) and otherwise
    leaves the status alone. A retry that cannot clear the remains keeps the
    breadcrumb + marker for the next sweep, never a fold. Deleting under a LIVE
    import is prevented by the purge primitive's placement deferral plus a fresh
    re-read of the marker (:func:`_partial_delete_still_armed`), not by waiting;
    an active ride-along coverage claim defers the retry entirely.

    File present but another live row claims the path -> release the stale
    breadcrumb and leave the row as it is (the re-grab/search is legitimate; its
    import will stamp a fresh breadcrumb). File gone -> the interrupted purge
    actually completed: record the eviction history it never got, release the
    breadcrumb (single-winner CAS), refresh Plex, and leave the status alone --
    a re-grab/search over a truly-gone file is exactly right.

    Returns whether this recovery destroyed data the sweep's pressure ledger has
    not accounted for (see :func:`_resume_one`).
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
            extra={
                "request_id": safe_int(pending.media_request_id),
                "tmdb_id": safe_int(pending.tmdb_id),
            },
        )
        return False

    if file_present:
        if await _path_claimed_by_another_row(session, library_path, pending):
            if await _release_stale_breadcrumb(
                session,
                pending,
                expected_path=library_path,
                expected_statuses=_REARMED_OR_BLOCKED_STATUSES,
            ):
                _logger.info(
                    "released a stale eviction breadcrumb of %r season %s: a newer "
                    "row owns the same path; the re-request proceeds normally",
                    _safe_title(title),
                    safe_int(pending.season_number),
                    extra={
                        "request_id": safe_int(pending.media_request_id),
                        "tmdb_id": safe_int(pending.tmdb_id),
                    },
                )
            # The marker is released together with the breadcrumb by that CAS --
            # the path belongs to the newer row now.
            return False
        if _partial_delete_outstanding(partial_delete_path, library_path):
            # Issues #482 / #485, the re-armed twin: the surviving directory stats
            # fine but the row durably records a destructive delete of exactly this
            # path that was never shown to have left the season intact, so the
            # season is NOT watchable and folding it to 'available' would cancel a
            # re-grab that is fetching genuinely absent files.
            #
            # FINISH THE PURGE -- for EVERY enumerated status, not just the
            # cancelled one (Codex round-4 P1). This branch used to leave a
            # still-pre-grab row alone on the theory that its replacement import
            # would re-place the tree, but an import cannot: TV destinations are
            # deterministic and ``LocalFileSystem.hardlink_or_copy`` REFUSES an
            # existing file with different content, so a surviving old episode
            # makes the replacement fail with ``FileExistsError`` ->
            # ``import_blocked`` -- and every operator retry then hits the same
            # conflict forever. The remnants must be gone BEFORE the placement,
            # so the marker's owner (this pass) completes its own purge instead of
            # delegating it to an import that is not allowed to overwrite.
            #
            # Deleting under a live import is prevented by the guards below plus
            # the purge primitive itself, not by waiting:
            #   * an import MID-PLACEMENT holds a placement registration, and
            #     ``purge_library_path`` returns ``deferred`` for a conflicting
            #     path rather than deleting into it;
            #   * an import that already COMMITTED re-stamped ``library_path`` and
            #     retired the marker in that same commit, which the fresh re-read
            #     below observes -- and because the placement registration is held
            #     until that commit is done, the two windows have no gap between
            #     them.
            #
            # The mutable safety facts (pin/watchlist intent, a COMPETING active
            # transfer -- pointedly not this row's own blocked import, see
            # :data:`_TRANSFER_ACTIVE_DOWNLOAD_STATES` -- and a ride-along coverage
            # claim) are read cheaply here for an honest early log, and re-read
            # authoritatively inside the delete-boundary hook below, which runs
            # under the held delete permit (issue #515).
            blocked = await _recovery_purge_blocked_reason(session, pending)
            if blocked is not None:
                _logger.info(
                    "deferring the incomplete-delete retry of %r season %s: %s; keeping "
                    "the breadcrumb and marker rather than deleting under it",
                    _safe_title(title),
                    safe_int(pending.season_number),
                    blocked,
                    extra={
                        "request_id": safe_int(pending.media_request_id),
                        "tmdb_id": safe_int(pending.tmdb_id),
                    },
                )
                return False
            if not await _partial_delete_still_armed(session, pending, library_path):
                _logger.info(
                    "leaving %r season %s alone: its incomplete-delete marker was retired "
                    "between this sweep's enumeration and now (a replacement import owns "
                    "the path); nothing purged",
                    _safe_title(title),
                    safe_int(pending.season_number),
                    extra={
                        "request_id": safe_int(pending.media_request_id),
                        "tmdb_id": safe_int(pending.tmdb_id),
                    },
                )
                return False
            try:
                retry = await purge_service.purge_library_path(
                    fs,
                    library_path,
                    hold_purge_registration=True,
                    before_delete=_recovery_delete_boundary(session, pending, library_path),
                )
            except _RecoveryPurgeRevoked as revoked:
                _logger.info(
                    "revoked the incomplete-delete retry of %r season %s at the delete "
                    "boundary: %s; no delete started, so the breadcrumb and marker stand",
                    _safe_title(title),
                    safe_int(pending.season_number),
                    revoked.reason,
                    extra={
                        "request_id": safe_int(pending.media_request_id),
                        "tmdb_id": safe_int(pending.tmdb_id),
                    },
                )
                return False
            if retry.outcome is not PurgeOutcome.deleted:
                _logger.warning(
                    "retrying the incompletely deleted eviction of %r season %s (%s) did "
                    "not clear the remains (%s: %s); keeping the breadcrumb for the next "
                    "sweep rather than folding a season that cannot be shown to be "
                    "complete back to 'available'",
                    _safe_title(title),
                    safe_int(pending.season_number),
                    safe_text(observed_status),
                    retry.outcome.value,
                    retry.detail,
                    extra={
                        "request_id": safe_int(pending.media_request_id),
                        "tmdb_id": safe_int(pending.tmdb_id),
                    },
                )
                # The same best-effort refresh ``_resume_one``'s retry takes: files
                # this (or the interrupted original) delete already removed are still
                # advertised by Plex. ``refused`` names an out-of-root path with no
                # section to scan, so it is the one outcome skipped.
                if retry.outcome is not PurgeOutcome.refused:
                    await purge_service.trigger_library_scan(
                        library,
                        library_path=library_path,
                        media_type="tv",
                        context="eviction",
                        extra={
                            "request_id": safe_int(pending.media_request_id),
                            "tmdb_id": safe_int(pending.tmdb_id),
                        },
                    )
                return retry.outcome is PurgeOutcome.partial
            # The remains are gone: take the SAME file-gone finalize below. It is
            # status-safe for every enumerated shape -- it only CASes ``cancelled``
            # -> ``evicted`` (disk truth over an aborted intent) and otherwise
            # leaves the status exactly as it found it, so a pre-grab re-grab keeps
            # searching and a blocked import keeps its surfaced, retryable state
            # (now over a CLEAN directory, so the retry can actually succeed). The
            # registration is held across it so a replacement import starting right
            # after the delete cannot re-stamp the breadcrumb the value-predicated
            # clear is about to match.
            try:
                await _finalize_gone_rearmed_season(
                    session=session,
                    library=library,
                    library_path=library_path,
                    title=title,
                    pending=pending,
                    recovery_note=_REMNANT_PURGE_NOTES.get(
                        observed_status,
                        "the incomplete purge was finished here so a replacement can land",
                    ),
                )
            finally:
                purge_service.end_purge(library_path)
            return True
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
                _safe_title(title),
                safe_int(pending.season_number),
                extra={
                    "request_id": safe_int(pending.media_request_id),
                    "tmdb_id": safe_int(pending.tmdb_id),
                },
            )
        else:
            await session.rollback()
            _logger.info(
                "re-armed season %s moved on before recovery (advanced past the "
                "status it was read at); leaving it to auto-grab/the reconciler",
                safe_int(pending.season_number),
                extra={
                    "request_id": safe_int(pending.media_request_id),
                    "tmdb_id": safe_int(pending.tmdb_id),
                },
            )
        return False

    await _finalize_gone_rearmed_season(
        session=session,
        library=library,
        library_path=library_path,
        title=title,
        pending=pending,
        recovery_note="the file was already gone and the season was re-requested",
    )
    return False


async def _finalize_gone_rearmed_season(
    *,
    session: AsyncSession,
    library: LibraryPort,
    library_path: str,
    title: str | None,
    pending: _SeasonPending,
    recovery_note: str,
) -> None:
    """Finalize ONE re-armed crash-window season whose library tree is now GONE --
    the shared tail of :func:`_recover_rearmed_season`'s two converging paths (the
    purge completed before the crash, or its cancelled re-grab's remains were
    cleared by the retry here; ``recovery_note`` says which, in the history row).

    Records the eviction history the interrupted sweep never wrote, releases the
    breadcrumb, applies the disk-truth status flip, and refreshes Plex.

    The clear is VALUE-predicated on the exact stale path recovery observed: a
    replacement import can commit between the caller's stat and this write,
    stamping a FRESH breadcrumb (and its imported content) onto this very row --
    an unconditional clear would wipe that fresh breadcrumb, leaving a playing
    season with no eviction/report handle. A mismatch means the import owns the
    row now: leave everything, log, done.
    """
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
            safe_int(pending.season_number),
            extra={
                "request_id": safe_int(pending.media_request_id),
                "tmdb_id": safe_int(pending.tmdb_id),
            },
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
                f"an interrupted sweep, {recovery_note} ({library_path})"
            ),
        )
    )
    # The tree is gone: retire the incomplete-delete marker unconditionally, the
    # same backstop the other finalizes keep for a breadcrumb clear that did not
    # match (issues #482 / #485).
    await SqlSeasonRequestRepository(session).clear_partial_delete_path(pending.season_request_id)
    await session.commit()
    await purge_service.trigger_library_scan(
        library,
        library_path=library_path,
        media_type="tv",
        context="eviction",
        extra={"request_id": pending.media_request_id, "tmdb_id": pending.tmdb_id},
    )
    _logger.info(
        "finalized an interrupted eviction of %r season %s: %s",
        _safe_title(title),
        safe_int(pending.season_number),
        recovery_note,
        extra={
            "request_id": safe_int(pending.media_request_id),
            "tmdb_id": safe_int(pending.tmdb_id),
        },
    )


async def _resume_one(
    *,
    session: AsyncSession,
    library: LibraryPort,
    fs: FileSystemPort,
    media_type: Literal["movie", "tv"],
    library_path: str,
    title: str | None,
    partial_delete_path: str | None,
    pending: _Pending,
    qbt: DownloadClientPort | None,
) -> bool:
    """Acquire the path claim atomically, then resume one interrupted purge."""
    season_note = f" season {pending.season_number}" if isinstance(pending, _SeasonPending) else ""
    if not purge_service.begin_purge(library_path):
        _logger.info(
            "deferring recovery of %r%s: another purge owns its path",
            _safe_title(title),
            season_note,
            extra={
                "request_id": safe_int(pending.media_request_id),
                "tmdb_id": safe_int(pending.tmdb_id),
            },
        )
        return False
    try:
        return await _resume_one_claimed(
            session=session,
            library=library,
            fs=fs,
            media_type=media_type,
            library_path=library_path,
            title=title,
            partial_delete_path=partial_delete_path,
            pending=pending,
            qbt=qbt,
        )
    finally:
        purge_service.end_purge(library_path)


async def _resume_one_claimed(
    *,
    session: AsyncSession,
    library: LibraryPort,
    fs: FileSystemPort,
    media_type: Literal["movie", "tv"],
    library_path: str,
    title: str | None,
    partial_delete_path: str | None,
    pending: _Pending,
    qbt: DownloadClientPort | None,
) -> bool:
    """Recover ONE claimed-but-not-finalized eviction — see
    :func:`_resume_interrupted_evictions` for the three-way decision.

    ``partial_delete_path`` is the row's DURABLE incomplete-delete marker as the
    enumeration read it (issues #482 / #485). When it still covers
    ``library_path``, a destructive delete of exactly this tree began and nothing
    proved it left the media intact — a classified partial, a crash mid-``rmtree``,
    a cancelled sweep, or any of those followed by a restart. Such a row is routed
    through the RETRY/converge path, never the restore-to-``available`` path; a
    bare ``os.stat`` success cannot tell an intact tree from a gutted one, which
    is the whole of #485.

    That retry re-checks the hard mutable SAFETY facts -- pin/watchlist intent, a
    competing active transfer, a ride-along coverage claim, another live row on the
    path, and the marker itself -- and DEFERS on any of them, both cheaply up front
    and again inside the delete-boundary hook under the held delete permit. It does
    NOT re-check disk pressure or Plex watch state: finishing an already-started
    delete is convergence, and re-gating it on the conditions that authorized the
    original eviction only strands gutted media (see
    :func:`_recovery_intent_blocked_reason`'s policy comment). It never restores,
    since the tree still cannot be shown complete. A retry that destroys more
    without clearing the remains refreshes Plex best-effort before returning: the
    interrupted original may never have scanned at all, leaving Plex advertising
    files that are already gone.

    Returns whether this recovery DESTROYED data whose freed bytes it cannot
    measure, so the sweep can re-baseline its pressure accounting (its disk
    snapshot is taken before recovery runs)."""
    season_note = f" season {pending.season_number}" if isinstance(pending, _SeasonPending) else ""
    if isinstance(pending, _MoviePending) and pending.correction_recovery:
        if not await _partial_delete_still_armed(session, pending, library_path):
            return False
        culprit = await SqlDownloadRepository(session).find_latest_imported_for_request(
            pending.media_request_id
        )
        if culprit is not None and qbt is None:
            _logger.warning(
                "deferring correction recovery of %r: qBittorrent is unconfigured, so "
                "the culprit torrent cannot be purged safely",
                _safe_title(title),
                extra={
                    "request_id": safe_int(pending.media_request_id),
                    "tmdb_id": safe_int(pending.tmdb_id),
                },
            )
            return False
        if (
            culprit is not None
            and qbt is not None
            and not await purge_service.remove_torrent(
                qbt,
                culprit.torrent_hash,
                context="correction recovery",
                extra={
                    "torrent_hash": culprit.torrent_hash,
                    "request_id": safe_int(pending.media_request_id),
                    "tmdb_id": safe_int(pending.tmdb_id),
                },
            )
        ):
            return False

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
        return False

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
                    _safe_title(title),
                    season_note,
                    extra={
                        "request_id": safe_int(pending.media_request_id),
                        "tmdb_id": safe_int(pending.tmdb_id),
                    },
                )
            # The path belongs to that row now (its own import stamped it), so
            # this row's incomplete-delete marker is spent -- released together
            # with the breadcrumb by the CAS above.
            return False
        if not _partial_delete_outstanding(partial_delete_path, library_path):
            # The claim committed but the purge never completed AND nothing on the
            # row says a destructive delete ever started: the file is still
            # watchable, so restore 'available' (+ the re-grab reconciliation) and
            # let THIS sweep's normal candidate assembly re-decide the eviction
            # fresh -- through the standard claim -> purge path, or not at all if
            # the pressure that justified it is gone.
            _logger.info(
                "resuming an interrupted eviction of %r%s: the file is still on disk; "
                "restored to 'available' for a fresh sweep decision",
                _safe_title(title),
                season_note,
                extra={
                    "request_id": safe_int(pending.media_request_id),
                    "tmdb_id": safe_int(pending.tmdb_id),
                },
            )
            await _restore_after_failed_delete(session, pending)
            return False
        # An OUTSTANDING delete of exactly this path (issues #482 / #485): the
        # path stats fine, but the row durably says a destructive delete began
        # here and nothing proved it left the media intact -- a classified
        # partial, a crash or cancellation mid-``rmtree``, or any of those plus a
        # restart. ``os.stat`` cannot tell the difference (a gutted directory
        # stats exactly like a whole one), so restoring is exactly the wrong move.
        # Finish the purge instead -- idempotent, and the only retry an 'evicted'
        # row can ever get (it is invisible to candidate assembly). Anything short
        # of a clean delete keeps the claim and the breadcrumb for the next sweep,
        # never a restore over a tree that cannot be shown complete. The purge
        # registration is HELD across the finalize below for the same reason
        # ``_evict_one`` holds it: a same-row replacement import starting after
        # the delete could otherwise re-place this exact path and re-stamp the
        # breadcrumb the value-predicated clear is about to match, erasing a live
        # import's only handle.
        #
        # The safety facts that can revoke this convergence (pin/watchlist intent,
        # a competing active transfer, a ride-along coverage claim, the path and
        # the marker itself -- see the RECOVERY POLICY comment above
        # :func:`_recovery_intent_blocked_reason` for what is deliberately NOT
        # re-checked and why) are read TWICE: cheaply here, so
        # an already-revoked row never pays for the primitive's preflight probes
        # and gets an honest log, and again inside the delete-boundary hook, which
        # runs under the held delete permit with only a synchronous worker start
        # left after it. The early read alone is a TOCTOU hole exactly like the one
        # ``_still_evictable`` is to ``_evict_one``'s claim CAS (issue #515): an
        # operator pin committed while this coroutine awaits the two preflight
        # probes or queues for a permit would otherwise be read, ignored, and the
        # pinned title's remaining files deleted anyway.
        blocked = await _recovery_purge_blocked_reason(session, pending)
        if blocked is not None:
            _logger.info(
                "deferring the incomplete-delete retry of %r%s: %s; keeping the claim, "
                "its breadcrumb and its marker rather than deleting under it",
                _safe_title(title),
                season_note,
                blocked,
                extra={
                    "request_id": safe_int(pending.media_request_id),
                    "tmdb_id": safe_int(pending.tmdb_id),
                },
            )
            return False
        try:
            retry = await purge_service.purge_library_path(
                fs,
                library_path,
                hold_purge_registration=True,
                before_delete=_recovery_delete_boundary(session, pending, library_path),
            )
        except _RecoveryPurgeRevoked as revoked:
            _logger.info(
                "revoked the incomplete-delete retry of %r%s at the delete boundary: %s; "
                "no delete started, so the claim, its breadcrumb and its marker all stand",
                _safe_title(title),
                season_note,
                revoked.reason,
                extra={
                    "request_id": safe_int(pending.media_request_id),
                    "tmdb_id": safe_int(pending.tmdb_id),
                },
            )
            return False
        if retry.outcome is not PurgeOutcome.deleted:
            _logger.warning(
                "retrying the incompletely deleted eviction of %r%s did not clear the "
                "remains (%s: %s); keeping the claim and its breadcrumb rather than "
                "restoring media that cannot be shown to be complete",
                _safe_title(title),
                season_note,
                retry.outcome.value,
                retry.detail,
                extra={
                    "request_id": safe_int(pending.media_request_id),
                    "tmdb_id": safe_int(pending.tmdb_id),
                },
            )
            # Best-effort Plex refresh (Codex round-3 P2), the same one
            # ``_evict_one``'s direct partial-outcome branch takes: an armed marker
            # means a destructive delete began on this path and was never shown to
            # have left it intact, so Plex may well be advertising entries that are
            # already gone -- most sharply after a crash, whose original attempt
            # never reached any scan at all. ``refused`` is the one outcome skipped:
            # it names a path outside every configured library root, so there is no
            # section to refresh. The DB state (claim + breadcrumb + marker) is
            # untouched by the scan, so the retry path stays exactly as it was.
            if retry.outcome is not PurgeOutcome.refused:
                await purge_service.trigger_library_scan(
                    library,
                    library_path=library_path,
                    media_type=media_type,
                    context="eviction",
                    extra={"request_id": pending.media_request_id, "tmdb_id": pending.tmdb_id},
                )
            return retry.outcome is PurgeOutcome.partial
        destroyed_unmeasured = True  # the retry deleted real bytes it never counted
        purge_held = True
        recovery_note = "the incomplete purge was finished here"
    else:
        purge_held = False  # nothing was deleted here, so nothing is registered
        destroyed_unmeasured = False
        recovery_note = "the file was already gone"

    # The purge completed but the finalize never ran (crash after the delete, or
    # a legacy eviction predating breadcrumb-clearing) -- or the retry above just
    # cleared a partial delete's remains: finalize now, identically. The
    # CAS-clear is the single-winner gate -- only the pass that actually clears
    # the breadcrumb writes the history row, so two concurrent resumes (or a
    # resume racing the sweep that just finalized) never double-record -- and it
    # is VALUE-predicated on the exact stale path observed, so a replacement
    # import re-stamping this row mid-recovery keeps its fresh breadcrumb.
    try:
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
                mark_removed=pending.correction_recovery,
            )
        if not cleared:
            # A concurrent pass finalized first, or a replacement import owns the
            # row's breadcrumb now -- honor it either way.
            await session.rollback()
            return destroyed_unmeasured
        if isinstance(pending, _SeasonPending):
            # DISK-TRUTH-OVER-INTENT (see _recover_rearmed_season's twin): a season
            # re-armed then CANCELLED between this row's enumeration and now would
            # otherwise finalize as 'cancelled' -- invisible to ``evicted_seasons``,
            # letting a re-request mint 'available' off stale Plex over the
            # just-deleted file. The file is genuinely gone; flip to 'evicted' (CAS
            # from {cancelled} only -- every other status is left exactly as the
            # normal finalize leaves it).
            await season_request_service.set_status_if_in(
                session,
                media_request_id=pending.media_request_id,
                season_request_id=pending.season_request_id,
                status=RequestStatus.evicted.value,
                allowed_from=frozenset({RequestStatus.cancelled.value}),
                tolerate_active_conflict=True,
            )
        else:
            # DISK-TRUTH-OVER-INTENT for SETTLED movie correction rows (#513):
            # ``cancelled`` says the replacement intent stopped and ``failed`` says
            # it exhausted, but recovery has now proved the old file is GONE. Neither
            # status is recognized by ``latest_request_evicted``, so preserving it
            # would let an immediate re-request trust eventually-consistent Plex
            # presence and mint ``available`` over no file and no download. Publish
            # ``evicted`` as the durable stale-Plex guard for those two settled
            # outcomes, matching the normal movie eviction finalize. Active/retryable
            # replacement statuses stay unchanged.
            await SqlRequestRepository(session).set_status_if_in(
                pending.media_request_id,
                RequestStatus.evicted.value,
                frozenset(
                    {
                        RequestStatus.cancelled.value,
                        RequestStatus.failed.value,
                    }
                ),
            )
        session.add(
            DownloadHistory(
                tmdb_id=pending.tmdb_id,
                torrent_hash=None,
                event_type=DownloadHistoryEvent.evicted,
                source_title=title,
                message=(
                    f"eviction finalized{season_note}: recovered after an interrupted "
                    f"sweep, {recovery_note} ({library_path})"
                ),
            )
        )
        # The tree is gone: retire the incomplete-delete marker unconditionally,
        # the same backstop ``_evict_one``'s finalize keeps for the case where the
        # value-predicated breadcrumb clear above did not match (issues #482/#485).
        await _disarm_partial_delete(session, pending)
        await session.commit()
    finally:
        if purge_held:
            purge_service.end_purge(library_path)
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
        "finalized an interrupted eviction of %r%s: %s",
        _safe_title(title),
        season_note,
        recovery_note,
        extra={
            "request_id": safe_int(pending.media_request_id),
            "tmdb_id": safe_int(pending.tmdb_id),
        },
    )
    return destroyed_unmeasured


async def _evict_one(
    *,
    session: AsyncSession,
    fs: FileSystemPort,
    library: LibraryPort,
    candidate: EvictionCandidate,
    pending: _Pending,
    grace_cutoff: datetime,
    note_unmeasured_free: Callable[[], None] | None = None,
) -> EvictionOutcome | None:
    """Claim (status CAS) + delete + log ONE selected candidate, in that order
    (#67): the atomic claim runs BEFORE the delete, so nothing is deleted until
    the row is won, and a failed delete restores the claim. ``None`` means it was
    skipped (logged honestly), never a silent success.

    The invariant set this ordering + the request-side guard together uphold:

    1. A pinned file is NEVER deleted -- the claim CAS folds the ``keep_forever``
       pin into its predicate (``require_unpinned`` / ``require_parent_unpinned``)
       and runs before any ``fs.delete``, so a pin landing after candidate
       assembly makes the claim match zero rows. This is a WHOLE-MODULE contract,
       not a claim-path one (issue #515): ``POST /requests/{id}/keep-forever`` has
       no status restriction, so an operator can pin a row that is already
       ``evicted`` -- and the recovery pass's marker-owned force-purge has no
       claim CAS ahead of it to fold the pin into. Recovery therefore re-reads the
       pin (and the watchlist) itself, at the delete boundary under the held
       delete permit, and revokes the purge rather than deleting a pinned title's
       remaining files (:func:`_recovery_delete_boundary`).
    2. A failed/refused delete NEVER strands a terminal-status row over a live
       file -- ``_restore_after_failed_delete`` compare-and-swaps the row back
       ``evicted`` -> ``available`` (recomputing the TV parent rollup). Its
       precondition is that the file is INTACT, which is why the purge
       primitive distinguishes an untouched failure from a PARTIAL one (issue
       #482): a tree the delete already ate into is not a live file, and
       restoring it would publish media the UI calls watchable and cannot play.
       A partial delete therefore keeps the claim instead (see the table below).
       Because a crash / cancellation / restart can stop an ``rmtree`` halfway
       with NO in-band result to classify (issue #485), that distinction is also
       recorded DURABLY on the claim row -- ``partial_delete_path`` is armed in
       the claim's own commit, before the delete, and disarmed only by an outcome
       that proves the tree intact-or-gone. Recovery reads the row, not process
       memory, so every restart/cancellation boundary sees the same truth.
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
    7. A shared breadcrumb NEVER gets deleted out from under its OTHER live owner
       (#155, "twins"): two ``available`` rows can legitimately share one exact
       ``library_path`` (remove-then-reacquire's pre-existing leftover, or the
       #148 force-reacquire shape), and the CLAIM above only compares THIS row's
       own status/pin -- it has no way to see the sibling. After the claim
       commits but BEFORE the delete, :func:`_path_claimed_by_another_row`
       (the same check crash-recovery already runs) is consulted; a live sibling
       claim releases the claim back to ``available`` (the ordinary
       purge-refused restore path) instead of deleting, so BOTH rows are decided
       fresh by later sweeps rather than one twin's delete silently orphaning
       the other.
    8. A rewatch during the sweep NEVER gets deleted (#209): candidate assembly
       (:func:`_movie_candidates`/:func:`_season_candidates`) reads Plex watch
       state once, up front, for the WHOLE candidate pool -- a single batched
       section crawl per media type (:meth:`LibraryPort.resolve_watch_states`,
       issues #213/#238), not a fresh re-crawl per candidate. Immediately BEFORE
       the claim, watch state is
       RE-READ for just this one candidate and re-checked against the watched +
       grace-cutoff predicate; anything now unwatched, too-recently-viewed, no
       longer path-correlated (zero/ambiguous, see issue #207), or erroring on
       the re-read is skipped with an honest log rather than claimed. This
       narrows the rewatch race to the short window between the re-read and the
       claim/delete immediately below -- it does not (and cannot) eliminate it.
       Both this re-read and the assembly read above are issue #207
       path-correlated: a duplicate elsewhere in the library can never stand in
       for THIS candidate's own file.

    Full lifecycle state table, from the moment the claim COMMITS (row
    ``evicted``, breadcrumb set, file on disk); every permutation of
    [crash | re-request | cancel | purge-fail | purge-ok] lands in one row:

    ========================================  =======================================
    event after the claim commit              end state
    ========================================  =======================================
    purge ok                                  ``evicted``, breadcrumb CLEARED,
                                              history row, Plex refreshed (finalized)
    purge refused / error (tree UNTOUCHED)    restored ``available`` (+ TV rollup);
                                              breadcrumb kept; retried next sweep
    purge PARTIAL -- some of the tree was     claim KEPT (``evicted`` + breadcrumb,
    removed, then the delete failed (#482)    the "still in progress" shape); NOT
                                              restored (the media is incomplete);
                                              no re-grab cancelled; a retried purge
                                              of the remains converges to deleted;
                                              REPORTED as a ``partial`` outcome so
                                              callers publish/refresh on it
    a re-armed season's replacement import    recovered next sweep: the remains are
    is BLOCKED by the surviving remnants      PURGED by the marker's owner (an import
    (``import_blocked`` + armed marker)       may not overwrite them), status left
                                              alone so the retry can now succeed
    crash, file still present, NO armed        resumed next sweep: restored
    incomplete-delete marker                   ``available``, re-decided fresh
    crash / cancellation mid-delete -- the      resumed next sweep: the armed marker
    marker armed with the claim is still on     routes it to a RETRY of the purge,
    the row (#485)                              never a restore; converges to the
                                                finalize once the remains clear
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
    THIS claim's own path is ALSO claimed by   restored ``available`` (+ TV rollup);
    another live (available/in-flight) row --  breadcrumb kept; NOTHING deleted --
    the shared-breadcrumb twins shape (#155)   invariant #7; the sibling is decided
                                              fresh by its own later sweep pass
    a pack's active ride-along coverage claim   restored ``available`` (+ TV rollup);
    over this season lands AFTER the pre-claim   NOTHING deleted (#465) -- re-checked
    read (during the watch-state re-read) --    post-claim/pre-delete, same shape as
    the scopeless-ride-along shape (#465)        the twins guard; the pack keeps its file
    that same coverage claim lands, then the    recovery re-checks coverage before its
    process EXITS before the post-claim guard   force-purge and DEFERS: claim, breadcrumb
    can run                                     and marker all stand, nothing deleted,
                                                re-decided once the pack settles
    an operator PINS the title (or watchlists   recovery REVOKES its force-purge at the
    it) while recovery is already inside its    delete boundary -- under the held delete
    force-purge's preflight/permit wait         permit, before the worker starts -- so
    (#515)                                      nothing is deleted and the pin holds
    disk pressure clears, or Plex reports a     recovery still FINISHES the purge: the
    rewatch, while a marker-owned delete is     marker says bytes already left, so
    outstanding (#515)                          convergence is the only honest end state
                                                (deferring would strand gutted media)
    rewatch (or watch-state error) lands       skipped BEFORE the claim (invariant
    between candidate assembly and the claim   #8, #209); row stays ``available``,
    (#209)                                     file untouched, re-decided next sweep
    ========================================  =======================================

    ``note_unmeasured_free`` is the sweep's re-baseline hook: invoked exactly when
    this eviction DESTROYED data whose freed byte count is unknowable (a partial
    delete -- the reclaimable-bytes measurement necessarily precedes the delete,
    and a delete that got part-way frees an unmeasured subset of it). The sweep
    uses it to re-stat the root before choosing further victims, so a partial
    delete that already relieved the pressure cannot make it keep deleting watched
    titles against a snapshot taken before that destruction. ``None`` (the default)
    for callers with no pressure ledger to keep.
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

    season_note = f" season {safe_int(candidate.season)}" if candidate.season is not None else ""

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

    # Rewatch-during-sweep re-read (#209, invariant #8): _still_evictable above
    # only re-checks DB status/pin/in_flight -- it never re-consults Plex, so a
    # rewatch that lands DURING the sequential sweep (candidate assembly ran a
    # Plex call per candidate, potentially long ago for a late candidate) would
    # otherwise still be deleted on its now-stale "watched" verdict. Re-read
    # path-correlated (#207) watch state for THIS candidate only, immediately
    # before the claim, and re-apply the same watched + grace-cutoff predicate
    # domain._is_eligible uses (inlined here since this function already holds
    # ``grace_cutoff`; pin/status/in_flight are NOT re-checked again -- that is
    # _still_evictable's and the claim CAS's job, not this one's).
    try:
        if isinstance(pending, _SeasonPending):
            fresh = await library.watch_state(
                pending.tmdb_id, "tv", season=pending.season_number, library_path=library_path
            )
        else:
            fresh = await library.watch_state(pending.tmdb_id, "movie", library_path=library_path)
    except (PlexLibraryError, PlexAuthError, NotImplementedError) as exc:
        _logger.warning(
            "skipping eviction of %r%s: could not re-read Plex watch state before the "
            "claim (%s); failing closed rather than deleting on unknown state",
            candidate.title,
            season_note,
            type(exc).__name__,
            extra={"request_id": pending.media_request_id, "tmdb_id": pending.tmdb_id},
        )
        return None
    if not (
        fresh.watched and fresh.last_viewed_at is not None and fresh.last_viewed_at < grace_cutoff
    ):
        _logger.info(
            "skipping eviction of %r%s: Plex watch state changed since candidate assembly "
            "(now unwatched, recently rewatched, or no longer path-correlated); re-checked "
            "immediately before the claim",
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
            require_not_watchlisted=True,
            tolerate_active_conflict=True,
        )
    else:
        claimed = await SqlRequestRepository(session).set_status_if_in(
            pending.media_request_id,
            RequestStatus.evicted.value,
            frozenset({RequestStatus.available.value}),
            require_unpinned=True,
            require_not_watchlisted=True,
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
    #
    # ARM the durable incomplete-delete marker in this SAME commit (issues #482 /
    # #485). ``shutil.rmtree`` is not atomic, and the ways a delete can stop
    # halfway through a tree are not all in-band: a classified
    # ``PurgeOutcome.partial`` is, but a crash, a cancelled sweep (whose worker
    # settles unobserved -- cancellation wins over the classified result), and a
    # process restart are not. So the marker records the delete as OUTSTANDING
    # before it starts and is cleared only by an outcome that PROVES the tree
    # needs no further reclaiming; anything that ends without such proof leaves
    # the row honestly saying "this media cannot be assumed complete", which is
    # exactly what stops the next sweep's recovery restoring gutted media to
    # 'available' off a bare ``os.stat``.
    await session.commit()

    # Shared-breadcrumb-twins guard (#155): the claim above only compares THIS
    # row's own status/pin -- it does nothing to notice that some OTHER live
    # (available/in-flight) row ALSO carries this exact ``library_path``
    # breadcrumb (reachable via remove-then-reacquire, or one click away via the
    # #148 force re-acquire leaving the old row's breadcrumb untouched while the
    # new row's import stamps the same deterministic path). Deleting now would
    # rip the file out from under that other row's still-live claim, leaving it
    # dishonestly ``available`` until a later sweep's own candidate assembly
    # notices the file is gone and self-heals it -- exactly the crash-recovery
    # pass's finalized-vs-interrupted discriminator
    # (:func:`_path_claimed_by_another_row`), reused here BEFORE the delete
    # instead of only after a crash. Caught, this claim is simply released the
    # same way a refused/erroring delete is: restored to 'available' (+ the
    # normal re-grab reconciliation, a no-op here since nothing re-grabbed) so
    # THIS row is never stranded 'evicted' over a file that never left, and the
    # sibling's own next sweep pass decides it fresh.
    if await _path_claimed_by_another_row(session, library_path, pending):
        await _restore_after_failed_delete(session, pending)
        _logger.warning(
            "skipping eviction of %r%s: another live row already claims this exact "
            "library_path (%s) -- restored to 'available' rather than deleting a "
            "file a sibling row still owns",
            candidate.title,
            season_note,
            library_path,
            extra={"request_id": pending.media_request_id, "tmdb_id": pending.tmdb_id},
        )
        return None

    # Ride-along coverage-claim guard (#465), the AUTHORITATIVE counterpart of the
    # cheap :func:`_still_evictable` read above -- structurally the same shape as
    # the twins guard just before it. The claim CAS compares only THIS season
    # row's own status/pin/watchlist; it can no more see a pack's active
    # ``download_coverage_claims`` row (a scopeless ride-along, invisible to
    # ``find_active_for_request``) than it can see a sibling's breadcrumb. A pack
    # can commit that claim AFTER _still_evictable read clean -- most of all during
    # the awaited Plex watch-state re-read just above -- so a pre-claim read alone
    # is a TOCTOU hole. Re-read it here, AFTER the claim committed ``evicted`` and
    # BEFORE the delete, off the same committed snapshot: a claim now present means
    # a live pack is re-fetching this season, so release the claim (restore to
    # 'available', the identical no-op-regrab path a refused delete takes) rather
    # than delete a file the pack is mid-transfer. Movies never hold a claim, so
    # this is a season-only check. Not atomically foldable into the status CAS (the
    # claim lives in a different table on a different key), and even a folded
    # predicate could not cover a claim landing between the CAS and the unavoidable
    # post-commit filesystem delete -- so this mirrors the module's established
    # post-claim/pre-delete recheck-and-restore pattern (twins #155, rewatch #209),
    # narrowing the window to the same irreducible micro-window those do.
    if await _coverage_claim_active(session, pending):
        await _restore_after_failed_delete(session, pending)
        _logger.warning(
            "skipping eviction of %r%s: a live pack holds an active ride-along "
            "coverage claim over this season (landed after the pre-claim re-check) "
            "-- restored to 'available' rather than deleting a file a pack is fetching",
            safe_text(candidate.title),
            season_note,
            extra={
                "request_id": safe_int(pending.media_request_id),
                "tmdb_id": safe_int(pending.tmdb_id),
            },
        )
        return None

    # THE MARKER-ARM SITE (issues #482 / #485 / #515). Handed to the purge
    # primitive as its durable delete-boundary hook, so it runs after both
    # read-only preflight probes, after one of the delete-worker permits is HELD,
    # and immediately before the synchronous worker start -- i.e. the marker
    # records "a delete of this path is starting", not "a delete was authorized",
    # which is the distinction #515 exists to draw.
    #
    # TWO RESIDUAL WINDOWS, stated rather than papered over:
    #
    # * ``Thread.start()`` can still fail AFTER this commit (thread exhaustion),
    #   leaving an armed marker over a provably intact tree. That one is
    #   TRACTABLE and is compensated below: the primitive raises the distinct
    #   ``DeleteWorkerStartError`` for exactly this case and the marker is
    #   disarmed in a follow-up commit.
    # * A cancellation delivered DURING ``session.commit()`` here is an ambiguous
    #   commit -- the write may or may not have landed, and no amount of ordering
    #   makes a single-statement commit and an in-memory record of it atomic.
    #   This is deliberately NOT chased. The containment is recovery's own
    #   delete-boundary re-checks (:func:`_recovery_delete_boundary`): a marker
    #   armed over an intact tree is converged, not restored, but only after the
    #   pin/watchlist, competing-ownership, path and marker facts are all re-read
    #   under the held delete permit -- so the failure mode of this sliver is a
    #   too-eager cleanup of media the operator has not protected, never the
    #   deletion of media they have. The opposite ordering (commit after the
    #   delete) trades that for the far worse one #485 documents: gutted media
    #   silently restored to ``available``.
    delete_marker_committed = False

    async def _mark_delete_started() -> None:
        nonlocal delete_marker_committed
        await _arm_partial_delete(session, pending, library_path)
        await session.commit()
        delete_marker_committed = True

    # Hardlink-aware reclaimable-bytes measurement + the root-guarded delete are
    # both done by the shared ``purge_service.purge_library_path`` primitive
    # (ADR-0014, "reuse don't duplicate") -- same accounting-before-delete order
    # (a file's link count is only readable while it still exists, R4-6, ADR-0012),
    # same containment guard, same idempotent already-gone no-op. Eviction keeps
    # its OWN log message + logger for each outcome (the primitive classifies, the
    # caller logs).
    purge_held = False
    try:
        purge = await purge_service.purge_library_path(
            fs,
            library_path,
            hold_purge_registration=True,
            before_delete=_mark_delete_started,
        )
    except purge_service.DeleteWorkerStartError:
        # No delete ran and none ever will for this attempt, so the marker armed a
        # moment ago is now a false statement about an intact tree. Compensate it
        # and let the failure keep propagating (honesty over silence -- the sweep's
        # per-candidate guard logs it and the row is re-decided next sweep, exactly
        # as it was before this hook existed).
        if delete_marker_committed:
            await _disarm_unstarted_delete(session, pending, candidate.title, season_note)
        raise
    if purge.outcome is PurgeOutcome.deleted:
        purge_held = True
    if purge.outcome is not PurgeOutcome.deleted:
        if purge.outcome is PurgeOutcome.deferred:
            # The deferral is decided BEFORE any destructive work starts (the
            # placement-conflict check precedes the delete), so nothing left disk
            # and the marker armed above must come back off -- otherwise recovery
            # would later force-purge the very content the import is placing,
            # instead of restoring the row once that import settles.
            await _disarm_partial_delete(session, pending)
            await session.commit()
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
        if purge.outcome is PurgeOutcome.partial:
            # The recursive delete removed SOME of the tree and then failed
            # (issue #482). Everything below this branch assumes the media is
            # untouched and still watchable -- here it demonstrably is not, so
            # the restore is exactly the wrong move: it would put the row back
            # to 'available' over a tree missing files and let the UI claim
            # media it can no longer play, silently and forever. The eviction is
            # instead still IN PROGRESS, which is the same shape as the
            # ``deferred`` branch above: the committed 'evicted' claim and its
            # breadcrumb both STAY, so the remains stay reclaimable and a
            # retried purge (idempotent -- an already-gone entry is a no-op)
            # converges to 'deleted' once whatever blocked the removal clears.
            # For the same reason no re-grab is cancelled: unlike a restore,
            # this file really did leave, so an in-window re-request re-fetching
            # it is legitimate and its import re-places the path. The durable
            # incomplete-delete marker armed with the claim above simply STAYS
            # armed -- this is the one outcome that proves nothing, so recovery
            # FINISHES the purge off the kept breadcrumb rather than reading the
            # surviving directory as intact media (see
            # :func:`_partial_delete_outstanding`). Keeping the claim is only safe
            # because the next sweep honors that marker.
            if note_unmeasured_free is not None:
                # Bytes really did leave disk, but the primitive cannot say how
                # many (it measured the tree before the delete and the delete only
                # got part-way). Tell the sweep so it re-baselines its pressure
                # accounting instead of continuing to pick victims against a
                # snapshot that predates this destruction (issue #482 review).
                note_unmeasured_free()
            _logger.warning(
                "eviction of %r%s deleted only PART of the tree before failing (%s); "
                "leaving the eviction claim and its breadcrumb -- the media is no "
                "longer complete, so it is NOT restored to 'available'",
                safe_text(candidate.title),
                season_note,
                purge.detail,
                extra={
                    "request_id": safe_int(pending.media_request_id),
                    "tmdb_id": safe_int(pending.tmdb_id),
                },
            )
            # Best-effort Plex refresh (Codex P2): the episodes the delete DID
            # remove are gone from disk but still advertised by Plex, so without a
            # scan users hit playback failures on entries the library still lists.
            # Same posture as every other scan here -- the DB state already stands,
            # and Plex catches up on its next scheduled scan if this fails. The
            # breadcrumb and claim are untouched by it, so the retry path is intact.
            await purge_service.trigger_library_scan(
                library,
                library_path=library_path,
                media_type=candidate.media_type,
                context="eviction",
                extra={"request_id": pending.media_request_id, "tmdb_id": pending.tmdb_id},
            )
            # Report it as an OUTCOME, not ``None`` (Codex round-4 P2): the row is
            # committed ``evicted`` and files really did leave disk, so returning
            # nothing would make ``POST /ops/evict`` answer an empty list with no
            # error and make BOTH sweep callers skip their realtime invalidation --
            # clients would keep showing the title as watchable. ``freed_bytes`` is
            # honestly ``None`` (the measurement necessarily predates the delete and
            # the delete only got part-way), and ``partial`` marks it as still
            # having reclaimable remains so the periodic loop's summary can say so.
            return EvictionOutcome(
                request_id=candidate.request_id,  # the same id the success path reports
                media_type=candidate.media_type,
                title=candidate.title,
                season=candidate.season,
                library_path=library_path,
                freed_bytes=None,
                partial=True,
            )
        # The delete refused (a stale/misconfigured breadcrumb pointing outside
        # every currently-configured library root) or errored (permission/I/O)
        # with the tree provably untouched. The file is STILL on disk and still
        # watchable -- so RESTORE the row to 'available' (#67): a failed unlink
        # must never strand an 'evicted' status over a live file (a re-request
        # would then re-grab content that never left). Never silently skipped,
        # never mis-deleted; a later sweep retries.
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
        # The tree is GONE, so the incomplete-delete marker armed with the claim
        # has nothing left to describe (issues #482 / #485). The breadcrumb clears
        # above already null it when their value-predicated CAS matches; this is
        # the unconditional backstop for the case where it did NOT (a replacement
        # import re-stamped the row mid-purge and owns its breadcrumb now) --
        # leaving a marker armed there would let a later eviction of the
        # replacement content skip its restore.
        await _disarm_partial_delete(session, pending)
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
    grace_cutoff: datetime | None = None,
) -> list[EvictionCandidate]:
    """Assemble every ``available`` movie / TV-season under ``root_path`` into a
    RAW :class:`~plex_manager.domain.eviction.EvictionCandidate` list -- fresh
    Plex watch state + an on-disk size per row -- with NO grace or eligibility
    filtering applied to the RETURNED set. The caller decides what to filter.

    This is the raw superset behind BOTH :func:`preview_candidates` (which simply
    ranks it) and the retention-telemetry sweep (which needs the ranked /
    would-evict subsets AND every row with any recorded view -- including a
    started-but-unfinished season the eligibility filter drops -- from one read).
    Read-only: never deletes, never flips a status, never writes history.

    ``grace_cutoff`` (issue #304) is an ORTHOGONAL, purely-internal walk-skip
    optimization -- NOT a filter on the returned list. When given, a row that
    :func:`~plex_manager.domain.eviction.is_evictable` already rules out at this
    cutoff (pinned, in-flight, unwatched, or still within grace) skips the
    expensive ``os.walk`` size lookup and reports the honest ``size_percent=0.0``
    fallback instead; it still appears in the returned superset exactly like
    every other row (see :func:`_movie_candidates`/:func:`_season_candidates`),
    so the retention-telemetry time-to-watch dataset -- which explicitly wants
    started-but-unfinished/unwatched/pinned rows -- is never silently narrowed.
    ``None`` (the default) walks every row unconditionally, exactly like before
    this optimization existed.

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
        await _movie_candidates(session, library, root_total_bytes, root_path, scope, grace_cutoff)
        if media_type == "movie"
        else await _season_candidates(
            session, library, root_total_bytes, root_path, scope, grace_cutoff
        )
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
        # NFS/SMB mount. Use the shared abandonable substrate so a wedged probe
        # cannot strand CPython's joined default executor past bounded shutdown.
        disk = await purge_service.run_abandonable_probe(
            lambda: read_disk_usage(root_path),
            root_path,
            operation_name="eviction preview disk-usage probe",
        )
    except OSError as exc:
        _logger.warning(
            "eviction candidate preview skipped for %s root %s (%s)",
            media_type,
            root_path,
            type(exc).__name__,
        )
        return []

    # Computed BEFORE assembly (issue #304) so it can be threaded straight into
    # ``assemble_candidates``'s walk-skip optimization -- this is the exact
    # cutoff ``rank_eviction_candidates`` below re-applies, so a row the walk
    # skipped is always exactly a row this ranking excludes anyway.
    grace_cutoff = datetime.now(UTC) - timedelta(days=grace_days)
    candidates = await assemble_candidates(
        session=session,
        library=library,
        media_type=media_type,
        root_path=root_path,
        root_total_bytes=disk.total_bytes,
        all_roots=all_roots,
        grace_cutoff=grace_cutoff,
    )
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
    qbt: DownloadClientPort | None = None,
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
    pressure pre-check so non-destructive restore/finalize recovery never waits for
    disk pressure; marker-gated destructive retries preserve the current pressure
    and eligibility predicate.

    SERIALIZED in-process (:data:`_sweep_latch`): only one sweep runs at a
    time. A second invocation while one is in flight — the manual
    ``POST /ops/evict`` button landing mid-tick, or vice versa — no-ops with a
    log line and returns ``[]``: overlapping sweeps would only re-select from
    the same candidate pool toward the same target, and every cross-sweep race
    class (double-claim, recovery racing a mid-purge claim) exists ONLY when
    they overlap. The skipped invocation's work is not lost — the in-flight
    sweep is already doing it, and the periodic tick retries every interval.
    "In flight" includes a CANCELLED sweep still unwinding: DURING ORDINARY
    OPERATION the ``finally`` below does not clear the latch until any purge
    delete this sweep started has genuinely settled on disk (issue #128), so a
    second invocation landing right after cancellation still correctly no-ops
    instead of racing crash-recovery against a delete that is still physically
    running.

    CAVEAT (issue #421 / #431): the ``_ACTIVE_PURGE_PATHS`` purge-vs-placement
    race this latch is often discussed alongside is now CLOSED even at process
    shutdown — :func:`~plex_manager.services.purge_service.purge_library_path`
    holds its path registration until the delete worker PHYSICALLY completes,
    including through abandonment (see its docstring). ``_sweep_latch`` itself,
    however, is a coarser in-process serialization flag, NOT a physical-
    completion tracker: at process shutdown
    :func:`~plex_manager.services.purge_service.abandon_active_settlements`
    (PR #406) can force the purge delete's shielded settlement to resolve
    without the daemon ``shutil.rmtree`` thread ever finishing, and this sweep's
    cancellation then unwinds through the ``finally`` below the same way it
    would after a real settlement — clearing ``_sweep_latch`` before the
    abandoned delete has physically stopped touching disk. That residual is a
    separate, ACCEPTED, NOT YET CLOSED gap (issue #451): it is reachable only
    inside the bounded shutdown wait on the way to process exit, and issue
    #128's crash-recovery sweep on the *next* startup reconciles whatever
    partial disk state an abandoned delete left behind, exactly as after a
    hard crash mid-delete. Closing the purge-path race did not (and was not
    meant to) make ``_sweep_latch`` track physical completion. See
    ``tests/web/test_shutdown_wait.py`` for the purge-side regression proving
    the ``_ACTIVE_PURGE_PATHS`` window is closed.
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
            qbt=qbt,
        )
    finally:
        _sweep_latch["busy"] = False


async def run_correction_recovery_sweep(
    *,
    session: AsyncSession,
    library: LibraryPort,
    fs: FileSystemPort,
    media_type: Literal["movie", "tv"],
    root_path: str,
    all_roots: Sequence[str] | None = None,
    qbt: DownloadClientPort | None = None,
) -> None:
    """Converge one root's INTERRUPTED OPERATOR CORRECTIONS and nothing else.

    ``eviction_enabled`` is the RETENTION bot's master switch, not a switch over
    the operator's own corrections. A report-issue purge that crashed mid-delete
    leaves remnants at a DETERMINISTIC destination, so the replacement import
    fails (``import_blocked``) against them on every retry -- and with the bot off,
    the periodic tick used to return before recovery ever ran, leaving that
    deadlock to be broken only by re-enabling eviction or hitting an unrelated
    manual action. That is exactly the terminal north star #1 forbids, so the tick
    routes through here instead (``web/app.py``'s ``_eviction_tick_leased``).

    This is :func:`_resume_interrupted_evictions` with ``correction_only=True`` and
    NOTHING else: no candidate assembly, no pressure gate, no proactive pass, no
    retention telemetry, no eviction -- hence no outcomes to return. Rows retention
    claimed for itself are skipped (see that function), so a disabled bot never
    finishes a delete the operator disabled it to stop.

    Keeps two of the sweep's preconditions, for the same reasons:

    * ``_sweep_latch``. Recovery must never run beside a sweep that could be
      mid-purge on the same path; a skipped pass is retried next tick.
    * The root disk stat. An unreadable root (missing mount) must never let a
      breadcrumb's ``os.stat`` read as "gone" and be finalized -- skipped and
      logged, never a crash. The reading itself is unused here: nothing in this
      pass reasons about pressure.

    An unconfigured qBittorrent (``qbt=None``) is honest, not fatal: recovery
    defers any correction whose culprit torrent must be removed first and retries
    on a later tick (see :func:`_resume_one_claimed`).
    """
    if _sweep_latch["busy"]:
        _logger.info(
            "correction recovery skipped for %s root %s: a sweep is already in "
            "progress (it runs this same recovery pass)",
            media_type,
            root_path,
        )
        return
    _sweep_latch["busy"] = True
    try:
        try:
            await purge_service.run_abandonable_probe(
                lambda: read_disk_usage(root_path),
                root_path,
                operation_name="correction recovery root probe",
            )
        except OSError as exc:
            _logger.warning(
                "correction recovery skipped for %s root %s (%s)",
                media_type,
                root_path,
                type(exc).__name__,
            )
            return
        await _resume_interrupted_evictions(
            session=session,
            library=library,
            fs=fs,
            qbt=qbt,
            media_type=media_type,
            root_path=root_path,
            all_roots=all_roots if all_roots is not None else (root_path,),
            correction_only=True,
        )
    finally:
        _sweep_latch["busy"] = False


async def _reprobe_disk(root_path: str, *, fallback: DiskUsage) -> DiskUsage:
    """Re-read ``root_path``'s disk usage after a destructive step whose freed
    bytes the sweep's ledger cannot account for (issues #482 / #485).

    The sweep takes ONE disk snapshot up front and then reasons about "have we
    freed enough yet?" from that snapshot plus the bytes each successful eviction
    reports. Two things break that arithmetic: crash-recovery finishing an
    incomplete purge (it runs AFTER the snapshot), and a PARTIAL delete (real
    bytes leave disk, but the hardlink-aware measurement necessarily precedes the
    delete and the delete only got part-way, so the primitive honestly reports
    ``0`` rather than guessing). Either way the picture is stale in the direction
    that keeps deleting, so it is re-read here before further victims are chosen.

    A failed re-probe returns ``fallback`` -- the ORIGINAL snapshot -- rather than
    aborting: every step this compensates for only ever FREES space, so the stale
    reading can only over-state pressure, and over-stating is the same behavior
    the sweep had before this correction existed. Logged, never swallowed.
    """
    try:
        return await purge_service.run_abandonable_probe(
            lambda: read_disk_usage(root_path),
            root_path,
            operation_name="eviction sweep pressure re-baseline probe",
        )
    except OSError as exc:
        _logger.warning(
            "could not re-read disk usage for %s after a destructive eviction step "
            "(%s); continuing on the pre-sweep snapshot, which can only over-state "
            "pressure",
            safe_text(root_path),
            type(exc).__name__,
        )
        return fallback


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
    qbt: DownloadClientPort | None = None,
) -> list[EvictionOutcome]:
    """The sweep body, entered only under :func:`run_eviction_sweep`'s
    serialization latch — see the public docstring."""
    try:
        # A hung/unresponsive mount must neither freeze the event loop nor keep
        # CPython alive through its joined default executor after shutdown's bound.
        disk = await purge_service.run_abandonable_probe(
            lambda: read_disk_usage(root_path),
            root_path,
            operation_name="eviction sweep disk-usage probe",
        )
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
    disk_used_pct = used_percent(disk)
    grace_cutoff = datetime.now(UTC) - timedelta(days=grace_days)

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
    # Recovery can DELETE (it finishes an incompletely-deleted eviction, issues
    # #482 / #485), and the snapshot above predates it. Re-probe before any
    # pressure arithmetic reads it, so a recovery that already relieved the disk
    # cannot make this sweep go on to evict watched titles it no longer needs to.
    # A re-probe failure is not fatal: fall back to the original snapshot, which
    # can only OVER-estimate pressure (recovery only ever frees), and log it.
    if await _resume_interrupted_evictions(
        session=session,
        library=library,
        fs=fs,
        qbt=qbt,
        media_type=media_type,
        root_path=root_path,
        all_roots=scope,
    ):
        disk = await _reprobe_disk(root_path, fallback=disk)

    # A multi-step operator correction can be between its DB publish and physical
    # purge while recovery runs. Its delete may satisfy this root's pressure, so a
    # non-proactive sweep must not select other victims from the snapshot taken
    # before that purge. The next periodic/manual sweep re-reads pressure after the
    # correction releases its claim.
    #
    # Two deliberate bounds, both pinned by tests:
    #   * ROOT-SCOPED. ``_ACTIVE_PURGE_PATHS`` is process-global; ownership is
    #     decided the same way candidate assembly decides it, so a correction on the
    #     anime root cannot stall reclamation on a genuinely full tv root -- pressure
    #     nothing in flight is going to relieve.
    #   * NON-PROACTIVE ONLY. The whole reason to defer is stale pressure ARITHMETIC.
    #     A proactive sweep has no pressure gate and no target to overshoot, so there
    #     is no snapshot to go stale and nothing here to suppress. The correction's
    #     OWN path is still protected in both modes, by the per-path defer in
    #     ``_recover_rearmed_season`` / ``_resume_one``, which runs above this.
    #
    # HONEST RESIDUAL (issue #526): this is a check, not a barrier. It sees only
    # corrections that had already claimed their path by the time the sweep reached
    # this line. A correction that starts AFTER it -- while candidate assembly is
    # awaiting Plex, or while an earlier victim's delete runs -- is invisible to this
    # sweep, which then evicts from a snapshot the correction is about to change.
    # Those victims were each independently eligible (watched, past grace, un-pinned),
    # so the cost is bounded to over-reclaiming, never to touching the correction's
    # own path; it is NOT the same thing as serializing corrections against sweeps,
    # and nothing here should be read as claiming that.
    if not proactive and any(
        _owned_by_root(active_path, root_path, scope)
        for active_path in purge_service.active_purge_paths()
    ):
        _logger.info(
            "eviction sweep deferred for %s root %s: an operator correction purge "
            "is active under this root; pressure will be re-read after it settles",
            media_type,
            safe_text(root_path),
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
    # The cutoff above is shared by recovery and fresh candidate selection so both
    # paths apply one sweep-wide grace decision.
    pairs = (
        await _movie_candidates(session, library, disk.total_bytes, root_path, scope, grace_cutoff)
        if media_type == "movie"
        else await _season_candidates(
            session, library, disk.total_bytes, root_path, scope, grace_cutoff
        )
    )
    if not pairs:
        return []

    pending_by_id: dict[int, _Pending] = {id(candidate): pending for candidate, pending in pairs}
    candidates = [candidate for candidate, _pending in pairs]

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
    # Set when an eviction destroyed data whose freed bytes could not be measured
    # (a partial delete, issue #482). The extra-pool loop below decides "have we
    # freed enough yet?" from ``disk_used_pct`` + ``freed_bytes_total``, and a
    # partial delete contributes real bytes to NEITHER -- so without this flag the
    # sweep would keep selecting further victims as though its destruction freed
    # nothing, deleting watched titles after the target was already reached.
    pressure_stale = False

    def _note_unmeasured_free() -> None:
        nonlocal pressure_stale
        pressure_stale = True

    async def _attempt(candidate: EvictionCandidate) -> None:
        nonlocal freed_bytes_total
        try:
            outcome = await _evict_one(
                session=session,
                fs=fs,
                library=library,
                candidate=candidate,
                pending=pending_by_id[id(candidate)],
                grace_cutoff=grace_cutoff,
                note_unmeasured_free=_note_unmeasured_free,
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

    async def _rebaseline() -> bool:
        """Re-stat the root after an UNMEASURED destructive delete and restart the
        pressure accounting from that fresh reading.

        ``False`` means the reading is unusable (an unmounted/zero-total root), so
        the caller must stop rather than keep deleting against arithmetic it can no
        longer check. Clears the flag first, so one re-stat serves one destruction.
        """
        nonlocal pressure_stale, disk, disk_used_pct, freed_bytes_total
        pressure_stale = False
        disk = await _reprobe_disk(root_path, fallback=disk)
        if disk.total_bytes <= 0:  # pragma: no cover - defensive
            return False
        disk_used_pct = used_percent(disk)
        freed_bytes_total = 0
        return True

    for candidate in selected:
        # RE-BASELINE MID-PREFIX (Codex round-4 P1). ``selected`` is the domain's
        # target-seeking prefix, sized from ESTIMATED reclaimable bytes, and the
        # whole prefix is normally deleted. But a partial delete frees real bytes
        # that neither ``freed_bytes_total`` nor the pre-sweep snapshot can see, so
        # an early victim's partial delete can already have reached the target
        # while every later victim in this prefix is still queued for deletion.
        # Re-stat the moment that happens and stop if the pressure is genuinely
        # gone -- deleting a watched title the operator did not need to lose is
        # exactly the harm the target exists to bound. ONLY when the flag is set:
        # with no unmeasured destruction the estimate-based prefix is honest and
        # keeps its existing all-or-nothing semantics. A proactive sweep has no
        # pressure target at all (it deliberately clears every eligible
        # candidate), so it never stops early.
        if pressure_stale and not proactive and disk.total_bytes > 0:
            if not await _rebaseline():
                break
            if pressure_relieved(disk_used_pct, freed_bytes_total, disk.total_bytes, target_pct):
                break
        await _attempt(candidate)

    # R4-6: the estimate-based selection above can under-deliver (a hardlinked
    # candidate frees far less than its nominal size) -- keep going, stalest
    # first, until the REAL running freed total actually reaches target_pct or
    # every remaining eligible candidate has been tried. A no-op when the
    # estimate matched reality (the common case): `extra_pool` is empty, or the
    # very first check below already finds `projected <= target_pct`.
    if extra_pool and disk.total_bytes > 0:
        for candidate in extra_pool:
            # RE-BASELINE (issue #482 review): an unmeasured destructive delete
            # landed, so the snapshot + running-total arithmetic below is no
            # longer an honest picture of the disk. Re-stat it and restart the
            # accounting from the fresh reading rather than keep deleting against
            # a stale one. Done lazily, only when it actually happened -- the same
            # helper the selected prefix above uses.
            if pressure_stale and not await _rebaseline():
                break
            if pressure_relieved(disk_used_pct, freed_bytes_total, disk.total_bytes, target_pct):
                break
            await _attempt(candidate)

    return outcomes
