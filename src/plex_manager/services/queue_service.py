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
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

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
from plex_manager.services import blocklist_service, purge_service, season_request_service

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from plex_manager.domain.events import DownloadFailed
    from plex_manager.ports.download_client import DownloadClientPort
    from plex_manager.ports.repositories import DownloadRecord

__all__ = ["InvalidStateTransitionError", "list_queue", "mark_failed", "reconcile_and_list"]

_logger = logging.getLogger(__name__)

_TERMINAL_STATUS_VALUES = frozenset(s.value for s in TERMINAL_STATES)


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
    mirror that with a three-phase flow (fail+blocklist commit -> remove_torrent ->
    re-arm commit), so the re-arm target must survive the FIRST commit as a plain
    value rather than being re-derived from the (now terminal, possibly identity-
    map-stale) ``Download`` row on the far side of the boundary.

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
    TV routes through ``season_request_service`` (the SEASON re-arms and the
    parent's computed rollup recomputes in the same transaction) with the
    ``skip_if_terminal`` guard: a season a PRIOR download already finished must
    never be dragged back to ``searching`` by THIS (later, unrelated) download's
    failure. A movie (no season) sets the request status directly.
    """
    if rearm.season is not None:
        await season_request_service.set_status(
            session,
            media_request_id=rearm.media_request_id,
            season_number=rearm.season,
            status=RequestStatus.searching.value,
            skip_if_terminal=True,
        )
    else:
        await SqlRequestRepository(session).set_status(
            rearm.media_request_id, RequestStatus.searching.value
        )


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
    """Phase A of a reconcile-driven failure: blocklist the release and complete
    ``FailedPending`` -> ``Failed`` (DB writes only). Returns the owning
    request/season to re-arm, or ``None`` if the row has no owning request.

    Deliberately does NOT re-arm the request here (issue #68). Re-arming to
    ``searching`` makes the request due for auto-grab again; if that re-grab
    re-resolves to the SAME info_hash before the old torrent's removal completes,
    the (stale) removal would delete the freshly-grabbed torrent + its data. So the
    re-arm is handed back to the caller to apply in a LATER transaction, AFTER
    :func:`purge_service.remove_torrent` has run (Radarr's ``EventHandleOrder.Last``
    ordering). :func:`reconcile_and_list` threads the returned descriptors across
    that commit boundary.

    Does NOT remove the torrent either: that ``qbt.remove(delete_files=True)`` is
    external client I/O and must not run inside the reconcile write transaction --
    holding SQLite's write lock across a network round-trip, and (worse) a later
    write failure in the same transaction would roll the DB back AFTER the torrent
    was already deleted. :func:`reconcile_and_list` removes the failed hashes
    best-effort AFTER the Phase A commit (mirroring :func:`mark_failed`); an
    already-gone hash is a no-op success there.
    """
    blocklist_repo = SqlBlocklistRepository(session)
    request_repo = SqlRequestRepository(session)

    record = next(
        (r for r in rows if r.torrent_hash.lower() == event.torrent_hash.lower()),
        None,
    )
    request = (
        await request_repo.get(record.media_request_id)
        if record is not None and record.media_request_id is not None
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
    # Complete the FailedPending -> Failed transition. The reconciler only moves
    # the row as far as ``failed_pending``; without this advance the row would be
    # stranded there forever — it is neither terminal (so it lingers in
    # ``list_active`` and the queue shows a zombie torrent) nor active (so the
    # reconciler never revisits it). The blocklist has now fired and the re-arm is
    # deferred to Phase C, so the row is genuinely Failed.
    if record is not None:
        await SqlDownloadRepository(session).update_status(
            record.id, DownloadState.Failed.value, failed_reason=event.reason
        )

    # Hand the owning request/season back for Phase C re-arm (after removal). The
    # descriptor is captured here — while ``record``/``request`` are in hand —
    # rather than re-read after the commit boundary, where the now-terminal row
    # would be excluded from ``list_active`` and the identity map may be stale.
    if record is not None and record.media_request_id is not None:
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

    # Issue #68 — three-phase failure handling so a torrent removal can NEVER
    # delete a fresh same-hash re-grab (Radarr's EventHandleOrder.Last: removal
    # precedes re-search eligibility):
    #
    #   Phase A: blocklist + FailedPending->Failed, committed, WITHOUT re-arming.
    #   Phase B: best-effort remove_torrent per failed hash (old torrent gone).
    #   Phase C: NOW re-arm the request/season to 'searching' and commit.
    #
    # Feed the CAS-APPLIED transitions only: a transition that lost its
    # compare-and-set (a concurrent writer moved the row) must not spawn a
    # blocklist/re-arm for a state change that never persisted.
    failed_events = list(failed_download_events(applied_transitions, rows, occurred_at=now))
    rearms: list[_FailedReArm] = []
    for event in failed_events:
        rearm = await _handle_failed(session, event, rows)
        if rearm is not None:
            rearms.append(rearm)

    # Phase A commit. qbt.remove is external client I/O and must not run while this
    # write transaction holds SQLite's write lock (nor before a later write that
    # could roll the DB back after the torrent was already deleted), so it is
    # deferred to Phase B below.
    await session.commit()

    # Phase B: close the seeding leak (ADR-0014) AND, per issue #68, ensure the old
    # torrent is gone BEFORE the request is re-armed. A client hiccup here never
    # undoes the committed blocklist/fail, and an already-gone hash (the common
    # case -- the row usually failed BECAUSE it went ClientMissing) is a no-op
    # success. Best-effort per hash, preserving the per-hash log extra. Because it
    # is best-effort (logged, never raised), a removal failure does NOT block the
    # Phase C re-arm: the row still lands Failed (visible) with its request re-armed
    # to 'searching' (retryable) -- honesty over silence.
    for event in failed_events:
        await purge_service.remove_torrent(
            qbt,
            event.torrent_hash,
            context="a reconcile-driven download failure",
            extra={"torrent_hash": event.torrent_hash, "tmdb_id": event.tmdb_id},
        )

    # Phase C: re-arm the owning requests/seasons now that the old torrents are
    # gone, then commit. Threaded from Phase A as plain values so no post-commit,
    # possibly-stale row read is needed.
    for rearm in rearms:
        await _rearm_failed_request(session, rearm)
    if rearms:
        await session.commit()

    # ``populate_existing`` refreshes the returned rows from the DB (issue #77):
    # with ``expire_on_commit=False`` a row that LOST a status CAS earlier in this
    # cycle keeps its stale in-memory status, and the identity map would otherwise
    # win over this SELECT, reporting a status the DB no longer holds.
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

    async def _raise_current_transition() -> None:
        await session.rollback()
        latest = await session.get(Download, download_id, populate_existing=True)
        actual = latest.status if latest is not None else current.value
        raise InvalidStateTransitionError(actual, DownloadState.Failed.value)

    # Route through FailedPending when the legal graph requires it (e.g. an
    # actively Downloading torrent cannot jump straight to Failed).
    if not is_legal_transition(current, DownloadState.Failed):
        if not is_legal_transition(current, DownloadState.FailedPending):
            raise InvalidStateTransitionError(current.value, DownloadState.Failed.value)
        pending = await download_repo.update_status_if_in(
            download_id,
            DownloadState.FailedPending.value,
            frozenset({current.value}),
        )
        if not pending:
            await _raise_current_transition()

    failed = await download_repo.update_status_if_in(
        download_id,
        DownloadState.Failed.value,
        frozenset({DownloadState.FailedPending.value}),
        failed_reason="marked failed by operator",
    )
    if not failed:
        await _raise_current_transition()

    if blocklist:
        source_title = (
            await blocklist_service.source_title_for(session, row.torrent_hash) or row.torrent_hash
        )
        indexer = await blocklist_service.indexer_for(session, row.torrent_hash)
        request = (
            await SqlRequestRepository(session).get(row.media_request_id)
            if row.media_request_id is not None
            else None
        )
        await SqlBlocklistRepository(session).create(
            source_title=source_title,
            reason=BlocklistReason.user_reported.value,
            tmdb_id=request.tmdb_id if request is not None else row.tmdb_id,
            torrent_hash=row.torrent_hash,
            indexer=indexer,
            # Scope by media namespace (see _handle_failed). Prefer the owning
            # request; fall back to the persisted download metadata or season scope.
            media_type=_media_type_for_blocklist(
                await download_repo.get_by_hash(row.torrent_hash),
                request.media_type if request is not None else None,
            ),
        )

    # Capture the owning request/season to re-arm, BEFORE the Phase A commit. The
    # re-arm is unconditional — the blocklist flag governs ONLY whether a Blocklist
    # row is written, NOT whether the request status is corrected. Without it, a
    # mark-failed(blocklist=false) drives the download to terminal Failed (gone from
    # the active queue) while the request stays 'downloading' forever: a dishonest
    # status asserting an active download that no longer exists, with nothing to
    # re-search or re-fail it. But per issue #68 the re-arm is DEFERRED to Phase C
    # (after the torrent removal) so a same-hash re-grab cannot be deleted by the
    # stale removal -- mirroring the reconcile-driven ``_handle_failed`` ordering.
    rearm: _FailedReArm | None = None
    if row.media_request_id is not None:
        rearm = _FailedReArm(
            media_request_id=row.media_request_id,
            season=row.season,
            media_type=(
                row.media_type.value
                if row.media_type is not None
                else ("tv" if row.season is not None else "movie")
            ),
        )

    # Phase A commit: the fail + optional blocklist are persisted; the request is
    # NOT yet re-armed.
    await session.commit()

    # Phase B: close the seeding leak (ADR-0014) and, per issue #68, remove the old
    # torrent BEFORE re-arming. Best-effort + already-gone-is-a-no-op (see
    # ``purge_service.remove_torrent``): a client hiccup never undoes the committed
    # fail/blocklist, and — because removal is logged-not-raised — never blocks the
    # Phase C re-arm below, so the row still lands in a visible, retryable state.
    # ``qbt is not None`` is guaranteed by the top-of-function guard whenever
    # ``remove_torrent`` is True; the explicit check narrows the optional type.
    if remove_torrent and qbt is not None:
        await purge_service.remove_torrent(
            qbt,
            row.torrent_hash,
            context="an operator mark-failed",
            extra={"torrent_hash": row.torrent_hash, "download_id": safe_int(download_id)},
        )

    # Phase C: re-arm now that the old torrent is gone, then commit.
    if rearm is not None:
        await _rearm_failed_request(session, rearm)
        await session.commit()

    failed = await download_repo.get_by_hash(row.torrent_hash)
    if failed is None:  # pragma: no cover - just updated this row
        raise LookupError(f"download {download_id} vanished mid-update")
    return failed
