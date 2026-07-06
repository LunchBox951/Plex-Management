"""Queue orchestration — reconcile against the client, blocklist-and-research.

``reconcile_and_list`` is the poll loop: it reads the active downloads and the
live qBittorrent snapshot, runs the **pure** :func:`reconcile`, and persists each
resulting transition *verbatim* (the reconciler output is authoritative — it is
NOT re-gated through ``is_legal_transition``; that guard governs only
operator-initiated moves). A transition flagged ``set_first_seen_at`` stamps the
missing-grace anchor. A transition whose reason records an unmapped raw client
state is logged at WARNING (closing the honesty gap). Each ``FailedPending``
transition drives the Radarr-style blocklist-then-research flow: a ``blocklist``
row is written and the originating request is set back to ``searching``.

``mark_failed`` is the operator move: it fails a download (routing through
``FailedPending`` when the legal graph requires it, via
:func:`is_legal_transition`) and optionally blocklists the release.

Failure handling is a three-phase flow (issue #68 + the Phase-C-strand hardening):

  Phase A  commit the reconcile transitions -> the failing row lands at the
           NON-terminal ``failed_pending`` (blocklist / terminal ``Failed`` /
           request re-arm are NOT yet written).
  Phase B  best-effort ``remove_torrent`` per failed hash (external client I/O,
           run OUTSIDE any open write transaction).
  Phase C  in ONE bounded-retry transaction: complete ``failed_pending`` ->
           ``Failed``, write the blocklist row, and re-arm the owning
           request/season to ``searching``.

Why the blocklist + ``Failed`` advance are DEFERRED to Phase C (not committed in
Phase A as they once were): a Phase-C failure must not STRAND the owner. If Phase A
committed terminal ``Failed`` and Phase C then failed, the download would be
terminal (out of ``list_active`` -> the reconciler never revisits it) while the
request stayed ``downloading`` forever — un-healable. By leaving the row at
``failed_pending`` (non-terminal, still in ``list_active``) until Phase C commits,
a Phase-C strand is *reconcilable*: :func:`reconcile_and_list` re-derives a failed
event for any row ALREADY at ``failed_pending`` at cycle start and re-runs Phase
B/C, so the reconcile loop heals it on a later cycle. See the Phase-C block for the
full invariant.

Why NOT re-arm BEFORE removal (the tempting simplification that removes the strand
window): it reintroduces issue #68. The re-arm makes the request due for auto-grab
again; a re-search can re-resolve to the SAME info_hash the stale removal is about
to delete. The blocklist does NOT prevent this — it is best-effort: an indexer
often exposes NO ``info_hash`` at grab-decision time (``grab_service`` line ~362),
so the real hash is only known AFTER ``qbt.add`` (line ~394); a hashless re-search
that title/indexer-varies past the tier-2 blocklist match still resolves to the
same torrent, which Phase B would then delete. Radarr's ``EventHandleOrder.Last``
(removal precedes re-search eligibility) is the real guard, so the re-arm MUST stay
after removal — the strand risk is instead removed by making the residual
reconcilable, above.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy.exc import SQLAlchemyError

from plex_manager.domain.events import DownloadFailed
from plex_manager.domain.reconciler import (
    StateTransition,
    failed_download_events,
    reconcile,
    unmapped_client_states,
)
from plex_manager.domain.state_machine import (
    TERMINAL_STATES,
    DownloadState,
    is_legal_transition,
)
from plex_manager.logsafe import safe_int
from plex_manager.models import (
    BlocklistReason,
    Download,
    RequestStatus,
)
from plex_manager.repositories.blocklist import SqlBlocklistRepository
from plex_manager.repositories.downloads import SqlDownloadRepository
from plex_manager.repositories.requests import SqlRequestRepository
from plex_manager.repositories.season_requests import SqlSeasonRequestRepository
from plex_manager.services import blocklist_service, purge_service, season_request_service
from plex_manager.services.request_service import TERMINAL_REQUEST_STATUS_VALUES

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from plex_manager.ports.download_client import DownloadClientPort
    from plex_manager.ports.repositories import DownloadRecord

__all__ = ["InvalidStateTransitionError", "list_queue", "mark_failed", "reconcile_and_list"]

_logger = logging.getLogger(__name__)

_TERMINAL_STATUS_VALUES = frozenset(s.value for s in TERMINAL_STATES)

# Request statuses from which a failed download's owning request/season may still
# be re-armed to ``searching`` — every NON-terminal status. This is the CAS
# allowed-from set that makes the re-arm safe against a concurrent settle: a
# request a concurrent ``cancel_request`` (or any terminal transition) already
# moved to ``cancelled`` / ``available`` / ``failed`` / ``evicted`` / ``completed``
# is left untouched (the CAS no-ops) rather than being dragged back to
# ``searching`` and auto-grabbed again. It is the compare-and-swap analogue of the
# season path's ``skip_if_terminal`` guard (proceed ONLY from a non-terminal
# status), applied atomically so no read-then-write TOCTOU window remains.
_REARMABLE_REQUEST_STATUS_VALUES: frozenset[str] = (
    frozenset(s.value for s in RequestStatus) - TERMINAL_REQUEST_STATUS_VALUES
)

# Phase-C bounded-retry policy. A re-arm commit can fail on a transient DB error
# (SQLite "database is locked" under write contention) or a late uniqueness
# conflict; a few short retries almost always clear it. On exhaustion the failure
# is surfaced loudly and the row is LEFT at ``failed_pending`` (reconcilable), never
# stranded at terminal ``Failed``. ``len(_PHASE_C_BACKOFF_SECONDS)`` is
# ``_PHASE_C_MAX_ATTEMPTS - 1`` (no sleep after the final attempt).
_PHASE_C_MAX_ATTEMPTS = 3
_PHASE_C_BACKOFF_SECONDS: tuple[float, ...] = (0.05, 0.1)


class InvalidStateTransitionError(Exception):
    """An operator move is illegal for the download's current state (HTTP 409)."""

    def __init__(self, frm: str, to: str) -> None:
        self.frm = frm
        self.to = to
        super().__init__(f"illegal transition {frm} -> {to}")


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class _FailedReArm:
    """The owning request/season to re-arm to ``searching`` AFTER torrent removal.

    Issue #68: re-arming a failed request makes it due for auto-grab again, and a
    re-grab resolving to the SAME info_hash BEFORE the old torrent's removal
    completes would have its live torrent + data deleted by that (now stale)
    removal. Radarr avoids this by running its ``RedownloadFailedDownloadService``
    with ``EventHandleOrder.Last`` -- removal precedes re-search eligibility. We
    mirror that with the three-phase flow (see the module docstring), so the re-arm
    target must survive Phase A's commit as a plain value rather than being
    re-derived from the (now ``failed_pending``, possibly identity-map-stale)
    ``Download`` row on the far side of the boundary.

    ``media_type`` is the resolved media namespace of the failed release; it is
    threaded alongside ``media_request_id`` / ``season`` so the re-arm phase holds
    the full failure identity without touching the committed row (``season is not
    None`` is what actually routes TV through the season rollup below).
    """

    media_request_id: int
    season: int | None
    media_type: str | None


async def _rearm_failed_request(session: AsyncSession, rearm: _FailedReArm) -> None:
    """Phase C: re-arm the owning request/season to ``searching`` (issue #68).

    Runs in a SEPARATE transaction AFTER ``remove_torrent`` (Phase B) so the
    request only becomes due for auto-grab once the old torrent is gone -- closing
    the window in which a same-hash re-grab could be deleted by the stale removal.

    Both branches are a COMPARE-AND-SWAP that only re-arms from a still-re-armable
    (non-terminal) status (``_REARMABLE_REQUEST_STATUS_VALUES``), never
    unconditionally:

    * **Movie** (``season is None``): a plain ``set_status_if_in``. Without the CAS
      a concurrent ``cancel_request`` committed between Phase A and Phase C -- which
      moved the request to terminal ``cancelled`` -- would be silently overwritten
      back to ``searching`` and re-queued/auto-grabbed. The CAS makes ``cancelled``
      (and every other terminal status) win: the re-arm no-ops.
    * **TV** (``season is not None``): the SEASON re-arms and the parent's computed
      rollup recomputes in the same transaction. It resolves the season row via
      ``ensure`` (idempotent get-or-create) and then CASes it with the SAME
      re-armable allowed-from set, so a season a prior download already finished
      (``completed`` / ``available`` / ``failed`` / ``evicted`` / ``cancelled``) is
      left untouched -- the compare-and-swap replacement for the old
      ``skip_if_terminal`` read-then-write, which had a narrow TOCTOU window between
      its ``ensure`` read and its status write.

    A no-op CAS (``False``) is logged, not silently swallowed (honesty over
    silence): it means the owner settled underneath us and the re-arm was
    deliberately declined.
    """
    if rearm.season is not None:
        season_repo = SqlSeasonRequestRepository(session)
        row = await season_repo.ensure(
            rearm.media_request_id, rearm.season, status=RequestStatus.pending.value
        )
        changed = await season_request_service.set_status_if_in(
            session,
            media_request_id=rearm.media_request_id,
            season_request_id=row.id,
            status=RequestStatus.searching.value,
            allowed_from=_REARMABLE_REQUEST_STATUS_VALUES,
        )
        if not changed:
            _logger.info(
                "re-arm declined: season %s of request %s already settled (%s)",
                rearm.season,
                rearm.media_request_id,
                row.status,
            )
        return
    changed = await SqlRequestRepository(session).set_status_if_in(
        rearm.media_request_id,
        RequestStatus.searching.value,
        _REARMABLE_REQUEST_STATUS_VALUES,
    )
    if not changed:
        _logger.info(
            "re-arm declined: request %s already settled (terminal) -- not re-queued",
            rearm.media_request_id,
        )


async def _commit_phase_c_with_retry(
    session: AsyncSession,
    apply: Callable[[], Awaitable[None]],
    *,
    context: str,
    identity: object,
) -> None:
    """Run the Phase-C writes (``apply``) and commit, retrying transient failures.

    Phase B (torrent removal) is IRREVERSIBLE, so a Phase-C failure must never
    strand the owner. Two layers protect against that:

    1. **Bounded retry.** A transient DB error (SQLite "database is locked") or a
       late uniqueness conflict is rolled back and retried up to
       ``_PHASE_C_MAX_ATTEMPTS`` times with a short backoff -- almost always
       succeeding. ``apply`` is re-run from scratch each attempt (the rollback
       discarded its writes), so nothing it writes -- the blocklist row, the
       ``Failed`` advance, the re-arm -- is ever duplicated across attempts.
    2. **Reconcilable residual.** If every attempt fails, the failure is logged at
       ERROR (loud, with the failing identity) and re-raised. Because the blocklist
       + terminal ``Failed`` advance live INSIDE ``apply`` (deferred out of Phase
       A), an exhausted Phase C leaves the download at the NON-terminal
       ``failed_pending`` -- still in ``list_active``, so a later
       :func:`reconcile_and_list` re-derives it (see the strand re-derivation
       there) and retries Phase B/C. The request is therefore never left
       ``downloading`` with an un-healable terminal download.

    Only :class:`SQLAlchemyError` is retried (a genuine DB failure); anything else
    (a programming error) propagates immediately, unmasked.
    """
    for attempt in range(1, _PHASE_C_MAX_ATTEMPTS + 1):
        try:
            await apply()
            await session.commit()
            return
        except SQLAlchemyError:
            await session.rollback()
            if attempt >= _PHASE_C_MAX_ATTEMPTS:
                _logger.error(
                    "Phase C (%s) failed after %d attempts; the download(s) are left at "
                    "the non-terminal failed_pending for the reconcile loop to re-derive "
                    "and heal -- NOT stranded at terminal Failed. identity=%r",
                    context,
                    attempt,
                    identity,
                )
                raise
            await asyncio.sleep(_PHASE_C_BACKOFF_SECONDS[attempt - 1])


def _media_type_for_blocklist(
    record: DownloadRecord | None, request_media_type: str | None
) -> str | None:
    if request_media_type is not None:
        return request_media_type
    if record is None:
        return None
    if record.media_type is not None:
        return record.media_type
    return "tv" if record.season is not None else "movie"


async def _handle_failed(
    session: AsyncSession,
    event: DownloadFailed,
    rows: list[DownloadRecord],
) -> _FailedReArm | None:
    """Phase C completion for one failed download: advance ``failed_pending`` ->
    ``Failed`` (compare-and-swap), write the blocklist row, and return the owning
    request/season to re-arm (or ``None`` if the row has no owning request or the
    row was already completed by a concurrent writer).

    The terminal advance is a CAS (``failed_pending`` -> ``Failed`` only if still
    ``failed_pending``), and the blocklist + re-arm are GATED on winning it, for two
    reasons:

    * **Idempotent self-heal.** :func:`reconcile_and_list` re-derives a failed event
      for a row already at ``failed_pending`` (a stranded prior Phase C). Gating on
      the CAS means re-running this NEVER writes a second blocklist row for a row
      that has since been completed.
    * **No double-processing.** A row sits at ``failed_pending`` across Phase B
      (external I/O). An operator ``mark_failed`` and the reconcile loop could both
      pick up the same ``failed_pending`` row; only the one that wins the CAS writes
      the blocklist / re-arm -- the loser no-ops.

    Deferring the blocklist + ``Failed`` advance out of Phase A (they used to commit
    there) is what makes an exhausted Phase C leave a RECONCILABLE ``failed_pending``
    row rather than an un-healable terminal ``Failed`` (see the module docstring).
    """
    record = next(
        (r for r in rows if r.torrent_hash.lower() == event.torrent_hash.lower()),
        None,
    )
    if record is None:
        # Every failed event is built from a row in ``rows`` (a reconcile transition
        # or the strand re-derivation), so this is unreachable in practice; guard
        # rather than fail a KeyError on a row we cannot advance.
        return None

    # Complete the FailedPending -> Failed transition (CAS). The reconciler only
    # moves the row as far as ``failed_pending``; without this advance the row would
    # be stranded there -- neither terminal (so it lingers in ``list_active`` and
    # the queue shows a zombie torrent) nor active (the reconciler skips
    # ``failed_pending``, only revisiting it via THIS Phase C). A losing CAS means a
    # concurrent writer already completed it: honor that, write nothing more.
    won = await SqlDownloadRepository(session).update_status_if_in(
        record.id,
        DownloadState.Failed.value,
        frozenset({DownloadState.FailedPending.value}),
        failed_reason=event.reason,
    )
    if not won:
        return None

    blocklist_repo = SqlBlocklistRepository(session)
    request_repo = SqlRequestRepository(session)
    request = (
        await request_repo.get(record.media_request_id)
        if record.media_request_id is not None
        else None
    )
    source_title = (
        await blocklist_service.source_title_for(session, event.torrent_hash) or event.source_title
    )
    indexer = await blocklist_service.indexer_for(session, event.torrent_hash)
    await blocklist_repo.create(
        source_title=source_title,
        reason=BlocklistReason.failed.value,
        tmdb_id=request.tmdb_id if request is not None else event.tmdb_id,
        torrent_hash=event.torrent_hash,
        indexer=indexer,
        # Scope by media namespace so this entry can't reject a movie/show that
        # happens to share the tmdb id. Prefer the owning request; fall back to the
        # persisted download metadata or season scope for orphan rows.
        media_type=_media_type_for_blocklist(
            record, request.media_type if request is not None else None
        ),
    )

    # Hand the owning request/season back for the re-arm (same Phase-C transaction).
    # Captured from ``record``/``request`` in hand rather than re-read after a commit
    # boundary, where the now-``Failed`` row would be excluded from ``list_active``.
    if record.media_request_id is not None:
        return _FailedReArm(
            media_request_id=record.media_request_id,
            season=record.season,
            media_type=_media_type_for_blocklist(
                record, request.media_type if request is not None else None
            ),
        )
    return None


async def list_queue(session: AsyncSession) -> list[DownloadRecord]:
    """Read-only snapshot of the active queue — NO reconcile, NO writes.

    The background reconcile loop (``web.app._reconcile_loop``) is the single owner
    of cross-system truth (overview §5, north-star #5): it reconciles the client and
    refreshes progress/seed_ratio on a fixed cadence. A GET /queue poll must NOT also
    reconcile — running ``reconcile_and_list`` concurrently with the loop can clobber
    the importer's CAS-claimed ``importing`` status (the per-download import lock does
    not cover this write path), stranding a placed file until a later cycle. So the
    read path is passive: it returns the currently persisted ``DownloadRecord`` rows
    and writes nothing. The loop's frequent refresh keeps the listed progress/status
    fresh enough for display.
    """
    return await SqlDownloadRepository(session).list_active()


async def reconcile_and_list(
    qbt: DownloadClientPort,
    session: AsyncSession,
) -> list[DownloadRecord]:
    """Reconcile active downloads against the client and return the live queue."""
    download_repo = SqlDownloadRepository(session)
    rows = await download_repo.list_active()
    statuses = await qbt.get_all_statuses()
    now = _utcnow()

    transitions = reconcile(rows, statuses, now=now)
    snapshot = {status.info_hash.lower(): status for status in statuses}

    # Honesty over silence: surface every unmapped raw client state on EVERY
    # cycle, even when it maps to the row's current state and so emits no
    # transition (otherwise the unknown string would be swallowed). This is
    # independent of the transition loop below, which only fires on a change.
    for torrent_hash, raw_state in unmapped_client_states(rows, statuses):
        _logger.warning(
            "download %s: unmapped qBittorrent state %r; tracking as downloading",
            torrent_hash,
            raw_state,
        )

    # Single update path over every tracked row. A row with a transition is moved
    # to its new state (carrying the live progress); a row with NO transition but
    # still present in the client snapshot has its progress/seed_ratio refreshed —
    # the pure reconciler only emits on a STATE change, so a download advancing
    # 10%->50%->90% while staying "Downloading" would otherwise show stale progress
    # in the queue forever (honesty over silence).
    transitions_by_id = {transition.download_id: transition for transition in transitions}
    applied_transitions: list[StateTransition] = []
    for row in rows:
        live = snapshot.get(row.torrent_hash.lower())
        transition = transitions_by_id.get(row.id)
        if transition is not None:
            applied = await download_repo.update_status_if_in(
                transition.download_id,
                transition.to_state.value,
                frozenset({transition.from_state}),
                progress=live.progress if live is not None else None,
                seed_ratio=live.ratio if live is not None else None,
                first_seen_at=now if transition.set_first_seen_at else None,
                clear_first_seen_at=transition.clear_first_seen_at,
            )
            if applied:
                applied_transitions.append(transition)
        elif live is not None:
            # Refresh live progress ONLY — never rewrite status. ``row.status`` is the
            # snapshot captured at list_active() time; an operator's import retry (or
            # the importer) may have CAS-claimed the row to ``importing`` during the
            # qbt.get_all_statuses() await above, and writing the stale snapshot status
            # back would clobber that claim (defeating the import finalize CAS). A
            # progress-only update leaves any concurrent transition intact (G5).
            await download_repo.refresh_progress(
                row.id, progress=live.progress, seed_ratio=live.ratio
            )

    # Phase A commit: persist the reconcile transitions (incl. Downloading ->
    # ``failed_pending``) + progress ONLY. The blocklist, the terminal ``Failed``
    # advance, and the request re-arm are DEFERRED to Phase C so an exhausted Phase C
    # cannot strand the owner (see the module docstring). qbt.remove is external I/O
    # and must not run while this write transaction holds SQLite's write lock, so it
    # is likewise deferred to Phase B below.
    await session.commit()

    # Build the set of failed rows to complete this cycle:
    #   1. Rows that transitioned TO ``failed_pending`` THIS cycle (CAS-APPLIED only
    #      -- a transition that lost its compare-and-set must not spawn a
    #      blocklist/re-arm for a state change that never persisted).
    #   2. Rows ALREADY at ``failed_pending`` in the cycle-start snapshot -- a prior
    #      cycle/operator advanced them there but Phase C never completed (a strand).
    #      They are disjoint from (1) BY CONSTRUCTION: a row transitioned to
    #      ``failed_pending`` this cycle had a DIFFERENT snapshot status (its
    #      from_state, e.g. ``downloading``/``client_missing``), never
    #      ``failed_pending``. Without this re-derivation such a strand would linger
    #      forever -- the pure reconciler skips ``failed_pending`` (not an ACTIVE
    #      state), so it emits no transition and no fresh event. THIS is the concrete
    #      heal a Phase-C failure relies on.
    failed_events = list(failed_download_events(applied_transitions, rows, occurred_at=now))
    failed_events += [
        DownloadFailed(
            torrent_hash=row.torrent_hash,
            source_title=row.torrent_hash,
            reason=row.failed_reason or "recovered stranded failed_pending row",
            tmdb_id=row.tmdb_id,
            occurred_at=now,
        )
        for row in rows
        if row.status == DownloadState.FailedPending.value
    ]

    if not failed_events:
        # ``populate_existing`` refreshes the returned rows from the DB (issue #77):
        # with ``expire_on_commit=False`` a row that LOST a status CAS earlier in this
        # cycle keeps its stale in-memory status, and the identity map would otherwise
        # win over this SELECT, reporting a status the DB no longer holds.
        return await download_repo.list_active(populate_existing=True)

    # Phase B: close the seeding leak (ADR-0014) AND, per issue #68, ensure each old
    # torrent is gone BEFORE its request is re-armed. Best-effort per hash: a client
    # hiccup never undoes the committed Phase A, an already-gone hash (the common
    # case -- the row usually failed BECAUSE it went ClientMissing) is a no-op
    # success, and because it is logged-not-raised a removal failure does not block
    # the Phase C completion below (the row still lands Failed + re-armed).
    for event in failed_events:
        await purge_service.remove_torrent(
            qbt,
            event.torrent_hash,
            context="a reconcile-driven download failure",
            extra={"torrent_hash": event.torrent_hash, "tmdb_id": event.tmdb_id},
        )

    # Phase C: complete each failure (failed_pending -> Failed + blocklist + re-arm)
    # in ONE bounded-retry transaction. On exhaustion the rows stay at the reconcilable
    # ``failed_pending`` for a later cycle's strand re-derivation to heal.
    async def _complete_reconcile_failures() -> None:
        rearms: list[_FailedReArm] = []
        for event in failed_events:
            rearm = await _handle_failed(session, event, rows)
            if rearm is not None:
                rearms.append(rearm)
        for rearm in rearms:
            await _rearm_failed_request(session, rearm)

    await _commit_phase_c_with_retry(
        session,
        _complete_reconcile_failures,
        context="reconcile-driven failures",
        identity=[event.torrent_hash for event in failed_events],
    )

    # ``populate_existing`` refreshes the returned rows from the DB (issue #77): see
    # the same note in the no-failures early return above.
    return await download_repo.list_active(populate_existing=True)


async def mark_failed(
    session: AsyncSession,
    qbt: DownloadClientPort | None,
    *,
    download_id: int,
    blocklist: bool,
    remove_torrent: bool = True,
) -> DownloadRecord:
    """Operator move: fail a download (and optionally blocklist its release).

    ``remove_torrent`` (default ``True``): also remove the torrent + its data from
    the client (ADR-0014's seeding-leak fix). Before this, a mark-failed left the
    bad torrent seeding forever; now it is removed best-effort (a failure is
    logged, never raised -- the DB fail/blocklist/re-arm stands regardless, and an
    already-gone hash is a no-op success).

    ``qbt`` may be ``None`` ONLY when ``remove_torrent`` is ``False`` (the DB-only
    path): the caller (the mark-failed endpoint) resolves qBittorrent optionally so a
    fail/blocklist/re-arm still works on an install without the client configured. A
    ``None`` client with ``remove_torrent=True`` is a caller bug -- the endpoint has
    already 409'd that combination up front -- so it is refused loudly here rather than
    silently skipping the removal (honesty over silence).

    Mirrors :func:`reconcile_and_list`'s three-phase ordering (issue #68 + the
    Phase-C-strand hardening): Phase A routes the row to the NON-terminal
    ``failed_pending`` and commits; Phase B removes the torrent; Phase C completes
    ``Failed`` + blocklist + re-arm in one bounded-retry transaction. The terminal
    ``Failed`` advance is DEFERRED to Phase C so an exhausted Phase C leaves a
    reconcilable ``failed_pending`` row (which the reconcile loop re-derives and
    heals) rather than an un-healable terminal ``Failed`` + ``downloading`` request.
    """
    if remove_torrent and qbt is None:
        raise ValueError("mark_failed(remove_torrent=True) requires a qBittorrent client")
    download_repo = SqlDownloadRepository(session)
    row = await session.get(Download, download_id)
    if row is None:
        raise LookupError(f"download {download_id} does not exist")

    current = DownloadState(row.status)
    if current.value in _TERMINAL_STATUS_VALUES:
        raise InvalidStateTransitionError(current.value, DownloadState.Failed.value)

    # Capture the identity into plain locals BEFORE the Phase A commit: Phase B/C read
    # them after the commit boundary, where touching the ORM ``row`` could trigger a
    # refresh (and, after a Phase-C retry rollback, an expiry). The re-arm is
    # UNCONDITIONAL of the ``blocklist`` flag -- that flag governs ONLY whether a
    # Blocklist row is written, NOT whether the request status is corrected. Without
    # the re-arm, a mark-failed(blocklist=false) drives the download to terminal Failed
    # (gone from the active queue) while the request stays ``downloading`` forever: a
    # dishonest status asserting an active download that no longer exists.
    torrent_hash = row.torrent_hash
    request_id = row.media_request_id
    download_tmdb_id = row.tmdb_id
    rearm: _FailedReArm | None = None
    if request_id is not None:
        rearm = _FailedReArm(
            media_request_id=request_id,
            season=row.season,
            media_type=(
                row.media_type.value
                if row.media_type is not None
                else ("tv" if row.season is not None else "movie")
            ),
        )

    async def _raise_current_transition() -> None:
        await session.rollback()
        latest = await session.get(Download, download_id, populate_existing=True)
        actual = latest.status if latest is not None else current.value
        raise InvalidStateTransitionError(actual, DownloadState.Failed.value)

    # Route to the pre-terminal ``failed_pending`` pause (unless already there). The
    # legal graph reaches ``Failed`` only from ``failed_pending``, so an actively
    # Downloading torrent (etc.) must pass through it first.
    if current is not DownloadState.FailedPending:
        if not is_legal_transition(current, DownloadState.FailedPending):
            raise InvalidStateTransitionError(current.value, DownloadState.Failed.value)
        pending = await download_repo.update_status_if_in(
            download_id,
            DownloadState.FailedPending.value,
            frozenset({current.value}),
        )
        if not pending:
            await _raise_current_transition()

    # Phase A commit: the row is at ``failed_pending`` (reconcilable). The blocklist,
    # the terminal ``Failed`` advance, and the re-arm are NOT yet written.
    await session.commit()

    # Phase B: close the seeding leak (ADR-0014) and, per issue #68, remove the old
    # torrent BEFORE re-arming. Best-effort + already-gone-is-a-no-op (see
    # ``purge_service.remove_torrent``): a client hiccup never undoes the committed
    # Phase A, and -- because removal is logged-not-raised -- never blocks Phase C.
    # ``qbt is not None`` is guaranteed by the top-of-function guard whenever
    # ``remove_torrent`` is True; the explicit check narrows the optional type.
    if remove_torrent and qbt is not None:
        await purge_service.remove_torrent(
            qbt,
            torrent_hash,
            context="an operator mark-failed",
            extra={"torrent_hash": torrent_hash, "download_id": safe_int(download_id)},
        )

    # Phase C: complete ``failed_pending`` -> ``Failed`` (CAS) + optional blocklist +
    # re-arm, in one bounded-retry transaction. A losing terminal CAS means a
    # concurrent completer (a reconcile strand-heal) already finished this row: honor
    # it (the operator's intent -- fail the download -- is satisfied) and skip the
    # duplicate blocklist/re-arm. On retry exhaustion the row stays at the
    # reconcilable ``failed_pending`` for the reconcile loop to heal.
    async def _complete_mark_failed() -> None:
        won = await download_repo.update_status_if_in(
            download_id,
            DownloadState.Failed.value,
            frozenset({DownloadState.FailedPending.value}),
            failed_reason="marked failed by operator",
        )
        if not won:
            return
        if blocklist:
            source_title = (
                await blocklist_service.source_title_for(session, torrent_hash) or torrent_hash
            )
            indexer = await blocklist_service.indexer_for(session, torrent_hash)
            request = (
                await SqlRequestRepository(session).get(request_id)
                if request_id is not None
                else None
            )
            await SqlBlocklistRepository(session).create(
                source_title=source_title,
                reason=BlocklistReason.user_reported.value,
                tmdb_id=request.tmdb_id if request is not None else download_tmdb_id,
                torrent_hash=torrent_hash,
                indexer=indexer,
                # Scope by media namespace (see _handle_failed). Prefer the owning
                # request; fall back to the persisted download metadata or season scope.
                media_type=_media_type_for_blocklist(
                    await download_repo.get_by_hash(torrent_hash),
                    request.media_type if request is not None else None,
                ),
            )
        if rearm is not None:
            await _rearm_failed_request(session, rearm)

    await _commit_phase_c_with_retry(
        session,
        _complete_mark_failed,
        context="operator mark-failed",
        identity=safe_int(download_id),
    )

    failed = await download_repo.get_by_hash(torrent_hash)
    if failed is None:  # pragma: no cover - just updated this row
        raise LookupError(f"download {download_id} vanished mid-update")
    return failed
