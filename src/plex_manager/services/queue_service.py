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

Operator provenance — a ``failed_pending`` row carries WHOSE failure it is, so the
reconcile heal can never override an operator's explicit ``mark_failed`` choices
(``blocklist=False`` / ``remove_torrent=False``). Two mechanisms compose:

* **Live claim registry** (``_operator_fail_claims``): an in-process map of
  download id -> the operator's flags, registered by :func:`mark_failed` BEFORE its
  Phase-A commit and cleared (``finally``) once its Phase C resolves.
  :func:`reconcile_and_list` skips claimed rows both when building its completion
  set (before Phase B) and again inside its Phase-C transaction, so a reconcile
  tick landing during the operator's Phase-B await can neither remove the torrent
  against ``remove_torrent=False`` nor steal the ``failed_pending`` -> ``Failed``
  CAS with reconcile-default side effects. This is safe as in-process state because
  the deployment is single-process by design (the same assumption
  ``web/routers/settings.py``'s ``_rotate_lock`` documents) and the registry is
  only ever touched by synchronous dict operations on the one event loop — there
  is no await inside any read-modify-write, so no lock is needed (a module-level
  ``asyncio.Lock`` would also bind to the first event loop that acquires it,
  breaking per-test loops, for zero added safety here).
* **Persistent marker** (crash/exhaustion provenance): :func:`mark_failed`'s Phase
  A stamps a structured marker into the existing ``failed_reason`` column (no
  schema change), the exact string
  ``operator mark-failed in progress (blocklist=yes|no, remove=yes|no)`` — see
  ``_operator_fail_marker`` / ``_parse_operator_fail_marker``. A residual that
  outlives the claim (Phase-C exhaustion, process crash) is re-derived by the
  reconcile heal WITH the operator's original semantics: ``remove=no`` skips the
  Phase-B removal, ``blocklist=no`` skips the blocklist row, and the blocklist
  reason stays ``user_reported`` (the operator vocabulary) rather than ``failed``.
  Completion replaces the marker with the final human-readable reason ("marked
  failed by operator"), so the marker never survives on a terminal row. A
  ``failed_pending`` row whose ``failed_reason`` is absent, unrelated free text, or
  a malformed marker parses to ``None`` and heals with today's reconcile-default
  semantics (blocklist + removal) — genuinely reconcile-derived rows are unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

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


@dataclass(frozen=True)
class _OperatorFailFlags:
    """An operator ``mark_failed``'s explicit semantics, carried as provenance.

    ``blocklist``: whether the operator asked for a Blocklist row. ``remove_torrent``:
    whether the operator asked for the torrent + data to be removed from the client.
    Threaded through both provenance mechanisms (the live claim registry and the
    persisted ``failed_reason`` marker — module docstring) so the reconcile heal of
    an operator-initiated ``failed_pending`` residual runs with the operator's
    ORIGINAL choices, never the reconcile defaults.
    """

    blocklist: bool
    remove_torrent: bool


# Live claim registry (module docstring, "Operator provenance"): download id -> the
# in-flight operator mark_failed's flags. Registered before mark_failed's Phase-A
# commit, cleared in its ``finally``. Read/written ONLY via synchronous dict ops on
# the single event loop (single-process deployment by design — see
# web/routers/settings.py's _rotate_lock), so no lock is required.
_operator_fail_claims: dict[int, _OperatorFailFlags] = {}

# The persisted provenance marker (module docstring): the EXACT ``failed_reason``
# string mark_failed's Phase A stamps, and the ONLY form the heal parses. Anything
# else (absent / free text / malformed) parses to ``None`` -> reconcile-default
# semantics. Human-readable on purpose: ``failed_reason`` surfaces in the queue UI
# during the (normally brief) ``failed_pending`` window.
_OPERATOR_FAIL_MARKER_RE: Final = re.compile(
    r"^operator mark-failed in progress \(blocklist=(yes|no), remove=(yes|no)\)$"
)

_OPERATOR_FAIL_FINAL_REASON: Final = "marked failed by operator"


def _operator_fail_marker(flags: _OperatorFailFlags) -> str:
    """Render the Phase-A ``failed_reason`` provenance marker for ``flags``."""
    return (
        "operator mark-failed in progress "
        f"(blocklist={'yes' if flags.blocklist else 'no'}, "
        f"remove={'yes' if flags.remove_torrent else 'no'})"
    )


def _parse_operator_fail_marker(failed_reason: str | None) -> _OperatorFailFlags | None:
    """Parse a ``failed_reason`` back into operator flags, or ``None``.

    ``None`` means "no operator provenance" — the row is healed with the
    reconcile-default semantics. Deliberately tolerant: an absent reason, unrelated
    free text (a reconcile transition reason), or an unknown/malformed marker all
    return ``None`` rather than raising, so a genuinely reconcile-derived row (or a
    future marker revision) can never wedge the heal.
    """
    if failed_reason is None:
        return None
    match = _OPERATOR_FAIL_MARKER_RE.match(failed_reason)
    if match is None:
        return None
    return _OperatorFailFlags(
        blocklist=match.group(1) == "yes",
        remove_torrent=match.group(2) == "yes",
    )


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


@dataclass(frozen=True)
class _FailureCompletion:
    """One failed download to complete in Phase B/C, WITH its provenance.

    ``blocklist`` / ``remove_torrent`` are the semantics this failure completes
    under: reconcile defaults (both ``True``) for a genuinely reconcile-derived
    failure, or the operator's original flags for a marker-carrying residual (see
    the module docstring's "Operator provenance"). ``blocklist_reason`` keeps the
    vocabulary honest: ``failed`` for a reconcile detection, ``user_reported`` for
    an operator decision the heal is finishing. ``download_id`` is what the claim
    registry is checked against (before Phase B AND again inside Phase C).
    """

    download_id: int
    event: DownloadFailed
    blocklist: bool
    remove_torrent: bool
    blocklist_reason: str


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
    completion: _FailureCompletion,
    rows: list[DownloadRecord],
) -> _FailedReArm | None:
    """Phase C completion for one failed download: advance ``failed_pending`` ->
    ``Failed`` (compare-and-swap), write the blocklist row (when the completion's
    provenance asks for one), and return the owning request/season to re-arm (or
    ``None`` if the row has no owning request or the row was already completed by a
    concurrent writer).

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

    The winning CAS writes ``completion.event.reason`` as the FINAL ``failed_reason``
    -- for an operator-marker residual that is the human-readable
    ``_OPERATOR_FAIL_FINAL_REASON``, so the Phase-A provenance marker never survives
    onto a terminal row.

    Deferring the blocklist + ``Failed`` advance out of Phase A (they used to commit
    there) is what makes an exhausted Phase C leave a RECONCILABLE ``failed_pending``
    row rather than an un-healable terminal ``Failed`` (see the module docstring).
    """
    event = completion.event
    record = next((r for r in rows if r.id == completion.download_id), None)
    if record is None:
        # Every completion is built from a row in ``rows`` (a reconcile transition
        # or the strand re-derivation), so this is unreachable in practice; guard
        # rather than fail on a row we cannot advance.
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

    request_repo = SqlRequestRepository(session)
    request = (
        await request_repo.get(record.media_request_id)
        if record.media_request_id is not None
        else None
    )
    # Honor the completion's provenance: an operator residual with blocklist=no is
    # healed WITHOUT a blocklist row -- the operator's explicit choice survives the
    # heal (the whole point of the marker; see the module docstring).
    if completion.blocklist:
        source_title = (
            await blocklist_service.source_title_for(session, event.torrent_hash)
            or event.source_title
        )
        indexer = await blocklist_service.indexer_for(session, event.torrent_hash)
        await SqlBlocklistRepository(session).create(
            source_title=source_title,
            reason=completion.blocklist_reason,
            tmdb_id=request.tmdb_id if request is not None else event.tmdb_id,
            torrent_hash=event.torrent_hash,
            indexer=indexer,
            # Scope by media namespace so this entry can't reject a movie/show that
            # happens to share the tmdb id. Prefer the owning request; fall back to
            # the persisted download metadata or season scope for orphan rows.
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
    #      blocklist/re-arm for a state change that never persisted). These are
    #      genuinely reconcile-detected failures: reconcile-default provenance.
    #   2. Rows ALREADY at ``failed_pending`` in the cycle-start snapshot -- a prior
    #      cycle/operator advanced them there but Phase C never completed (a strand).
    #      They are disjoint from (1) BY CONSTRUCTION: a row transitioned to
    #      ``failed_pending`` this cycle had a DIFFERENT snapshot status (its
    #      from_state, e.g. ``downloading``/``client_missing``), never
    #      ``failed_pending``. Without this re-derivation such a strand would linger
    #      forever -- the pure reconciler skips ``failed_pending`` (not an ACTIVE
    #      state), so it emits no transition and no fresh event. THIS is the concrete
    #      heal a Phase-C failure relies on. Each strand's ``failed_reason`` is
    #      parsed for the operator provenance marker (module docstring): a marker
    #      row heals with the operator's ORIGINAL flags (skip removal / skip
    #      blocklist as chosen, ``user_reported`` reason, operator final reason);
    #      anything else heals with reconcile defaults.
    row_id_by_hash = {r.torrent_hash.lower(): r.id for r in rows}
    completions: list[_FailureCompletion] = []
    for event in failed_download_events(applied_transitions, rows, occurred_at=now):
        event_row_id = row_id_by_hash.get(event.torrent_hash.lower())
        if event_row_id is None:  # pragma: no cover - events derive from ``rows``
            continue
        completions.append(
            _FailureCompletion(
                download_id=event_row_id,
                event=event,
                blocklist=True,
                remove_torrent=True,
                blocklist_reason=BlocklistReason.failed.value,
            )
        )
    for row in rows:
        if row.status != DownloadState.FailedPending.value:
            continue
        operator_flags = _parse_operator_fail_marker(row.failed_reason)
        completions.append(
            _FailureCompletion(
                download_id=row.id,
                event=DownloadFailed(
                    torrent_hash=row.torrent_hash,
                    source_title=row.torrent_hash,
                    reason=(
                        _OPERATOR_FAIL_FINAL_REASON
                        if operator_flags is not None
                        else row.failed_reason or "recovered stranded failed_pending row"
                    ),
                    tmdb_id=row.tmdb_id,
                    occurred_at=now,
                ),
                blocklist=operator_flags.blocklist if operator_flags is not None else True,
                remove_torrent=(
                    operator_flags.remove_torrent if operator_flags is not None else True
                ),
                blocklist_reason=(
                    BlocklistReason.user_reported.value
                    if operator_flags is not None
                    else BlocklistReason.failed.value
                ),
            )
        )

    # Live-claim filter (module docstring, "Operator provenance"): a row an operator
    # mark_failed currently has in flight is THAT call's to complete -- skipping it
    # here keeps this cycle's Phase B from removing a torrent the operator said to
    # keep, and its Phase C from stealing the failed_pending -> Failed CAS with the
    # wrong side effects. If the operator call later fails, its claim is cleared and
    # the marker-carrying residual heals (with the operator's flags) next cycle.
    deferred = [c for c in completions if c.download_id in _operator_fail_claims]
    completions = [c for c in completions if c.download_id not in _operator_fail_claims]
    for completion in deferred:
        _logger.info(
            "deferring reconcile completion of download %s: an operator mark-failed "
            "holds the claim",
            safe_int(completion.download_id),
        )

    if not completions:
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
    # the Phase C completion below (the row still lands Failed + re-armed). An
    # operator residual whose marker says remove=no is honored: no removal.
    for completion in completions:
        if not completion.remove_torrent:
            continue
        await purge_service.remove_torrent(
            qbt,
            completion.event.torrent_hash,
            context="a reconcile-driven download failure",
            extra={
                "torrent_hash": completion.event.torrent_hash,
                "tmdb_id": completion.event.tmdb_id,
            },
        )

    # Phase C: complete each failure (failed_pending -> Failed + blocklist + re-arm)
    # in ONE bounded-retry transaction. On exhaustion the rows stay at the reconcilable
    # ``failed_pending`` for a later cycle's strand re-derivation to heal. The claim
    # registry is RE-CHECKED per completion: an operator mark_failed that claimed a
    # row after the filter above (its Phase A landing during this cycle's Phase B
    # await) must not have its completion stolen with the wrong semantics -- skip it;
    # the operator call (or, if it fails, the next cycle's heal) completes it.
    async def _complete_reconcile_failures() -> None:
        rearms: list[_FailedReArm] = []
        for completion in completions:
            if completion.download_id in _operator_fail_claims:
                continue
            rearm = await _handle_failed(session, completion, rows)
            if rearm is not None:
                rearms.append(rearm)
        for rearm in rearms:
            await _rearm_failed_request(session, rearm)

    await _commit_phase_c_with_retry(
        session,
        _complete_reconcile_failures,
        context="reconcile-driven failures",
        identity=[completion.event.torrent_hash for completion in completions],
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

    The operator's explicit ``blocklist`` / ``remove_torrent`` choices are carried
    as provenance so neither a concurrent reconcile tick nor the later heal can
    override them (module docstring, "Operator provenance"): the live claim
    registry is registered BEFORE the Phase-A commit (and cleared in ``finally``),
    so a reconcile cycle landing during the Phase-B await skips this row entirely;
    and Phase A stamps the structured ``failed_reason`` marker, so a residual that
    outlives this call (Phase-C exhaustion, crash) heals with THESE flags.
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

    # Operator provenance (module docstring): the live claim is registered BEFORE
    # any Phase-A write and cleared in the ``finally`` below, so a reconcile cycle
    # observing the committed ``failed_pending`` row at ANY point while this call is
    # in flight defers to it. The marker persists the same flags for a residual that
    # outlives the claim (Phase-C exhaustion / crash).
    flags = _OperatorFailFlags(blocklist=blocklist, remove_torrent=remove_torrent)
    marker = _operator_fail_marker(flags)
    _operator_fail_claims[download_id] = flags
    try:
        # Route to the pre-terminal ``failed_pending`` pause. The legal graph reaches
        # ``Failed`` only from ``failed_pending``, so an actively Downloading torrent
        # (etc.) must pass through it first. A row ALREADY at ``failed_pending`` (a
        # reconcile detection or a stranded prior attempt) is re-stamped with THIS
        # call's marker via a same-state CAS instead: the operator's flags now own
        # the residual (they are the most recent explicit instruction), and a losing
        # CAS means the row was just completed under us -- surfaced as the same 409
        # as any other lost race.
        if current is not DownloadState.FailedPending:
            if not is_legal_transition(current, DownloadState.FailedPending):
                raise InvalidStateTransitionError(current.value, DownloadState.Failed.value)
            pending = await download_repo.update_status_if_in(
                download_id,
                DownloadState.FailedPending.value,
                frozenset({current.value}),
                failed_reason=marker,
            )
            if not pending:
                await _raise_current_transition()
        else:
            stamped = await download_repo.update_status_if_in(
                download_id,
                DownloadState.FailedPending.value,
                frozenset({DownloadState.FailedPending.value}),
                failed_reason=marker,
            )
            if not stamped:
                await _raise_current_transition()

        # Phase A commit: the row is at ``failed_pending`` (reconcilable), carrying
        # the provenance marker. The blocklist, the terminal ``Failed`` advance, and
        # the re-arm are NOT yet written.
        await session.commit()

        # Phase B: close the seeding leak (ADR-0014) and, per issue #68, remove the
        # old torrent BEFORE re-arming. Best-effort + already-gone-is-a-no-op (see
        # ``purge_service.remove_torrent``): a client hiccup never undoes the
        # committed Phase A, and -- because removal is logged-not-raised -- never
        # blocks Phase C. ``qbt is not None`` is guaranteed by the top-of-function
        # guard whenever ``remove_torrent`` is True; the explicit check narrows the
        # optional type.
        if remove_torrent and qbt is not None:
            await purge_service.remove_torrent(
                qbt,
                torrent_hash,
                context="an operator mark-failed",
                extra={"torrent_hash": torrent_hash, "download_id": safe_int(download_id)},
            )

        # Phase C: complete ``failed_pending`` -> ``Failed`` (CAS) + optional
        # blocklist + re-arm, in one bounded-retry transaction. The winning CAS
        # replaces the Phase-A marker with the final human-readable reason. A losing
        # terminal CAS means a concurrent completer already finished this row: honor
        # it (the operator's intent -- fail the download -- is satisfied) and skip
        # the duplicate blocklist/re-arm. On retry exhaustion the row stays at the
        # reconcilable ``failed_pending`` WITH the marker, so the reconcile loop
        # heals it under these same flags.
        async def _complete_mark_failed() -> None:
            won = await download_repo.update_status_if_in(
                download_id,
                DownloadState.Failed.value,
                frozenset({DownloadState.FailedPending.value}),
                failed_reason=_OPERATOR_FAIL_FINAL_REASON,
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
                    # request; fall back to the persisted metadata or season scope.
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
    finally:
        # Clear the live claim on EVERY exit -- success, a 409'd lost race, or a
        # Phase-C exhaustion. After an exhaustion the persisted marker (not the
        # claim) is what carries the flags to the reconcile heal.
        _operator_fail_claims.pop(download_id, None)

    failed = await download_repo.get_by_hash(torrent_hash)
    if failed is None:  # pragma: no cover - just updated this row
        raise LookupError(f"download {download_id} vanished mid-update")
    return failed
