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
(``blocklist=False`` / ``remove_torrent=False``). Ownership is DURABLE and
PREDICATE-ATOMIC (the final protocol form):

* **The nonce-marker is provenance AND durable ownership.** :func:`mark_failed`'s
  Phase A stamps the exact string ``operator mark-failed in progress
  (blocklist=yes|no, remove=yes|no|done, nonce=<token>)`` into the existing
  ``failed_reason`` column (no schema change) — see ``_operator_fail_marker`` /
  ``_parse_operator_fail_marker``. The nonce is the registering call's monotonic
  claim token, so the marker value identifies exactly WHICH call owns the row,
  durably (it survives crashes and Phase-C exhaustion, unlike the in-process
  registry). The marker also records the removal OUTCOME: immediately after a
  row's delete succeeds (either actor's Phase B), the marker is CAS-restamped to
  ``remove=done`` and committed at once — an operator ``remove=yes`` marker
  keeps its flags and nonce (ownership unchanged), and a PLAIN (unmarked)
  reconcile failure gets the reconcile-provenance record ``reconcile failure in
  progress (blocklist=yes, remove=done, nonce=0)`` (honest actor prefix,
  reconcile-default flags, the fixed nonce-0 reconcile tag — operator tokens
  start at 1). Either way a later Phase-C exhaustion leaves a residual every
  healer can complete WITHOUT a client — a removal that happened is never
  forgotten, and a removal that failed keeps saying it is owed.
* **Every decision-critical mutation is one predicate-atomic CAS.** WHY no
  check-then-act window remains: each side-effect-committing UPDATE re-proves
  BOTH the lifecycle position (``status``) AND the ownership (the exact
  ``failed_reason`` value this actor observed or owns) in the SAME statement's
  WHERE, so the decision and the write cannot be separated by an await — a
  concurrent ownership change rewrites ``failed_reason`` and the stale statement
  then matches 0 rows atomically at the database. Concretely:

  1. **Stamps/restamps are CAS UPDATEs.** The initial stamp rides the
     status-changing CAS (``downloading`` etc. -> ``failed_pending``); an ADOPT
     restamp CASes on ``status='failed_pending' AND failed_reason = <the exact
     value this call observed>``. An older call cannot clobber a newer call's
     marker — its WHERE misses; it then yields if it no longer owns the registry
     claim, or (still-owner: only an OLDER stamp can have intervened) re-observes
     and retries — each competitor's own predicate can never match again after
     the newer stamp lands, so the loop converges. A stale-snapshot CAS loss
     against a row that is currently at the uncompleted ``failed_pending``
     ADOPTS it (re-reads once, restamps) instead of 409ing; only a genuinely
     non-adoptable state raises.
  2. **mark_failed's terminal CAS carries ITS marker in the WHERE**
     (``failed_pending`` -> ``failed`` WHERE ``failed_reason = <my exact
     nonce-marker>``): a newer restamp defeats the older call atomically at the
     DB — no post-CAS token re-check exists or is needed. A miss yields, logged;
     the newer owner (or the heal) completes with the owning flags.
  3. **Reconcile's terminal CAS carries its OBSERVED reason in the WHERE**
     (:func:`_handle_failed` via ``_FailureCompletion.observed_failed_reason``):
     an operator nonce-marker landing during ANY of the cycle's awaits — before
     Phase C, or even after a Phase-B removal already ran — changes
     ``failed_reason``, so the stale completion's CAS matches 0 rows and drops,
     explicitly logged; the marker-carrying residual heals on a later cycle with
     the owning flags. **Phase B re-proves durably too**: removal is I/O (it
     cannot itself be predicate-gated), so immediately before each delete the
     row is re-read fresh — if it is no longer ``failed_pending`` with the exact
     observed reason (an operator stamped/completed/released since the
     snapshot, leaving a durable record but no live claim), the removal is
     skipped and the completion dropped, logged with the cause. The
     removal-physics guard is registered synchronously BEFORE that re-proof's
     first await: no yield point separates "operators are barred" from "the
     row's durable state is verified", so an operator either finished before
     the bar (the re-proof sees it) or is refused by the guard — a skip
     releases the bar immediately (nothing ran, nothing to settle).
  4. Every yield/drop is backstopped by the marker: the row is left either still
     active (nothing stamped — reconcile resumes normally once unclaimed) or as a
     marker-carrying ``failed_pending`` residual whose next completion honors the
     marker's flags. No yield path strands the row.

* **The in-process registry remains ONLY for what the DB cannot express**
  (``_operator_fail_claims`` + ``_reconcile_removals_in_flight``):

  - **Removal physics (BOTH actors).** Removal is client I/O,
    not a DB row mutation, so it cannot be predicate-gated: once a delete await
    has started the remove decision is irreversible, and
    ``_register_operator_claim`` REFUSES registration
    (:class:`RemovalInProgressError` -> HTTP 409 ``removal_in_progress``) while
    one is in flight. Operator side: the claim is flagged removal-in-flight
    immediately before :func:`mark_failed`'s delete await, held until release.
    Reconcile side: each automatic Phase-B delete registers its download id in
    ``_reconcile_removals_in_flight`` from just before the delete await until
    that row's removal CONSEQUENCE settles — its completion commits the
    terminal CAS, or is dropped/deferred in Phase C, or the cycle's Phase C
    exhausts — released at cycle scope, so the returned-but-unsettled gap
    between a delete and its completion cannot admit a ``remove_torrent=False``
    command for data this cycle already destroyed.
  - **Pre-stamp invisibility fast-path.** A claim exists from BEFORE the marker
    is stamped, so reconcile skips claimed ids at every phase boundary (Phase-A
    transition application; Phase B, where a claimed completion is DROPPED from
    the whole cycle — it was built pre-marker, so even a claim released seconds
    later must not let this cycle apply stale semantics; Phase C per completion,
    every retry attempt). These are fast-path courtesies for the window the
    marker cannot yet cover — the terminal CAS predicates above remain the
    authoritative guard.
  - Registration REPLACES (the newest command owns; older tokens silently stop
    owning); release and the removal-in-flight flag are token-gated, so a
    superseded finisher can never clear the newer command's live claim. Safe as
    in-process state: single-process deployment by design (the same assumption
    ``web/routers/settings.py``'s ``_rotate_lock`` documents), and the registry
    is only touched by synchronous dict/set operations on the one event loop —
    no await inside any read-modify-write, so no lock is needed.
  - **The "newest wins" replace rule is an OPERATOR-vs-OPERATOR rule only**
    (issue #165 hardening finding). The stalled-download self-heal
    (:func:`_self_heal_stalled_download`) calls :func:`mark_failed` with
    ``allow_claim_supersede=False``, so its own registration attempt REFUSES
    (:class:`OperatorClaimActiveError`) instead of replacing whenever ANY claim
    already exists. This closes a race the caller-side pre-check
    (``reconcile_and_list``'s ``_is_operator_claimed``) cannot: that check and
    self-heal's own later registration are separated by self-heal's own
    ``session.get`` await inside ``mark_failed``, a window an operator's manual
    call can register a claim in without the pre-check ever seeing it. Because
    the re-check happens in the SAME synchronous step that would otherwise grant
    the claim, no further await separates "another owner might exist" from "I am
    now the owner" for the self-heal path — closing the gap outright rather than
    narrowing it. An operator's OWN calls are unaffected: two operator commands
    racing each other still resolve newest-wins, exactly as before.
  - **A released claim is not the same as an unclaimed row** (Codex review,
    comment 3541100611 — the narrower race left open by the above). The claim
    above is LIVE, in-process state, released the instant its owning call
    returns — but the ``failed_pending`` row + durable marker it stamped can
    outlive that release (e.g. Phase-C exhaustion, or a clean completion whose
    terminal CAS simply hasn't run yet). ``allow_claim_supersede=False`` alone
    only refuses a claim that is STILL live; it has nothing to say about a row
    an operator's (or reconcile's) call already finished with and moved on from.
    Self-heal ALSO passes ``allow_adopt_existing_marker=False``, which refuses
    (:class:`FailedPendingAdoptionRefusedError`) :func:`mark_failed`'s adopt
    branch outright whenever the row is already ``failed_pending`` at the point
    this call attempts it — live claim or not — rather than restamping someone
    else's durable provenance with the reconcile-default ``blocklist=True,
    remove_torrent=True``. An operator's OWN calls keep the default
    ``allow_adopt_existing_marker=True``: adopting an abandoned residual and
    driving it to completion with the newest explicit instruction is exactly
    what the operator-facing endpoint is FOR.

* **Healing honors the marker.** A residual re-derived by
  :func:`reconcile_and_list` (or, when qBittorrent is unconfigured OR the cycle
  hits a client outage, by the narrow DB-only
  :func:`heal_failed_pending_without_client`, which completes exactly the
  ``remove=no`` / ``remove=done`` marker residuals — neither needs client I/O)
  runs with the operator's ORIGINAL semantics:
  ``remove=no`` skips the removal, ``blocklist=no`` skips the blocklist row, and
  a written blocklist keeps the ``user_reported`` reason. Completion replaces the
  marker with the final human-readable reason ("marked failed by operator"), so
  the marker never survives on a terminal row. An absent / free-text / malformed
  ``failed_reason`` parses to no-provenance and heals with the reconcile-default
  semantics (blocklist + removal) — genuinely reconcile-derived rows unchanged.

* **Residual: the pre-stamp window** (accepted, tracked in issue #127). Between
  a mark_failed's claim registration and its marker becoming DURABLE (the Phase-A
  stamp committing), ownership exists only in the in-process registry — a
  concurrent actor already past its registry checks can race exactly the one
  in-flight statement in that sub-second window. The outcomes stay honest: the
  losing side's predicate CAS misses (it yields, or surfaces the already-terminal
  409 ``invalid_state_transition``), the row always lands terminal-and-consistent
  (never stranded, never double-completed), and the only possible divergence is a
  SIDE EFFECT the operator did not choose (e.g. a reconcile-default blocklist row
  an operator ``blocklist=False`` would have skipped) — operator-visible and
  reversible via the blocklist management UI, with the request re-armed either
  way. Closing it fully would need the claim itself to be a DB row; #127 tracks
  that trade-off.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from itertools import count
from typing import TYPE_CHECKING, Final

from sqlalchemy import update
from sqlalchemy.exc import SQLAlchemyError

from plex_manager.adapters.qbittorrent import QbittorrentError
from plex_manager.domain.download_payload import (
    EMPTY_PAYLOAD_REJECTION_REASON,
    format_payload_rejection,
    validate_payload_files,
)
from plex_manager.domain.events import DownloadFailed
from plex_manager.domain.reconciler import (
    StallDetection,
    StateTransition,
    detect_stalls,
    failed_download_events,
    reconcile,
    unmapped_client_states,
)
from plex_manager.domain.state_machine import (
    ACTIVE_STATES,
    TERMINAL_STATES,
    DownloadState,
    is_legal_transition,
)
from plex_manager.logsafe import safe_int, safe_text
from plex_manager.models import (
    BlocklistReason,
    Download,
    DownloadHistory,
    DownloadHistoryEvent,
    DownloadScope,
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

    from plex_manager.ports.download_client import DownloadClientPort, DownloadStatus
    from plex_manager.ports.repositories import DownloadRecord, QueueRecord

__all__ = [
    "FailedPendingAdoptionRefusedError",
    "InvalidStateTransitionError",
    "OperatorClaimActiveError",
    "RemovalInProgressError",
    "heal_failed_pending_without_client",
    "list_queue",
    "mark_failed",
    "reconcile_and_list",
]

_logger = logging.getLogger(__name__)

_TERMINAL_STATUS_VALUES = frozenset(s.value for s in TERMINAL_STATES)
_PAYLOAD_VALIDATION_STATUS_VALUES = frozenset(s.value for s in ACTIVE_STATES) | frozenset(
    {DownloadState.ImportBlocked.value}
)
_COMPLETE_PROGRESS = 1.0
_PAYLOAD_COMPLETE_RAW_STATES = frozenset(
    {"uploading", "stalledUP", "pausedUP", "stoppedUP", "queuedUP", "checkingUP", "forcedUP"}
)

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
_UNRESOLVED_SCOPE_STATUSES: Final[frozenset[str]] = frozenset({"active", "import_blocked"})


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


@dataclass(frozen=True)
class _OperatorClaim:
    """One live ownership claim: the monotonic token + the owner's flags.

    ``removal_in_flight`` (the removal-physics rule): set immediately before the owner's
    torrent-removal await. From that moment the remove decision is PHYSICALLY
    irreversible, so :func:`_register_operator_claim` refuses a replacement while
    it is set — the one place later-wins yields to physics.
    """

    token: int
    flags: _OperatorFailFlags
    removal_in_flight: bool = False


# Single-owner claim registry (module docstring, "Operator provenance"): download
# id -> the CURRENT owning operator mark_failed's claim. Registered (replacing any
# older owner) before mark_failed's Phase-A writes, released token-gated in its
# ``finally``. Read/written ONLY via the synchronous helpers below on the single
# event loop (single-process deployment by design — see web/routers/settings.py's
# _rotate_lock), so no lock is required.
_operator_fail_claims: dict[int, _OperatorClaim] = {}

_claim_tokens = count(1)

# Reconcile-side removal window (the removal-physics rule covers BOTH actors):
# download ids whose AUTOMATIC reconcile-driven Phase-B delete has started and
# whose CONSEQUENCE has not yet settled. ``_register_operator_claim`` refuses
# registration during this window for the same physics reason as the operator
# flag above — an operator ``remove_torrent=False`` claim registered mid-delete,
# OR in the gap between the delete's return and the row's Phase-C settlement,
# would complete promising a file this cycle already destroyed. An id enters just
# before its delete await and leaves at the CYCLE-SCOPE finally, once that row's
# completion committed its terminal CAS, was dropped/deferred in Phase C, or the
# cycle's Phase C exhausted (the residual is then settled-but-unhealed — plain,
# claimable, reconcilable).
_reconcile_removals_in_flight: set[int] = set()


def _register_operator_claim(
    download_id: int, flags: _OperatorFailFlags, *, allow_supersede: bool = True
) -> int:
    """Claim ``download_id`` for a mark_failed call; return the owner token.

    By default (``allow_supersede=True``, every OPERATOR-initiated call)
    REPLACES any existing claim: the newest operator command owns the row
    (protocol step 1) — the superseded call's token silently stops owning, so its
    later token-gated phases yield. The ONE exception (the removal-physics rule):
    while a torrent-removal I/O for this download is in flight — EITHER the
    current owner's (``removal_in_flight`` on the claim) OR reconcile's automatic
    Phase-B delete (``_reconcile_removals_in_flight``) — the remove decision is
    already irreversible, so registration is REFUSED with
    :class:`RemovalInProgressError` — accepting a ``remove_torrent=False`` command
    then would promise a file the in-flight delete is destroying. This physics
    check applies UNCONDITIONALLY, even when ``allow_supersede=False``.

    ``allow_supersede=False`` (the stalled-download self-heal's own path, issue
    #165 hardening finding) additionally refuses registration — with
    :class:`OperatorClaimActiveError` — whenever ANY claim already exists, live
    owner or not yet removal-in-flight. Self-heal must NEVER become the newer
    owner over an existing claim: unlike two racing operator commands (where
    "newest wins" is the correct, intentional semantics), a self-heal call
    superseding an operator's in-flight claim would silently override a live
    human decision the operator is still in the middle of committing. This check
    is the ONLY thing that closes that race, because it re-verifies atomically in
    the SAME synchronous step that would otherwise register the superseding
    claim — no ``await`` separates the check from the decision here, unlike the
    caller's own pre-check (:func:`_is_operator_claimed` in
    :func:`reconcile_and_list`), which races against exactly this registration.
    """
    if download_id in _reconcile_removals_in_flight:
        raise RemovalInProgressError(download_id)
    existing = _operator_fail_claims.get(download_id)
    if existing is not None and existing.removal_in_flight:
        raise RemovalInProgressError(download_id)
    if not allow_supersede and existing is not None:
        raise OperatorClaimActiveError(download_id)
    token = next(_claim_tokens)
    _operator_fail_claims[download_id] = _OperatorClaim(token=token, flags=flags)
    return token


def _mark_removal_in_flight(download_id: int, token: int) -> None:
    """Flag the owner's claim as removal-in-flight (removal-physics rule), token-gated.

    Called immediately before the owning mark_failed's ``qbt`` delete await; from
    then until the claim is released, :func:`_register_operator_claim` refuses
    supersession. Deliberately NOT cleared when the removal await returns: the
    deletion has happened (or been attempted) — a later command's differing
    ``remove_torrent`` choice is moot for this torrent either way, so the refusal
    holds for the remainder of the owning call. A stale (non-owning) token is a
    silent no-op, mirroring :func:`_release_operator_claim`.
    """
    claim = _operator_fail_claims.get(download_id)
    if claim is not None and claim.token == token:
        _operator_fail_claims[download_id] = _OperatorClaim(
            token=token, flags=claim.flags, removal_in_flight=True
        )


def _owns_operator_claim(download_id: int, token: int) -> bool:
    """Whether ``token`` is still the CURRENT owner of ``download_id``'s claim."""
    claim = _operator_fail_claims.get(download_id)
    return claim is not None and claim.token == token


def _is_operator_claimed(download_id: int) -> bool:
    """Whether ANY live operator claim exists for ``download_id`` (reconcile's view:
    a claimed id is invisible at every phase boundary — the pre-stamp
    invisibility fast-path)."""
    return download_id in _operator_fail_claims


def _release_operator_claim(download_id: int, token: int) -> None:
    """Release the claim, but ONLY if ``token`` still owns it (token-gated release).

    A stale finisher (an older superseded call reaching its ``finally``) must never
    clear the NEWER command's live claim — a token mismatch is a silent no-op.
    """
    claim = _operator_fail_claims.get(download_id)
    if claim is not None and claim.token == token:
        del _operator_fail_claims[download_id]


# The persisted provenance-AND-ownership marker (module docstring): the EXACT
# ``failed_reason`` string mark_failed's Phase A stamps, and the ONLY form the heal
# parses. The ``nonce`` is the registering call's monotonic claim token, making the
# marker a DURABLE ownership record: every side-effect CAS includes the exact
# marker value in its WHERE, so a newer call's restamp (a different nonce) defeats
# a stale mutation atomically at the database. Anything else (absent / free text /
# malformed) parses to ``None`` -> reconcile-default semantics. Human-readable on
# purpose: ``failed_reason`` surfaces in the queue UI during the (normally brief)
# ``failed_pending`` window.
_OPERATOR_FAIL_MARKER_RE: Final = re.compile(
    r"^(operator mark-failed|reconcile failure) in progress "
    r"\(blocklist=(yes|no), remove=(yes|no|done), nonce=(\d+)\)$"
)

_OPERATOR_FAIL_FINAL_REASON: Final = "marked failed by operator"

# The terminal reason a reconcile-provenance done-marker residual completes with
# (the marker itself must never survive onto a terminal row).
_RECONCILE_FAIL_FINAL_REASON: Final = "download failed; torrent already removed"

# The reconcile actor's fixed marker nonce. Registry tokens start at 1, so 0 can
# never collide with an operator call's token.
_RECONCILE_DONE_NONCE: Final = 0


def _operator_fail_marker(flags: _OperatorFailFlags, nonce: int) -> str:
    """Render the Phase-A ``failed_reason`` marker: provenance flags + owner nonce."""
    return (
        "operator mark-failed in progress "
        f"(blocklist={'yes' if flags.blocklist else 'no'}, "
        f"remove={'yes' if flags.remove_torrent else 'no'}, "
        f"nonce={nonce})"
    )


def _operator_fail_marker_removal_done(blocklist: bool, nonce: int) -> str:
    """Render the ``remove=done`` marker form: the removal OUTCOME, made durable.

    Stamped (CAS on the exact prior marker, same nonce -- ownership unchanged)
    immediately after a ``remove=yes`` marker row's torrent removal SUCCEEDS, and
    committed at once. Without it, a Phase-C exhaustion after the removal leaves a
    residual whose marker still claims the removal is owed -- the DB-only healer
    (outage / unconfigured client) would skip it forever even though the delete
    already happened. ``remove=done`` parses as "no client I/O needed" (exactly
    like ``remove=no``), so every healer can complete the strand.
    """
    return (
        "operator mark-failed in progress "
        f"(blocklist={'yes' if blocklist else 'no'}, remove=done, nonce={nonce})"
    )


def _reconcile_removal_done_marker() -> str:
    """Render the RECONCILE actor's durable removal-outcome record.

    Stamped (CAS'd on the observed plain/NULL reason, committed at once) after a
    successful reconcile-owned delete of an UNMARKED row, so a Phase-C exhaustion
    leaves a residual every healer can complete WITHOUT a client -- the same
    durability :func:`_operator_fail_marker_removal_done` gives operator rows.
    Same grammar, honest actor prefix ("reconcile failure", not "operator
    mark-failed"), reconcile-default flags (``blocklist=yes``), and the fixed
    ``nonce=0`` reconcile tag (operator tokens start at 1).
    """
    return (
        f"reconcile failure in progress (blocklist=yes, remove=done, nonce={_RECONCILE_DONE_NONCE})"
    )


@dataclass(frozen=True)
class _ParsedOperatorMarker:
    """A parsed failure marker: the owning actor, its flags, and its nonce.

    ``operator`` is True for an ``operator mark-failed`` marker (heals with the
    operator vocabulary: ``user_reported`` blocklist reason, the operator final
    reason) and False for a ``reconcile failure`` marker (the reconcile actor's
    durable ``remove=done`` outcome record -- heals with the reconcile-default
    vocabulary)."""

    operator: bool
    flags: _OperatorFailFlags
    nonce: int


def _parse_operator_fail_marker(failed_reason: str | None) -> _ParsedOperatorMarker | None:
    """Parse a ``failed_reason`` back into operator flags + owner nonce, or ``None``.

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
    return _ParsedOperatorMarker(
        operator=match.group(1) == "operator mark-failed",
        flags=_OperatorFailFlags(
            blocklist=match.group(2) == "yes",
            # ``done`` parses like ``no``: the removal already HAPPENED, so no
            # further client I/O is needed to complete the residual.
            remove_torrent=match.group(3) == "yes",
        ),
        nonce=int(match.group(4)),
    )


class InvalidStateTransitionError(Exception):
    """An operator move is illegal for the download's current state (HTTP 409)."""

    def __init__(self, frm: str, to: str) -> None:
        self.frm = frm
        self.to = to
        super().__init__(f"illegal transition {frm} -> {to}")


class RemovalInProgressError(Exception):
    """A torrent removal is already in flight for this download (HTTP 409,
    ``removal_in_progress``).

    The ownership protocol's removal-physics rule — the one place later-wins
    yields to physics,
    covering BOTH removal actors: once a ``qbt`` delete await has started —
    whether an operator mark-failed's Phase B or reconcile's automatic Phase-B
    delete — the remove decision is irreversible, so a mark_failed (whose flags
    could promise the opposite) is refused instead of registering a claim. Retry
    once the in-flight removal resolves.
    """

    def __init__(self, download_id: int) -> None:
        self.download_id = download_id
        super().__init__(
            f"a removal for download {download_id} is already in progress; retry after it completes"
        )


class OperatorClaimActiveError(Exception):
    """A NON-superseding claim attempt found an existing live claim (issue #165
    hardening finding, self-heal-vs-operator race).

    Raised only by a caller that registered with ``allow_supersede=False``
    (currently the stalled-download self-heal path only, via
    :func:`mark_failed`'s ``allow_claim_supersede``): an ordinary operator
    ``mark_failed`` call always supersedes (the module docstring's "newest
    command owns" rule is unchanged for operator-vs-operator races). Self-heal
    opts OUT of that rule instead, because it must never be the newer owner over
    ANY existing claim — including one an operator's own manual call registered
    AFTER ``reconcile_and_list``'s own ``_is_operator_claimed`` pre-check already
    ran and passed (that pre-check and self-heal's own registration are not one
    atomic step: the caller's ``await session.get`` in between is exactly the
    window an operator's call can land in). Re-checking atomically at
    registration time — the same synchronous critical section that decides
    ownership — closes that gap without touching the existing single-operator
    -click behaviour at all.
    """

    def __init__(self, download_id: int) -> None:
        self.download_id = download_id
        super().__init__(
            f"download {download_id} already has an active mark-failed claim; "
            "backing off rather than superseding it"
        )


class FailedPendingAdoptionRefusedError(Exception):
    """A caller that opted OUT of adopting an existing residual
    (``allow_adopt_existing_marker=False``) found the row ALREADY at
    ``failed_pending`` when it actually attempted the call (Codex review, comment
    3541100611 -- the narrower race left open by issue #165's claim-supersede
    fix).

    ``OperatorClaimActiveError`` guards the LIVE-claim window: it stops self-heal
    from superseding a claim that is still registered. But a claim is only
    in-process state -- it is released in the owning call's ``finally`` the
    moment that call returns (success, a lost race, or a Phase-C exhaustion that
    re-raises), while the row it stamped can be left behind as a DURABLE
    ``failed_pending`` residual (the marker survives; the claim does not). A
    caller arriving AFTER that release sees no live claim at all, so
    ``allow_claim_supersede`` never enters into it -- yet the row still carries
    someone else's provenance (an operator's explicit ``blocklist``/
    ``remove_torrent`` choice, or a reconcile failure awaiting completion).

    The stalled-download self-heal (:func:`_self_heal_stalled_download`) is the
    one caller that passes ``allow_adopt_existing_marker=False``: unlike a
    genuine operator instruction -- which IS entitled to adopt and restamp an
    abandoned residual with its own explicit flags, the newest human decision
    being authoritative -- self-heal has no standing to override a residual it
    did not create. It must back off and leave the residual for its OWN owner's
    provenance to drive completion (the reconcile heal machinery, or the
    operator's own retry), never restamp over it with the reconcile-default
    ``blocklist=True, remove_torrent=True``.
    """

    def __init__(self, download_id: int) -> None:
        self.download_id = download_id
        super().__init__(
            f"download {download_id} is already failed_pending with an existing "
            "residual; refusing to adopt/restamp it"
        )


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
    registry is checked against at every phase boundary (the pre-B filter, per hash
    immediately before each Phase-B removal await, per completion inside Phase C).

    ``observed_failed_reason`` is the EXACT ``failed_reason`` this completion's
    provenance was derived from (the row's snapshot value — ``None``, free text, or
    an operator nonce-marker). The terminal CAS includes it in its WHERE
    (``require_failed_reason``), making the completion PREDICATE-ATOMIC: if any
    actor restamped the row after this completion was built (a fresher ownership
    record), the CAS matches 0 rows and the stale completion drops — the ownership
    re-proof and the terminal write are one statement.
    """

    download_id: int
    event: DownloadFailed
    blocklist: bool
    remove_torrent: bool
    blocklist_reason: str
    observed_failed_reason: str | None


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


async def _mark_download_scopes_terminal(
    session: AsyncSession, download_id: int, status: str
) -> None:
    await session.execute(
        update(DownloadScope)
        .where(
            DownloadScope.download_id == download_id,
            DownloadScope.status.in_(_UNRESOLVED_SCOPE_STATUSES),
        )
        .values(status=status)
    )


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

    The terminal advance is a PREDICATE-ATOMIC CAS: ``failed_pending`` -> ``Failed``
    only while the row is still ``failed_pending`` AND its ``failed_reason`` still
    equals the exact value this completion's provenance was derived from
    (``observed_failed_reason``). The blocklist + re-arm are GATED on winning it:

    * **Idempotent self-heal.** :func:`reconcile_and_list` re-derives a failed event
      for a row already at ``failed_pending`` (a stranded prior Phase C). Gating on
      the CAS means re-running this NEVER writes a second blocklist row for a row
      that has since been completed.
    * **No double-processing.** A row sits at ``failed_pending`` across Phase B
      (external I/O). An operator ``mark_failed`` and the reconcile loop could both
      pick up the same ``failed_pending`` row; only the one that wins the CAS writes
      the blocklist / re-arm -- the loser no-ops.
    * **No check-then-act window.** An operator nonce-marker landing during ANY of
      this cycle's awaits changes ``failed_reason``, so this stale completion's CAS
      matches 0 rows atomically at the database -- the ownership re-proof and the
      terminal write are the same statement. The drop is logged (below), and the
      fresher marker's residual heals with ITS flags on a later cycle.

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

    # Complete the FailedPending -> Failed transition (predicate-atomic CAS; see
    # the docstring). The reconciler only moves the row as far as
    # ``failed_pending``; without this advance the row would be stranded there --
    # neither terminal (so it lingers in ``list_active`` and the queue shows a
    # zombie torrent) nor active (the reconciler skips ``failed_pending``, only
    # revisiting it via THIS Phase C). A losing CAS means a concurrent writer
    # completed the row OR restamped fresher ownership onto it: honor that, write
    # nothing more.
    won = await SqlDownloadRepository(session).update_status_if_in(
        record.id,
        DownloadState.Failed.value,
        frozenset({DownloadState.FailedPending.value}),
        failed_reason=event.reason,
        require_failed_reason=completion.observed_failed_reason,
    )
    if not won:
        _logger.info(
            "dropping stale completion of download %s: its terminal CAS matched no "
            "row (completed by a concurrent writer, or a fresher ownership marker "
            "was stamped); any marker-carrying residual heals on a later cycle "
            "with the owning flags",
            safe_int(completion.download_id),
        )
        return None
    await _mark_download_scopes_terminal(session, record.id, RequestStatus.failed.value)

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

    if record.scopes:
        media_type = _media_type_for_blocklist(
            record, request.media_type if request is not None else None
        )
        for scope in record.scopes:
            if scope.media_request_id is not None and scope.season is not None:
                await _rearm_failed_request(
                    session,
                    _FailedReArm(
                        media_request_id=scope.media_request_id,
                        season=scope.season,
                        media_type=media_type,
                    ),
                )
        return None

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


async def _self_heal_stalled_download(
    session: AsyncSession,
    qbt: DownloadClientPort,
    row: DownloadRecord,
    detection: StallDetection,
    *,
    now: datetime,
) -> None:
    """Issue #165's minimal self-heal: mark-fail + blocklist a stalled download —
    the EXACT same :func:`mark_failed` call the operator's manual "mark failed +
    blocklist" button makes, so the existing single-owner claim/nonce protocol,
    blocklist write, torrent removal, and request/season re-arm are reused
    verbatim (module docstring). Re-arming returns the owner to ``searching``,
    which re-enters the EXISTING auto-grab backoff ladder
    (``auto_grab_service.BACKOFF_SCHEDULE``) — no separate retry cap is added
    here; a pathological title still bounds out at the honest,
    retryable ``no_acceptable_release`` park state on exhaustion.

    A losing race (an operator claimed the row, or it left the legal
    Downloading/MetadataFetching states, between this cycle's snapshot and this
    call) is logged and skipped rather than raised — one stalled row's edge case
    must never abort the whole reconcile cycle (imports, availability, every
    OTHER row) for this tick.

    Passes ``allow_claim_supersede=False`` (issue #165 hardening finding): this
    call must never become the newer owner over an EXISTING claim, even one that
    only registered after ``reconcile_and_list``'s own ``_is_operator_claimed``
    pre-check already ran (that pre-check races this call's own ``session.get``
    inside :func:`mark_failed` -- see :class:`OperatorClaimActiveError`). Caught
    below exactly like the other losing-race outcomes.

    Also passes ``allow_adopt_existing_marker=False`` (Codex review, comment
    3541100611 -- the narrower race left open by the claim-supersede fix above):
    a claim is only LIVE state, released the instant its owning call returns, but
    the ``failed_pending`` row + marker it stamped can durably outlive that
    release. If an operator's own ``mark_failed`` (or a reconcile failure) landed
    on this row and finished — releasing its claim — in the window between this
    cycle's stall snapshot and this call, ``allow_claim_supersede=False`` sees no
    live claim to refuse and would otherwise walk straight into
    :func:`mark_failed`'s adopt branch, restamping someone else's durable
    residual with the reconcile-default ``blocklist=True, remove_torrent=True``.
    ``allow_adopt_existing_marker=False`` closes that: :class:`mark_failed`
    refuses (:class:`FailedPendingAdoptionRefusedError`) instead of adopting
    whenever it finds the row already ``failed_pending``, live claim or not.
    Caught below exactly like the other losing-race outcomes.
    """
    elapsed = now - row.added_at if row.added_at is not None else None
    try:
        await mark_failed(
            session,
            qbt,
            download_id=row.id,
            blocklist=True,
            remove_torrent=True,
            allow_claim_supersede=False,
            allow_adopt_existing_marker=False,
        )
    except (
        InvalidStateTransitionError,
        RemovalInProgressError,
        OperatorClaimActiveError,
        FailedPendingAdoptionRefusedError,
        LookupError,
    ) as exc:
        _logger.warning(
            "self-heal (issue #165) skipped for download %s (%s, elapsed=%s): %s",
            safe_int(row.id),
            detection.shape,
            elapsed,
            exc,
        )
        return
    _logger.warning(
        "self-heal (issue #165): download %s auto mark-failed + blocklisted after a "
        "%s stall (elapsed=%s)",
        safe_int(row.id),
        detection.shape,
        elapsed,
    )
    session.add(
        DownloadHistory(
            tmdb_id=row.tmdb_id,
            torrent_hash=row.torrent_hash,
            event_type=DownloadHistoryEvent.stalled,
            source_title=row.release_title,
            message=f"auto-detected {detection.shape} stall (elapsed={elapsed}); "
            "mark-failed + blocklisted, replacing",
        )
    )
    await session.commit()


def _payload_failure_transition(row: DownloadRecord, reason: str) -> StateTransition:
    return StateTransition(
        download_id=row.id,
        torrent_hash=row.torrent_hash,
        from_state=row.status,
        to_state=DownloadState.FailedPending,
        reason=reason,
    )


def _payload_manifest_is_complete(live: DownloadStatus) -> bool:
    return live.progress >= _COMPLETE_PROGRESS or live.raw_state in _PAYLOAD_COMPLETE_RAW_STATES


async def _payload_safety_transitions(
    qbt: DownloadClientPort,
    rows: list[DownloadRecord],
    snapshot: dict[str, DownloadStatus],
) -> list[StateTransition]:
    """Fail torrents whose manifest contains anything except video/subtitle files."""
    transitions: list[StateTransition] = []
    for row in rows:
        if row.status not in _PAYLOAD_VALIDATION_STATUS_VALUES:
            continue
        live = snapshot.get(row.torrent_hash.lower())
        if live is None:
            continue
        complete = _payload_manifest_is_complete(live)
        if live.progress <= 0 and not complete:
            continue
        if _is_operator_claimed(row.id):
            continue

        try:
            files = await qbt.list_files(row.torrent_hash)
        except QbittorrentError as exc:
            _logger.warning(
                "download %s: could not validate torrent payload manifest (%s); "
                "deferring payload safety decision",
                safe_text(row.torrent_hash),
                type(exc).__name__,
            )
            continue
        if not files:
            if complete:
                transitions.append(_payload_failure_transition(row, EMPTY_PAYLOAD_REJECTION_REASON))
            continue

        validation = validate_payload_files(files)
        if not validation.accepted:
            reason = format_payload_rejection(validation)
            transitions.append(_payload_failure_transition(row, reason))

    return transitions


async def list_queue(session: AsyncSession) -> list[QueueRecord]:
    """Read-only snapshot of the active queue — NO reconcile, NO writes.

    The background reconcile loop (``web.app._reconcile_loop``) is the single owner
    of cross-system truth (overview §5, north-star #5): it reconciles the client and
    refreshes progress/seed_ratio on a fixed cadence. A GET /queue poll must NOT also
    reconcile — running ``reconcile_and_list`` concurrently with the loop can clobber
    the importer's CAS-claimed ``importing`` status (the per-download import lock does
    not cover this write path), stranding a placed file until a later cycle. So the
    read path is passive: it returns the currently persisted rows and writes nothing.
    The loop's frequent refresh keeps the listed progress/status fresh enough for
    display.

    Uses ``list_active_for_queue`` (issue #134), NOT ``list_active`` -- the queue view
    needs the MediaRequest-joined ``title``/``poster_url`` for a human-legible row;
    ``list_active`` stays untouched, still used by :func:`reconcile_and_list` so the
    domain/reconcile contract is unaffected by this presentation-only join.
    """
    return await SqlDownloadRepository(session).list_active_for_queue()


async def reconcile_and_list(
    qbt: DownloadClientPort,
    session: AsyncSession,
) -> list[DownloadRecord]:
    """Reconcile active downloads against the client and return the live queue."""
    download_repo = SqlDownloadRepository(session)
    rows = await download_repo.list_active()
    statuses = await qbt.get_all_statuses()
    now = _utcnow()

    snapshot = {status.info_hash.lower(): status for status in statuses}
    transitions = reconcile(rows, statuses, now=now)
    payload_transitions = await _payload_safety_transitions(qbt, rows, snapshot)

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
    transitions_by_id.update(
        {transition.download_id: transition for transition in payload_transitions}
    )
    applied_transitions: list[StateTransition] = []
    for row in rows:
        # Pre-stamp invisibility fast-path, Phase A: a claimed row is INVISIBLE — apply no
        # transition (and no progress write) to it. Without this, a mark_failed that
        # registered its claim but had not yet stamped the provenance marker could
        # have reconcile move the same row to an UNMARKED ``failed_pending`` first:
        # the operator's own Phase-A CAS would then lose, its command 409s, and the
        # residual heals as reconcile-owned — the operator's flags silently lost.
        # Checked per row, synchronously before this row's await, so a claim
        # registered during an EARLIER row's await is still honored.
        if _is_operator_claimed(row.id):
            continue
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

    # Stalled-download self-heal (issue #165's minimal fixed-cooldown design):
    # reuses THIS cycle's already-fetched ``rows``/``statuses`` (no new interval,
    # no extra client call — see ``domain.reconciler.detect_stalls``). A
    # detection whose row this SAME cycle's reconcile above already moved (it's
    # in ``transitions_by_id``) is skipped -- it changed domain state THIS tick,
    # so the pre-cycle stall read is stale for it and next cycle re-evaluates it
    # fresh. A row an operator ``mark_failed`` currently owns is likewise skipped
    # (the pre-stamp invisibility fast-path, module docstring) -- the operator's
    # own call, not this heal, is what completes it.
    rows_by_id = {row.id: row for row in rows}
    for detection in detect_stalls(rows, statuses, now=now):
        if detection.download_id in transitions_by_id or _is_operator_claimed(
            detection.download_id
        ):
            continue
        stalled_row = rows_by_id.get(detection.download_id)
        if stalled_row is None:  # pragma: no cover - detections derive from rows
            continue
        await _self_heal_stalled_download(session, qbt, stalled_row, detection, now=now)

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
    rows_by_hash = {r.torrent_hash.lower(): r for r in rows}
    completions: list[_FailureCompletion] = []
    for event in failed_download_events(applied_transitions, rows, occurred_at=now):
        event_row = rows_by_hash.get(event.torrent_hash.lower())
        if event_row is None:  # pragma: no cover - events derive from ``rows``
            continue
        completions.append(
            _FailureCompletion(
                download_id=event_row.id,
                event=event,
                blocklist=True,
                remove_torrent=True,
                blocklist_reason=BlocklistReason.failed.value,
                # The Phase-A transition CAS never writes ``failed_reason``, so the
                # cycle-start snapshot value is still the row's current reason; the
                # terminal CAS re-proves that atomically (an operator marker landing
                # mid-cycle changes it and defeats this completion at the DB).
                observed_failed_reason=event_row.failed_reason,
            )
        )
    for row in rows:
        if row.status != DownloadState.FailedPending.value:
            continue
        marker = _parse_operator_fail_marker(row.failed_reason)
        # Actor-aware residual semantics: an OPERATOR marker heals with the
        # operator vocabulary; a RECONCILE done-record (a plain failure whose
        # removal already happened) heals with the reconcile defaults minus the
        # already-performed removal; an unmarked strand heals as today. The
        # marker itself never survives onto the terminal row.
        if marker is not None and marker.operator:
            final_reason = _OPERATOR_FAIL_FINAL_REASON
            blocklist_reason = BlocklistReason.user_reported.value
        elif marker is not None:
            final_reason = _RECONCILE_FAIL_FINAL_REASON
            blocklist_reason = BlocklistReason.failed.value
        else:
            final_reason = row.failed_reason or "recovered stranded failed_pending row"
            blocklist_reason = BlocklistReason.failed.value
        completions.append(
            _FailureCompletion(
                download_id=row.id,
                event=DownloadFailed(
                    torrent_hash=row.torrent_hash,
                    source_title=row.torrent_hash,
                    reason=final_reason,
                    tmdb_id=row.tmdb_id,
                    occurred_at=now,
                ),
                blocklist=marker.flags.blocklist if marker is not None else True,
                remove_torrent=(marker.flags.remove_torrent if marker is not None else True),
                blocklist_reason=blocklist_reason,
                # The exact marker (or free text / None) this provenance came from:
                # a NEWER nonce-marker restamped after this snapshot defeats this
                # completion's terminal CAS atomically.
                observed_failed_reason=row.failed_reason,
            )
        )

    # Live-claim filter (pre-stamp invisibility fast-path): a row an operator mark_failed
    # currently has in flight is THAT call's to complete -- skipping it here keeps
    # this cycle's Phase B from removing a torrent the operator said to keep, and
    # its Phase C from stealing the failed_pending -> Failed CAS with the wrong
    # side effects. This pre-filter is only the FIRST check; Phases B and C
    # re-check per hash / per completion at the moment of each await, since a claim
    # can be registered at any point mid-cycle. If the operator call later fails,
    # its claim is released and the marker-carrying residual heals (with the
    # operator's flags) next cycle.
    deferred = [c for c in completions if _is_operator_claimed(c.download_id)]
    completions = [c for c in completions if not _is_operator_claimed(c.download_id)]
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
    # operator residual whose marker says remove=no is honored: no removal. The
    # claim registry is RE-CHECKED per completion IMMEDIATELY before its removal
    # await (pre-stamp invisibility fast-path, Phase B): a claim registered while an
    # EARLIER hash's removal was in flight must protect every later hash -- the
    # pre-loop filter alone is a stale snapshot by the second iteration. A claimed
    # completion is dropped from the WHOLE cycle, not just its removal: it was
    # built BEFORE the operator stamped its marker, so even if the claim is
    # released seconds later (the operator call failing fast), this cycle's Phase C
    # must never complete the row with those stale pre-marker semantics -- the
    # residual instead heals NEXT cycle from the marker, the designed path.
    # Removal-physics guard lifetime (reconcile side): a removed row's id enters
    # ``_reconcile_removals_in_flight`` just before its delete await and is HELD
    # until the CONSEQUENCE of that removal settles -- this cycle's Phase C either
    # commits the row's terminal completion, drops it, or exhausts (the
    # cycle-scope ``finally`` below). Releasing at the delete's return would leave
    # a returned-but-unsettled gap: an operator mark_failed(remove_torrent=False)
    # registering there is refused nothing, stamps its marker, drops the stale
    # completion via the CAS predicate, and later completes promising remove=no
    # semantics for data this cycle already destroyed -- a physics lie. Only ids
    # THIS cycle added are released (``settling``), never another actor's.
    settling: set[int] = set()
    try:
        unclaimed: list[_FailureCompletion] = []
        for completion in completions:
            if _is_operator_claimed(completion.download_id):
                _logger.info(
                    "dropping download %s from this reconcile cycle: an operator "
                    "mark-failed claimed it mid-cycle; its residual heals next cycle",
                    safe_int(completion.download_id),
                )
                continue
            if not completion.remove_torrent:
                unclaimed.append(completion)
                continue
            # GUARD FIRST (round 10): registering the removal-physics guard is a
            # SYNCHRONOUS set-add -- no await separates the claim check above from
            # this line, and from this line no operator claim can be created for
            # the row (_register_operator_claim refuses). So there is NO yield
            # point between "operators are barred" and the durable verification
            # below: anything an operator did finished BEFORE the bar and is
            # visible to the re-proof SELECT; anything after the bar is refused.
            _reconcile_removals_in_flight.add(completion.download_id)
            settling.add(completion.download_id)
            # DURABLE re-proof immediately before the irreversible delete: the
            # claim check above sees only LIVE claims -- an operator who stamped,
            # completed, and released since this cycle's snapshot left a durable
            # record (a marker or a terminal status) instead of a claim, and its
            # remove=no may have already WON. One fresh SELECT (the rollbacks keep
            # this session's read snapshot fresh and ensure no transaction is
            # held across the client I/O below, per this module's discipline):
            # proceed only while the row is still failed_pending with the exact
            # reason this completion observed; otherwise drop it -- no deletion,
            # no Phase C -- and say why.
            await session.rollback()
            latest = await session.get(Download, completion.download_id, populate_existing=True)
            latest_status = latest.status if latest is not None else None
            latest_reason = latest.failed_reason if latest is not None else None
            await session.rollback()
            if (
                latest_status != DownloadState.FailedPending.value
                or latest_reason != completion.observed_failed_reason
            ):
                _logger.info(
                    "dropping download %s from this reconcile cycle before its "
                    "removal: the row changed durably underneath it (status=%s) -- "
                    "an operator marker or terminal completion now governs",
                    safe_int(completion.download_id),
                    latest_status if latest_status is not None else "gone",
                )
                # No delete ran for this row, so there is no consequence to
                # settle: release the bar NOW (not at cycle scope) so operator
                # commands for a row this cycle no longer touches are not
                # refused. Rows whose delete DOES run keep the round-8 lifetime.
                _reconcile_removals_in_flight.discard(completion.download_id)
                settling.discard(completion.download_id)
                continue
            # Removal-physics rule, reconcile side: the guard registered above
            # bars an operator mark_failed(remove_torrent=False) (409
            # removal_in_progress) from completing with remove=no semantics
            # while (or right after) this await destroys the data. Held until
            # the cycle-scope finally -- see the lifetime note above.
            removed_ok = await purge_service.remove_torrent(
                qbt,
                completion.event.torrent_hash,
                context="a reconcile-driven download failure",
                extra={
                    "torrent_hash": completion.event.torrent_hash,
                    "tmdb_id": completion.event.tmdb_id,
                },
            )
            # Persist the removal OUTCOME (see the two done-marker renderers): a
            # row whose delete just succeeded is CAS-restamped to remove=done and
            # committed AT ONCE -- durable even if this cycle's Phase C later
            # exhausts, so no healer ever skips the residual waiting for a
            # removal that already happened. An operator remove=yes marker keeps
            # its flags and nonce; an UNMARKED (plain reconcile) row gets the
            # reconcile-provenance done record. The completion then observes the
            # done-marker so its own Phase-C predicate still matches. A failed
            # delete restamps nothing: the removal is still owed. The commit is
            # guarded -- a transient failure loses only the outcome record (the
            # next cycle's no-op re-removal restamps it), never the cycle.
            provenance = _parse_operator_fail_marker(completion.observed_failed_reason)
            done_marker: str | None = None
            if removed_ok and provenance is not None and provenance.flags.remove_torrent:
                done_marker = _operator_fail_marker_removal_done(
                    provenance.flags.blocklist, provenance.nonce
                )
            elif removed_ok and provenance is None:
                done_marker = _reconcile_removal_done_marker()
            if done_marker is not None:
                restamped = await download_repo.update_status_if_in(
                    completion.download_id,
                    DownloadState.FailedPending.value,
                    frozenset({DownloadState.FailedPending.value}),
                    failed_reason=done_marker,
                    require_failed_reason=completion.observed_failed_reason,
                )
                if restamped:
                    try:
                        await session.commit()
                    except SQLAlchemyError:
                        await session.rollback()
                        _logger.warning(
                            "could not persist the removal outcome for download %s; "
                            "the marker still says the removal is owed -- a later "
                            "cycle re-removes (a no-op) and restamps",
                            safe_int(completion.download_id),
                        )
                    else:
                        completion = replace(completion, observed_failed_reason=done_marker)
            unclaimed.append(completion)
        completions = unclaimed

        # Phase C: complete each failure (failed_pending -> Failed + blocklist +
        # re-arm) in ONE bounded-retry transaction. On exhaustion the rows stay at
        # the reconcilable ``failed_pending`` for a later cycle's strand
        # re-derivation to heal (the finally below releases their removal guard,
        # so an operator command CAN claim them between cycles -- the residual is
        # settled-but-unhealed at that point, not mid-consequence). The claim
        # registry is RE-CHECKED per completion, on EVERY retry attempt (pre-stamp
        # invisibility fast-path, Phase C): an operator mark_failed that claimed a
        # row after the Phase-B checks above must not have its completion stolen
        # with the wrong semantics -- skip it; the operator call (or, if it fails,
        # the next cycle's heal) completes it.
        async def _complete_reconcile_failures() -> None:
            rearms: list[_FailedReArm] = []
            for completion in completions:
                if _is_operator_claimed(completion.download_id):
                    continue
                rearm = await _handle_failed(session, completion, rows)
                if rearm is not None:
                    rearms.append(rearm)
            for rearm in rearms:
                await _rearm_failed_request(session, rearm)

        if completions:
            await _commit_phase_c_with_retry(
                session,
                _complete_reconcile_failures,
                context="reconcile-driven failures",
                identity=[completion.event.torrent_hash for completion in completions],
            )
    finally:
        # Every removal this cycle performed has now SETTLED: its completion
        # committed, was dropped/deferred in Phase C, or Phase C exhausted
        # (leaving a plain reconcilable residual). Release the physics guard for
        # exactly the ids this cycle added.
        _reconcile_removals_in_flight.difference_update(settling)

    # ``populate_existing`` refreshes the returned rows from the DB (issue #77): see
    # the same note in the no-failures early return above.
    return await download_repo.list_active(populate_existing=True)


async def heal_failed_pending_without_client(session: AsyncSession) -> None:
    """DB-only Phase C for operator residuals needing no client I/O
    (``remove=no``, or ``remove=done`` — the removal already happened).

    ``web.app._reconcile_once`` skips the client reconcile entirely when
    qBittorrent is unconfigured, so a ``remove=no`` operator residual — which by
    the operator's OWN choice needs no client I/O — would otherwise have no
    automatic path to Failed/re-arm until qBittorrent is configured (it may never
    be: ``mark_failed(remove_torrent=False)`` works on exactly such installs).
    This narrow heal completes ONLY those rows: no client construction, no removal
    attempts. Both marker actors qualify — an operator ``remove=no``/``done``
    residual heals with the operator vocabulary, a RECONCILE ``remove=done``
    record (a plain failure whose delete already ran before its Phase C
    exhausted) with the reconcile defaults. Every other ``failed_pending`` row
    (an unmarked strand or a ``remove=yes`` marker) genuinely needs the client's
    removal first, so it is left for the full reconcile cycle once qBittorrent
    returns — counted and logged, never silently dropped (honesty over silence).
    Claimed rows are skipped exactly as in :func:`reconcile_and_list` (pre-stamp
    invisibility).
    """
    download_repo = SqlDownloadRepository(session)
    rows = await download_repo.list_active()
    now = _utcnow()
    completions: list[_FailureCompletion] = []
    awaiting_client = 0
    for row in rows:
        if row.status != DownloadState.FailedPending.value:
            continue
        marker = _parse_operator_fail_marker(row.failed_reason)
        if marker is None or marker.flags.remove_torrent:
            awaiting_client += 1
            continue
        if _is_operator_claimed(row.id):
            continue
        completions.append(
            _FailureCompletion(
                download_id=row.id,
                event=DownloadFailed(
                    torrent_hash=row.torrent_hash,
                    source_title=row.torrent_hash,
                    reason=(
                        _OPERATOR_FAIL_FINAL_REASON
                        if marker.operator
                        else _RECONCILE_FAIL_FINAL_REASON
                    ),
                    tmdb_id=row.tmdb_id,
                    occurred_at=now,
                ),
                blocklist=marker.flags.blocklist,
                remove_torrent=False,
                blocklist_reason=(
                    BlocklistReason.user_reported.value
                    if marker.operator
                    else BlocklistReason.failed.value
                ),
                # The exact nonce-marker this heal is finishing: a fresher restamp
                # defeats the terminal CAS atomically (see _FailureCompletion).
                observed_failed_reason=row.failed_reason,
            )
        )
    if awaiting_client:
        _logger.info(
            "%d failed_pending row(s) need a torrent removal and wait for qBittorrent "
            "to be configured before they can heal",
            awaiting_client,
        )
    if not completions:
        return

    async def _complete_db_only() -> None:
        rearms: list[_FailedReArm] = []
        for completion in completions:
            if _is_operator_claimed(completion.download_id):
                continue
            rearm = await _handle_failed(session, completion, rows)
            if rearm is not None:
                rearms.append(rearm)
        for rearm in rearms:
            await _rearm_failed_request(session, rearm)

    await _commit_phase_c_with_retry(
        session,
        _complete_db_only,
        context="db-only strand heal (qBittorrent unconfigured)",
        identity=[completion.event.torrent_hash for completion in completions],
    )


async def mark_failed(
    session: AsyncSession,
    qbt: DownloadClientPort | None,
    *,
    download_id: int,
    blocklist: bool,
    remove_torrent: bool = True,
    allow_claim_supersede: bool = True,
    allow_adopt_existing_marker: bool = True,
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

    ``allow_claim_supersede`` (default ``True``, every OPERATOR call -- unchanged
    behaviour): whether THIS call's claim registration may replace an existing
    live claim (module docstring, "Operator provenance"). The stalled-download
    self-heal (issue #165 hardening finding) is the one caller that passes
    ``False``: it must never become the newer owner over an existing claim,
    because ``reconcile_and_list``'s own pre-check (``_is_operator_claimed``) can
    race an operator's manual call that is suspended between ITS OWN
    ``session.get`` and ITS OWN claim registration -- the pre-check and this
    call's registration are two different moments, not one atomic step. Passing
    ``False`` re-verifies atomically at registration time instead of trusting the
    caller's earlier snapshot: see :func:`_register_operator_claim` and
    :class:`OperatorClaimActiveError`.

    ``allow_adopt_existing_marker`` (default ``True``, every OPERATOR call --
    unchanged behaviour): whether this call may ADOPT a row it finds already at
    ``failed_pending`` (restamp it with its own flags and drive it to
    completion). An operator is always entitled to adopt an abandoned residual --
    the newest human instruction wins. The stalled-download self-heal passes
    ``False`` (Codex review, comment 3541100611): a claim is released the moment
    its owning call returns, but the ``failed_pending`` row + marker it stamped
    can durably outlive that release (e.g. a Phase-C exhaustion), so
    ``allow_claim_supersede=False`` alone does not stop self-heal from walking
    straight into the adopt branch of an UNCLAIMED, already-marked residual and
    restamping someone else's provenance with the reconcile-default
    ``blocklist=True, remove_torrent=True``. When ``False`` and this call would
    otherwise adopt (whether the row was already ``failed_pending`` when read,
    or reached it via a concurrent transition discovered mid-CAS), it raises
    :class:`FailedPendingAdoptionRefusedError` instead of adopting -- the row,
    its marker, and its live claim (there is none) are left untouched.

    Mirrors :func:`reconcile_and_list`'s three-phase ordering (issue #68 + the
    Phase-C-strand hardening): Phase A routes the row to the NON-terminal
    ``failed_pending`` and commits; Phase B removes the torrent; Phase C completes
    ``Failed`` + blocklist + re-arm in one bounded-retry transaction. The terminal
    ``Failed`` advance is DEFERRED to Phase C so an exhausted Phase C leaves a
    reconcilable ``failed_pending`` row (which the reconcile loop re-derives and
    heals) rather than an un-healable terminal ``Failed`` + ``downloading`` request.

    The operator's explicit ``blocklist`` / ``remove_torrent`` choices are carried
    as provenance so neither a concurrent reconcile tick nor the later heal can
    override them (module docstring, "Operator provenance"): a single-owner claim
    TOKEN is registered BEFORE any Phase-A write (reconcile treats the row as
    invisible at every one of its phase boundaries while this call is in flight),
    and the SAME token becomes the nonce inside the ``failed_reason`` marker Phase
    A stamps — durable ownership. Every mutation of the row is then
    PREDICATE-ATOMIC: the Phase-A stamp/restamp CASes on the exact reason value
    this call observed, and the Phase-C terminal CAS advances the row only while
    ``failed_reason`` still equals this call's own nonce-marker — so a newer
    command's restamp defeats a stale call at the database itself; the stale call
    yields (the newer owner completes with ITS flags). Phase B (removal I/O — not
    expressible as a DB predicate) stays arbitrated by the registry, including the
    removal-in-flight physics rule. The ``finally`` release is token-gated so a
    superseded finisher never clears the newer command's claim, and a residual
    that outlives the claim (Phase-C exhaustion, crash) heals with the owning
    flags read back from the marker.
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
    record_snapshot = await download_repo.get_by_hash(torrent_hash)
    request_media_type = (
        row.media_type.value
        if row.media_type is not None
        else ("tv" if row.season is not None else "movie")
    )
    rearms: list[_FailedReArm] = []
    if record_snapshot is not None and record_snapshot.scopes:
        media_type = _media_type_for_blocklist(record_snapshot, request_media_type)
        rearms = [
            _FailedReArm(
                media_request_id=scope.media_request_id,
                season=scope.season,
                media_type=media_type,
            )
            for scope in record_snapshot.scopes
            if scope.media_request_id is not None and scope.season is not None
        ]
    elif request_id is not None:
        rearms = [
            _FailedReArm(
                media_request_id=request_id,
                season=row.season,
                media_type=request_media_type,
            )
        ]

    # Ownership protocol (module docstring): register the single-owner claim BEFORE
    # any Phase-A write -- from this point reconcile treats the row as invisible at
    # all of its phase boundaries -- and hold the returned token. The token doubles
    # as the marker NONCE, making ownership DURABLE: every later mutation re-proves
    # ownership by including the exact marker in its WHERE, so no check-then-act
    # window separates the ownership decision from the write.
    flags = _OperatorFailFlags(blocklist=blocklist, remove_torrent=remove_torrent)
    token = _register_operator_claim(download_id, flags, allow_supersede=allow_claim_supersede)
    marker = _operator_fail_marker(flags, token)
    superseded = False
    try:
        # Route to the pre-terminal ``failed_pending`` pause. The legal graph reaches
        # ``Failed`` only from ``failed_pending``, so an actively Downloading torrent
        # (etc.) must pass through it first. A row ALREADY at ``failed_pending`` (a
        # reconcile detection or a stranded prior attempt) is re-stamped with THIS
        # call's nonce-marker instead: the operator's flags now own the residual
        # (they are the most recent explicit instruction).
        adopt = current is DownloadState.FailedPending
        observed_reason = row.failed_reason
        if not adopt:
            if not is_legal_transition(current, DownloadState.FailedPending):
                raise InvalidStateTransitionError(current.value, DownloadState.Failed.value)
            pending = await download_repo.update_status_if_in(
                download_id,
                DownloadState.FailedPending.value,
                frozenset({current.value}),
                failed_reason=marker,
            )
            if not pending:
                # The CAS lost against a STALE snapshot. Re-read once: an in-flight
                # reconcile cycle may have moved the row to ``failed_pending``
                # (detected, not yet completed) during this call's snapshot -- that
                # state is ADOPTABLE, exactly like arriving to find it there (the
                # adopt branch below), so raising a 409 from the stale ``current``
                # would refuse a command the operator is fully entitled to. Adopt
                # it; raise only when the re-read shows a genuinely non-adoptable
                # state (importing, terminal, ...).
                await session.rollback()
                latest = await session.get(Download, download_id, populate_existing=True)
                if latest is None or latest.status != DownloadState.FailedPending.value:
                    actual = latest.status if latest is not None else current.value
                    raise InvalidStateTransitionError(actual, DownloadState.Failed.value)
                adopt = True
                observed_reason = latest.failed_reason
        if adopt and not allow_adopt_existing_marker:
            # Codex review (comment 3541100611): this caller opted OUT of adoption
            # (self-heal only). The row is ALREADY failed_pending here -- whether it
            # was found that way at the top-of-function read, or reached via the CAS
            # re-read just above -- so restamping it would silently override
            # whatever provenance is already durably recorded (an operator's
            # explicit blocklist/remove_torrent choice, or a reconcile failure
            # awaiting completion), even though no LIVE claim exists to have
            # blocked this call via ``allow_claim_supersede``. Refuse outright;
            # nothing has been written yet in this branch, so there is nothing to
            # roll back beyond the ``finally``'s claim release.
            raise FailedPendingAdoptionRefusedError(download_id)
        if adopt:
            # Predicate-atomic restamp (finding: an older call must not clobber a
            # newer call's marker): stamp MY nonce-marker over EXACTLY the reason
            # value this call observed. A miss means another writer got between the
            # observation and this statement -- the DB, not in-memory state,
            # decided. Then: (a) no longer the registry owner -> a NEWER command
            # superseded this one; YIELD (it completes with its flags). (b) still
            # the owner -> only an OLDER concurrent stamp (or a status move) can
            # have intervened; re-observe and retry -- an older call's own
            # predicate can never match again after MY stamp lands, so each
            # competitor defeats this loop at most once and it converges. A row
            # that left ``failed_pending`` raises honestly.
            while True:
                stamped = await download_repo.update_status_if_in(
                    download_id,
                    DownloadState.FailedPending.value,
                    frozenset({DownloadState.FailedPending.value}),
                    failed_reason=marker,
                    require_failed_reason=observed_reason,
                )
                if stamped:
                    break
                if not _owns_operator_claim(download_id, token):
                    superseded = True
                    await session.rollback()
                    break
                await session.rollback()
                latest = await session.get(Download, download_id, populate_existing=True)
                if latest is None or latest.status != DownloadState.FailedPending.value:
                    actual = latest.status if latest is not None else current.value
                    raise InvalidStateTransitionError(actual, DownloadState.Failed.value)
                observed_reason = latest.failed_reason

        if not superseded:
            # Phase A commit: the row is at ``failed_pending`` (reconcilable),
            # carrying THIS call's nonce-marker. The blocklist, the terminal
            # ``Failed`` advance, and the re-arm are NOT yet written.
            await session.commit()

            # Phase B: close the seeding leak (ADR-0014) and, per issue #68, remove
            # the old torrent BEFORE re-arming. Best-effort +
            # already-gone-is-a-no-op (see ``purge_service.remove_torrent``): a
            # client hiccup never undoes the committed Phase A, and -- because
            # removal is logged-not-raised -- never blocks Phase C. ``qbt is not
            # None`` is guaranteed by the top-of-function guard whenever
            # ``remove_torrent`` is True; the explicit check narrows the optional
            # type. The registry fast-path check covers the not-yet-restamped
            # window of a newer command (removal is I/O -- it cannot be
            # predicate-gated at the DB, so the registry arbitrates it).
            # Immediately before the delete await the claim is flagged
            # removal-in-flight (removal-physics rule): from that point the remove
            # decision is PHYSICALLY irreversible, so supersession is refused for
            # the remainder of this call -- the one place later-wins yields to
            # physics.
            if remove_torrent and qbt is not None and _owns_operator_claim(download_id, token):
                _mark_removal_in_flight(download_id, token)
                removed_ok = await purge_service.remove_torrent(
                    qbt,
                    torrent_hash,
                    context="an operator mark-failed",
                    extra={"torrent_hash": torrent_hash, "download_id": safe_int(download_id)},
                )
                # Persist the removal OUTCOME (see _operator_fail_marker_removal_
                # done): the delete succeeded, so the durable marker must say so --
                # CAS-restamped on this call's exact marker (same nonce, ownership
                # unchanged) and committed AT ONCE, so a later Phase-C exhaustion
                # leaves a remove=done residual every healer can complete without
                # a client. The terminal CAS below then predicates on the DONE
                # marker. A failed delete keeps remove=yes: the removal is owed.
                if removed_ok:
                    done_marker = _operator_fail_marker_removal_done(flags.blocklist, token)
                    restamped = await download_repo.update_status_if_in(
                        download_id,
                        DownloadState.FailedPending.value,
                        frozenset({DownloadState.FailedPending.value}),
                        failed_reason=done_marker,
                        require_failed_reason=marker,
                    )
                    if restamped:
                        # Guarded: a transient commit failure loses only the
                        # outcome record (the marker keeps saying remove=yes; a
                        # later heal re-removes as a no-op and restamps), never
                        # this call -- and the terminal predicate then still
                        # targets the marker the DB actually holds.
                        try:
                            await session.commit()
                        except SQLAlchemyError:
                            await session.rollback()
                            _logger.warning(
                                "could not persist the removal outcome for download "
                                "%s; the marker still says the removal is owed",
                                safe_int(download_id),
                            )
                        else:
                            marker = done_marker

            # Phase C: complete ``failed_pending`` -> ``Failed`` + optional
            # blocklist + re-arm, in one bounded-retry transaction. The terminal
            # CAS is PREDICATE-ATOMIC on this call's own nonce-marker: it advances
            # the row ONLY while ``failed_reason`` still equals the exact marker
            # this call stamped, so a newer command's restamp defeats it at the
            # database itself -- the ownership re-proof and the terminal write are
            # one statement, with no post-CAS token re-check needed. The winning
            # CAS replaces the marker with the final human-readable reason. A miss
            # means a concurrent completer finished the row or a newer command
            # restamped it: yield, logged. On retry exhaustion the row stays at
            # the reconcilable ``failed_pending`` WITH the marker, so the
            # reconcile loop heals it under these same flags.
            async def _complete_mark_failed() -> None:
                won = await download_repo.update_status_if_in(
                    download_id,
                    DownloadState.Failed.value,
                    frozenset({DownloadState.FailedPending.value}),
                    failed_reason=_OPERATOR_FAIL_FINAL_REASON,
                    require_failed_reason=marker,
                )
                if not won:
                    _logger.info(
                        "yielding mark-failed completion of download %s: the row was "
                        "completed by a concurrent writer or restamped by a newer "
                        "operator command (which completes it with its own flags)",
                        safe_int(download_id),
                    )
                    return
                await _mark_download_scopes_terminal(
                    session, download_id, RequestStatus.failed.value
                )
                if blocklist:
                    source_title = (
                        await blocklist_service.source_title_for(session, torrent_hash)
                        or torrent_hash
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
                        # Scope by media namespace (see _handle_failed). Prefer the
                        # owning request; fall back to metadata or season scope.
                        media_type=_media_type_for_blocklist(
                            await download_repo.get_by_hash(torrent_hash),
                            request.media_type if request is not None else None,
                        ),
                    )
                for rearm in rearms:
                    await _rearm_failed_request(session, rearm)

            await _commit_phase_c_with_retry(
                session,
                _complete_mark_failed,
                context="operator mark-failed",
                identity=safe_int(download_id),
            )
    finally:
        # Release the live claim on EVERY exit -- success, a 409'd lost race, or a
        # Phase-C exhaustion -- but ONLY while this call's token still owns it
        # (token-gated release): a superseded call's finally must never clear
        # the NEWER command's live claim out from under it. After an exhaustion the
        # persisted marker (not the claim) carries the flags to the reconcile heal.
        _release_operator_claim(download_id, token)

    # ``populate_existing`` (issue #77's pattern): on the YIELD path a superseding
    # mark_failed completed this row in a DIFFERENT session, and this session's
    # identity map would otherwise report the stale pre-completion status.
    failed = await download_repo.get_by_hash(torrent_hash, populate_existing=True)
    if failed is None:  # pragma: no cover - just updated this row
        raise LookupError(f"download {download_id} vanished mid-update")
    return failed
