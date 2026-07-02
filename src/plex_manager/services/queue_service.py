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
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from plex_manager.domain.reconciler import (
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


async def _handle_failed(
    session: AsyncSession,
    qbt: DownloadClientPort,
    event: DownloadFailed,
    rows: list[DownloadRecord],
) -> None:
    """Blocklist a failed release, re-arm its request, and remove the torrent.

    The torrent removal (``qbt.remove(delete_files=True)``, best-effort) closes the
    seeding leak (ADR-0014): before this, a reconcile-driven blocklist-and-research
    left the bad torrent seeding and holding disk indefinitely. Removing an
    already-gone hash (the common case here -- the row usually failed BECAUSE it
    went ClientMissing) is a no-op success.
    """
    blocklist_repo = SqlBlocklistRepository(session)
    request_repo = SqlRequestRepository(session)

    record = next(
        (r for r in rows if r.torrent_hash.lower() == event.torrent_hash.lower()),
        None,
    )
    source_title = (
        await blocklist_service.source_title_for(session, event.torrent_hash) or event.source_title
    )
    indexer = await blocklist_service.indexer_for(session, event.torrent_hash)
    await blocklist_repo.create(
        source_title=source_title,
        reason=BlocklistReason.failed.value,
        tmdb_id=event.tmdb_id,
        torrent_hash=event.torrent_hash,
        indexer=indexer,
        # Scope by media namespace so this entry can't reject a movie/show that
        # happens to share the tmdb id. A TV download is always season-scoped, so a
        # non-NULL season identifies it as tv (else movie).
        media_type="tv" if record is not None and record.season is not None else "movie",
    )
    if record is not None and record.media_request_id is not None:
        if record.season is not None:
            # TV: route through season_request_service so the SEASON re-arms to
            # 'searching' and the parent's computed rollup is recomputed in the
            # same transaction, rather than stomping the request status directly.
            # ``skip_if_terminal``: a season a PRIOR download already finished
            # (completed/available/failed) must never be dragged back to
            # 'searching' by THIS (later, unrelated) download's failure -- e.g. a
            # supplementary per-episode re-grab for an already-available season.
            # This download's own row still moves to Failed (+ blocklist) below
            # regardless, so the failure stays fully visible in the queue.
            await season_request_service.set_status(
                session,
                media_request_id=record.media_request_id,
                season_number=record.season,
                status=RequestStatus.searching.value,
                skip_if_terminal=True,
            )
        else:
            await request_repo.set_status(record.media_request_id, RequestStatus.searching.value)

    # Complete the FailedPending -> Failed transition. The reconciler only moves
    # the row as far as ``failed_pending``; without this advance the row would be
    # stranded there forever — it is neither terminal (so it lingers in
    # ``list_active`` and the queue shows a zombie torrent) nor active (so the
    # reconciler never revisits it). The blocklist + re-search has now fired, so
    # the row is genuinely Failed.
    if record is not None:
        await SqlDownloadRepository(session).update_status(
            record.id, DownloadState.Failed.value, failed_reason=event.reason
        )

    # Close the seeding leak (ADR-0014): remove the blocklisted torrent + its data.
    # Best-effort (logged, never raised) so a client hiccup never undoes the
    # blocklist/re-arm just written; an already-gone hash is a no-op success.
    await purge_service.remove_torrent(
        qbt,
        event.torrent_hash,
        context="a reconcile-driven download failure",
        extra={"torrent_hash": event.torrent_hash, "tmdb_id": event.tmdb_id},
    )


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
    for row in rows:
        live = snapshot.get(row.torrent_hash.lower())
        transition = transitions_by_id.get(row.id)
        if transition is not None:
            await download_repo.update_status(
                transition.download_id,
                transition.to_state.value,
                progress=live.progress if live is not None else None,
                seed_ratio=live.ratio if live is not None else None,
                first_seen_at=now if transition.set_first_seen_at else None,
                clear_first_seen_at=transition.clear_first_seen_at,
            )
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

    for event in failed_download_events(transitions, rows, occurred_at=now):
        await _handle_failed(session, qbt, event, rows)

    await session.commit()
    return await download_repo.list_active()


async def mark_failed(
    session: AsyncSession,
    qbt: DownloadClientPort,
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
    """
    download_repo = SqlDownloadRepository(session)
    row = await session.get(Download, download_id)
    if row is None:
        raise LookupError(f"download {download_id} does not exist")

    current = DownloadState(row.status)
    if current.value in _TERMINAL_STATUS_VALUES:
        raise InvalidStateTransitionError(current.value, DownloadState.Failed.value)

    # Route through FailedPending when the legal graph requires it (e.g. an
    # actively Downloading torrent cannot jump straight to Failed).
    if not is_legal_transition(current, DownloadState.Failed):
        if not is_legal_transition(current, DownloadState.FailedPending):
            raise InvalidStateTransitionError(current.value, DownloadState.Failed.value)
        await download_repo.update_status(download_id, DownloadState.FailedPending.value)

    await download_repo.update_status(
        download_id,
        DownloadState.Failed.value,
        failed_reason="marked failed by operator",
    )

    if blocklist:
        source_title = (
            await blocklist_service.source_title_for(session, row.torrent_hash) or row.torrent_hash
        )
        indexer = await blocklist_service.indexer_for(session, row.torrent_hash)
        await SqlBlocklistRepository(session).create(
            source_title=source_title,
            reason=BlocklistReason.user_reported.value,
            tmdb_id=row.tmdb_id,
            torrent_hash=row.torrent_hash,
            indexer=indexer,
            # Scope by media namespace (see _handle_failed) — season present => tv.
            media_type="tv" if row.season is not None else "movie",
        )

    # Re-arm the owning request unconditionally — the blocklist flag governs ONLY
    # whether a Blocklist row is written, NOT whether the request status is
    # corrected. Without this, a mark-failed(blocklist=false) drives the download
    # to terminal Failed (gone from the active queue) while the request stays
    # 'downloading' forever: a dishonest status asserting an active download that
    # no longer exists, with nothing to re-search or re-fail it. Mirrors the
    # reconcile-driven ``_handle_failed`` re-arm.
    if row.media_request_id is not None:
        if row.season is not None:
            # TV: same rollup-aware routing (and the same terminal-season guard)
            # as _handle_failed above.
            await season_request_service.set_status(
                session,
                media_request_id=row.media_request_id,
                season_number=row.season,
                status=RequestStatus.searching.value,
                skip_if_terminal=True,
            )
        else:
            await SqlRequestRepository(session).set_status(
                row.media_request_id, RequestStatus.searching.value
            )

    await session.commit()

    # Close the seeding leak (ADR-0014): remove the torrent + its data AFTER the
    # DB fail/blocklist/re-arm has committed, so a client hiccup never undoes that
    # committed state. Best-effort + already-gone-is-a-no-op (see
    # ``purge_service.remove_torrent``).
    if remove_torrent:
        await purge_service.remove_torrent(
            qbt,
            row.torrent_hash,
            context="an operator mark-failed",
            extra={"torrent_hash": row.torrent_hash, "download_id": safe_int(download_id)},
        )

    failed = await download_repo.get_by_hash(row.torrent_hash)
    if failed is None:  # pragma: no cover - just updated this row
        raise LookupError(f"download {download_id} vanished mid-update")
    return failed
