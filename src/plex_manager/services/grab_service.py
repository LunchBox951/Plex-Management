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

__all__ = ["DEFAULT_CATEGORY", "NoGrabSourceError", "grab"]

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
        # The client accepted it but no hash could be derived (rare opaque URL):
        # fall back to the guid so the row is still uniquely keyed and trackable.
        torrent_hash = candidate.guid

    existing = await download_repo.get_by_hash(torrent_hash)
    if existing is not None and existing.status not in _TERMINAL_STATUS_VALUES:
        return existing

    if existing is not None:
        # A terminal row (Failed/Imported) already owns this hash. ``torrent_hash``
        # is UNIQUE, so a plain insert would raise IntegrityError -> opaque 500.
        # A previously-failed (not blocklisted) release may legitimately be
        # grabbed afresh, so REUSE the row: drive it back to Downloading and clear
        # the stale failure reason, rather than colliding on the constraint.
        await download_repo.update_status(
            existing.id,
            DownloadState.Downloading.value,
            clear_failed_reason=True,
        )
        record = await download_repo.get_by_hash(torrent_hash)
        if record is None:  # pragma: no cover - just updated this row
            raise LookupError(f"download for hash {torrent_hash} vanished mid-grab")
    else:
        record = await download_repo.create(
            torrent_hash=torrent_hash,
            status=DownloadState.Downloading.value,
            media_request_id=request_id,
            magnet_link=source,
            tmdb_id=tmdb_id,
            year=year,
            season=season,
        )
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
