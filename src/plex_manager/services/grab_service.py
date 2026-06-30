"""Grab orchestration — hand the chosen release to qBittorrent, track it.

``grab`` adds the top (or operator-chosen) :class:`ScoredRelease` to the download
client, then records a ``downloads`` row (``Downloading``) plus an append-only
``download_history`` ``grabbed`` event — the durable state-recovery anchor. The
alpha pipeline stops here: the reconciler tracks the torrent from this point.

Idempotency: a duplicate grab of the same torrent is a no-op. The guard checks
both the candidate's pre-known info-hash and the hash qBittorrent returns (a 409
"already present" resolves to the existing hash), so a double-click never creates
a second row or a second history event.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from sqlalchemy.exc import IntegrityError

from plex_manager.domain.state_machine import TERMINAL_STATES, DownloadState
from plex_manager.models import (
    DownloadHistory,
    DownloadHistoryEvent,
    RequestStatus,
)
from plex_manager.repositories.downloads import SqlDownloadRepository
from plex_manager.repositories.requests import SqlRequestRepository

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from plex_manager.domain.release import ScoredRelease
    from plex_manager.ports.download_client import DownloadClientPort
    from plex_manager.ports.repositories import DownloadRecord

__all__ = ["DEFAULT_CATEGORY", "GrabError", "NoGrabSourceError", "grab"]

# The qBittorrent category the app tags its torrents with (lets a later import
# pipeline filter to only app-managed downloads).
DEFAULT_CATEGORY: Final = "plex-manager"

# Terminal download states (string values) — a download in one of these is
# finished, so an identical hash may be grabbed afresh.
_TERMINAL_STATUS_VALUES: Final[frozenset[str]] = frozenset(s.value for s in TERMINAL_STATES)


class NoGrabSourceError(Exception):
    """The chosen release exposes neither a magnet nor a download url.

    Surfaced (HTTP 409), never a silent skip — there is nothing to hand the
    client.
    """

    def __init__(self, guid: str) -> None:
        self.guid = guid
        super().__init__(f"release {guid} has no magnet or download url")


class GrabError(Exception):
    """qBittorrent accepted the grab but no real torrent info-hash could be found.

    Surfaced (HTTP 409), never silently tracked: the prior behaviour stored the
    indexer ``guid`` as ``downloads.torrent_hash`` when the client returned no
    derivable hash and the indexer omitted ``infoHash``. The reconciler then never
    matches that fake hash against the client snapshot, so the download is wrongly
    declared ``ClientMissing`` and fails after the grace window — a false failure.
    Honesty over silence: refuse to track an unmatchable grab and tell the operator.
    """

    def __init__(self, title: str) -> None:
        self.title = title
        super().__init__(f"could not determine torrent hash for {title}")


async def _reuse_terminal_row(
    download_repo: SqlDownloadRepository,
    download_id: int,
    torrent_hash: str,
    request_id: int | None,
) -> DownloadRecord:
    """Drive a terminal (Failed/Imported) row back to Downloading and re-own it.

    A previously-failed (not blocklisted) release may legitimately be grabbed
    afresh; the row's ``torrent_hash`` is UNIQUE, so we reuse the existing row
    rather than colliding on a fresh insert. The stale failure reason is cleared
    and ``media_request_id`` is repointed at the CURRENT request (it may differ
    from the prior owner) so the row is owned by the active request.
    """
    await download_repo.update_status(
        download_id,
        DownloadState.Downloading.value,
        clear_failed_reason=True,
        media_request_id=request_id,
    )
    record = await download_repo.get_by_hash(torrent_hash)
    if record is None:  # pragma: no cover - just updated this row
        raise LookupError(f"download for hash {torrent_hash} vanished mid-grab")
    return record


async def grab(
    qbt: DownloadClientPort,
    session: AsyncSession,
    *,
    scored: ScoredRelease,
    request_id: int | None = None,
    tmdb_id: int | None = None,
    year: int | None = None,
    season: int | None = None,
    save_path: str = "",
    category: str = DEFAULT_CATEGORY,
) -> DownloadRecord:
    """Grab ``scored``: add it to the client and persist a tracked download.

    Returns the existing record (without re-adding to the client a second time)
    when a non-terminal download for the same hash already exists.
    """
    download_repo = SqlDownloadRepository(session)
    candidate = scored.candidate

    source = candidate.magnet_url or candidate.download_url
    if source is None:
        raise NoGrabSourceError(candidate.guid)

    # Pre-check on the candidate's own hash (when the indexer supplied one) so a
    # known duplicate never even hits the client.
    known_hash = candidate.info_hash.lower() if candidate.info_hash else None
    if known_hash is not None:
        pre = await download_repo.get_by_hash(known_hash)
        if pre is not None and pre.status not in _TERMINAL_STATUS_VALUES:
            return pre

    torrent_hash = (await qbt.add(source, save_path, category)).lower() or (known_hash or "")
    if not torrent_hash:
        # The client accepted it but no real info-hash could be derived (rare
        # opaque URL) and the indexer supplied none either. Tracking by the indexer
        # guid would never match the client snapshot, so the reconciler would
        # false-fail it as ClientMissing. Surface the failure instead of silently
        # tracking an unmatchable row.
        raise GrabError(candidate.title)

    existing = await download_repo.get_by_hash(torrent_hash)
    if existing is not None and existing.status not in _TERMINAL_STATUS_VALUES:
        return existing

    if existing is not None:
        record = await _reuse_terminal_row(download_repo, existing.id, torrent_hash, request_id)
    else:
        try:
            record = await download_repo.create(
                torrent_hash=torrent_hash,
                status=DownloadState.Downloading.value,
                media_request_id=request_id,
                magnet_link=source,
                tmdb_id=tmdb_id,
                year=year,
                season=season,
            )
        except IntegrityError:
            # Two concurrent grabs for the same release both passed the
            # get_by_hash check above, then both tried to INSERT — ``torrent_hash``
            # is UNIQUE, so the loser lands here. Recover instead of crashing
            # (opaque 500): roll back the failed insert, re-fetch the winner, and
            # reuse it — the same recovery as the terminal-reuse path.
            await session.rollback()
            winner = await download_repo.get_by_hash(torrent_hash)
            if winner is None:  # pragma: no cover - the conflicting row must exist
                raise
            if winner.status not in _TERMINAL_STATUS_VALUES:
                return winner
            record = await _reuse_terminal_row(download_repo, winner.id, torrent_hash, request_id)
    session.add(
        DownloadHistory(
            tmdb_id=tmdb_id,
            torrent_hash=torrent_hash,
            event_type=DownloadHistoryEvent.grabbed,
            source_title=candidate.title,
            indexer=candidate.indexer_name,
            message=f"grabbed {scored.quality.name} from {candidate.indexer_name}",
        )
    )
    if request_id is not None:
        await SqlRequestRepository(session).set_status(request_id, RequestStatus.downloading.value)
    await session.commit()
    return record
