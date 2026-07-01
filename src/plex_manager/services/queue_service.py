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

from sqlalchemy import select

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
from plex_manager.models import (
    BlocklistReason,
    Download,
    DownloadHistory,
    RequestStatus,
)
from plex_manager.repositories.blocklist import SqlBlocklistRepository
from plex_manager.repositories.downloads import SqlDownloadRepository
from plex_manager.repositories.requests import SqlRequestRepository

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


async def _source_title_for(session: AsyncSession, torrent_hash: str) -> str | None:
    """Best-effort original release title from the download history (for blocklist)."""
    stmt = (
        select(DownloadHistory.source_title)
        .where(DownloadHistory.torrent_hash == torrent_hash)
        .where(DownloadHistory.source_title.is_not(None))
        .order_by(DownloadHistory.id.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalars().first()


async def _indexer_for(session: AsyncSession, torrent_hash: str) -> str | None:
    """Best-effort originating indexer from the download history (for blocklist).

    Recorded at grab time (``grab_service`` writes ``DownloadHistory.indexer``).
    Without it a blocklist row has ``indexer=None``, so the pure two-tier identity
    check can never fall back to title+indexer for a candidate that exposes no
    info_hash — defeating blocklist-then-research for hashless feeds.
    """
    stmt = (
        select(DownloadHistory.indexer)
        .where(DownloadHistory.torrent_hash == torrent_hash)
        .where(DownloadHistory.indexer.is_not(None))
        .order_by(DownloadHistory.id.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalars().first()


async def _handle_failed(
    session: AsyncSession,
    event: DownloadFailed,
    rows: list[DownloadRecord],
) -> None:
    """Blocklist a failed release and re-arm its request for a fresh search."""
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
    source_title = await _source_title_for(session, event.torrent_hash) or event.source_title
    indexer = await _indexer_for(session, event.torrent_hash)
    await blocklist_repo.create(
        source_title=source_title,
        reason=BlocklistReason.failed.value,
        tmdb_id=request.tmdb_id if request is not None else event.tmdb_id,
        torrent_hash=event.torrent_hash,
        indexer=indexer,
        media_type=request.media_type if request is not None else None,
    )
    if record is not None and record.media_request_id is not None:
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
        await _handle_failed(session, event, rows)

    await session.commit()
    return await download_repo.list_active()


async def mark_failed(
    session: AsyncSession,
    *,
    download_id: int,
    blocklist: bool,
) -> DownloadRecord:
    """Operator move: fail a download (and optionally blocklist its release)."""
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
        source_title = await _source_title_for(session, row.torrent_hash) or row.torrent_hash
        indexer = await _indexer_for(session, row.torrent_hash)
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
            media_type=request.media_type if request is not None else None,
        )

    # Re-arm the owning request unconditionally — the blocklist flag governs ONLY
    # whether a Blocklist row is written, NOT whether the request status is
    # corrected. Without this, a mark-failed(blocklist=false) drives the download
    # to terminal Failed (gone from the active queue) while the request stays
    # 'downloading' forever: a dishonest status asserting an active download that
    # no longer exists, with nothing to re-search or re-fail it. Mirrors the
    # reconcile-driven ``_handle_failed`` re-arm.
    if row.media_request_id is not None:
        await SqlRequestRepository(session).set_status(
            row.media_request_id, RequestStatus.searching.value
        )

    await session.commit()
    failed = await download_repo.get_by_hash(row.torrent_hash)
    if failed is None:  # pragma: no cover - just updated this row
        raise LookupError(f"download {download_id} vanished mid-update")
    return failed
