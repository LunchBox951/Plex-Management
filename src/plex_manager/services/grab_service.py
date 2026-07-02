"""Grab orchestration — hand the chosen release to qBittorrent, track it.

``grab`` adds the top (or operator-chosen) :class:`ScoredRelease` to the download
client, then records a ``downloads`` row (``Downloading``) plus an append-only
``download_history`` ``grabbed`` event — the durable state-recovery anchor. The
alpha pipeline stops here: the reconciler tracks the torrent from this point.

Idempotency & one-active-per-request: re-grabbing the SAME torrent is a no-op —
the guard checks both the candidate's pre-known info-hash and the hash
qBittorrent returns (a 409 "already present" resolves to the existing hash), so a
double-click never creates a second row or history event. Grabbing a DIFFERENT
release while the request already has an active download is refused with
``AlreadyDownloadingError`` (the app-level guard, backstopped by the
``uq_downloads_active_request`` partial unique index under true concurrency), so a
request never ends up with two active downloads racing each other.
"""

from __future__ import annotations

import logging
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
from plex_manager.services import season_request_service
from plex_manager.services.request_service import TERMINAL_REQUEST_STATUS_VALUES

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from plex_manager.domain.release import ScoredRelease
    from plex_manager.ports.download_client import DownloadClientPort
    from plex_manager.ports.repositories import DownloadRecord

__all__ = [
    "DEFAULT_CATEGORY",
    "AlreadyDownloadingError",
    "DownloadScopeConflictError",
    "GrabError",
    "NoGrabSourceError",
    "RequestNotActiveError",
    "SeasonRequiredError",
    "TorrentAlreadyTrackedError",
    "grab",
]

_logger = logging.getLogger(__name__)

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


class AlreadyDownloadingError(Exception):
    """The request already has a DIFFERENT active download — refuse a parallel grab.

    Surfaced (HTTP 409 ``already_downloading``), never a silent second row. The
    duplicate guard makes re-grabbing the SAME release idempotent; this catches
    grabbing a *different* accepted release while one is still in flight, which
    would create a second active row for the request (and a later failure of either
    would wrongly re-arm the request while the other still runs). Honesty over
    silence: tell the operator one is already downloading.
    """

    def __init__(self, request_id: int) -> None:
        self.request_id = request_id
        super().__init__(f"request {request_id} already has an active download")


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


class RequestNotActiveError(Exception):
    """The request being grabbed is already terminal — refuse before adding anything.

    Surfaced (HTTP 409 ``request_not_active``), never a silent 500. A stale,
    terminal request id (``completed`` / ``available`` / ``failed``) can still be
    handed to ``/queue/grab``; a newer ACTIVE request for the same
    ``(tmdb_id, media_type)`` now owns the ``uq_media_requests_active`` slot. Adding
    the torrent first and only then trying to drive the old row back to
    ``downloading`` would have the partial unique index reject the update, leaving
    an untracked torrent behind. Honesty over silence: reject up front so nothing
    is added.
    """

    def __init__(self, request_id: int) -> None:
        self.request_id = request_id
        super().__init__(f"request {request_id} is terminal and cannot be grabbed")


class SeasonRequiredError(Exception):
    """A TV request was grabbed with no ``season`` — every TV grab is per-season.

    Surfaced (HTTP 422 ``tv_grab_requires_season``), never a silent unscoped grab.
    An unscoped TV download would persist with ``season=None``, so
    :func:`grab` would update the parent ``MediaRequest`` directly instead of a
    ``SeasonRequest`` (breaking the "status is a computed rollup" invariant), and
    the importer would later block the download as season-less. The endpoint
    already rejects this before even previewing; this is the domain-boundary
    backstop so the invariant holds regardless of caller.
    """

    def __init__(self, request_id: int) -> None:
        self.request_id = request_id
        super().__init__(f"request {request_id} is tv and requires a season to grab")


class DownloadScopeConflictError(Exception):
    """The same torrent is already active for an INCOMPATIBLE scope — refuse the reuse.

    Surfaced (HTTP 409 ``download_scope_conflict``), never a silent no-op. A
    ``Download.torrent_hash`` is UNIQUE (one physical torrent = one row with one
    ``(season, episodes)`` scope), so an already-active pack grabbed for a DIFFERENT
    season — or a different episode subset the active row does not cover — cannot be
    tracked as a second row. Without this guard the same-hash reuse returned the
    FIRST scope's row as an idempotent no-op: the newly requested season/episodes
    were never marked ``downloading`` and the import only ever processed the stale
    scope, silently stranding the rest. Honesty over silence: tell the operator the
    torrent is already downloading a different scope (one download satisfying many
    seasons/episodes is a tracked follow-up, not this row's job).
    """

    def __init__(
        self,
        torrent_hash: str,
        *,
        active_season: int | None,
        active_episodes: list[int] | None,
        requested_season: int | None,
        requested_episodes: list[int] | None,
    ) -> None:
        self.torrent_hash = torrent_hash
        self.active_season = active_season
        self.requested_season = requested_season
        super().__init__(
            f"torrent {torrent_hash} is already active with an incompatible scope "
            f"(active season={active_season} episodes={active_episodes}; "
            f"requested season={requested_season} episodes={requested_episodes})"
        )


def _reuse_conflicts(
    existing: DownloadRecord, season: int | None, episodes: list[int] | None
) -> bool:
    """Whether an active same-hash row's scope fails to COVER the requested one.

    Returning a non-covering row as an idempotent no-op would silently leave the new
    scope untracked (the importer only ever processes the active row's stored scope):

    - a DIFFERENT ``season`` always conflicts (a different ``SeasonRequest``);
    - same season, the active row's EPISODE scope must cover the request:
      ``episodes_json is None`` imports the whole season -> covers any request; a
      whole-season request (``episodes is None``) is covered ONLY by a whole-season
      active row; otherwise the request's episodes must be a SUBSET of the active
      row's episodes.

    Movies (both seasons ``None``, both episode lists ``None``) always cover -> the
    reuse stays an idempotent no-op, unchanged.
    """
    if existing.season != season:
        return True
    if existing.episodes is None:
        return False
    if episodes is None:
        return True
    return not set(episodes).issubset(set(existing.episodes))


class TorrentAlreadyTrackedError(Exception):
    """The same torrent hash is already active under another request."""

    def __init__(self, torrent_hash: str, owner_request_id: int | None) -> None:
        self.torrent_hash = torrent_hash
        self.owner_request_id = owner_request_id
        super().__init__(f"torrent {torrent_hash} is already tracked by request {owner_request_id}")


async def _reuse_terminal_row(
    session: AsyncSession,
    download_repo: SqlDownloadRepository,
    download_id: int,
    torrent_hash: str,
    request_id: int | None,
    *,
    source: str,
    tmdb_id: int | None,
    year: int | None,
    season: int | None,
    episodes: list[int] | None,
    media_type: str | None,
) -> tuple[DownloadRecord, bool]:
    """Drive a terminal (Failed/Imported) row back to Downloading and re-own it.

    A previously-failed (not blocklisted) release may legitimately be grabbed
    afresh; the row's ``torrent_hash`` is UNIQUE, so we reuse the existing row
    rather than colliding on a fresh insert. The stale failure reason is cleared
    and ``media_request_id`` is repointed at the CURRENT request (it may differ
    from the prior owner) so the row is owned by the active request.

    The stale ``first_seen_at`` grace anchor is also reset: a row that previously
    went ``ClientMissing`` carries its old anchor, and driving it straight back to
    ``Downloading`` without clearing it would let the reconciler fast-fail this
    fresh grab against the long-expired window. ``clear_first_seen_at`` gives the
    re-grab a clean grace window.

    The stale ``download_path`` breadcrumb is likewise cleared: an ``Imported`` row
    carries ``download_path`` pointing at the OLD Plex library file. Left in place,
    the next import's ``_resolve_content`` would fall back to that stale library
    path when the client reports no ``content_path`` and validate the wrong file —
    blocking the fresh download as no-video if the old file is gone, or wrongly
    completing the new request without importing it if it still exists.
    ``clear_download_path`` drops the breadcrumb so the re-grab tracks its own
    content.

    The TV scope (``season``/``episodes_json``) is likewise REFRESHED to the
    CURRENT grab's scope, unconditionally (``set_scope=True``): a resurrected row
    otherwise keeps whatever season/episodes it was created with, so re-selecting
    the SAME torrent hash under a different season (a multi-season pack) or a
    narrower episode filter would leave the queue/importer operating on the
    WRONG episodes while the newly requested season is marked downloading.
    Unconditional (not ``is not None``-gated) so a movie reuse correctly clears
    any stale season/episodes back to ``None`` too.
    """
    claimed = await download_repo.update_status_if_in(
        download_id,
        DownloadState.Downloading.value,
        _TERMINAL_STATUS_VALUES,
        progress=0.0,
        seed_ratio=0.0,
        clear_failed_reason=True,
        clear_first_seen_at=True,
        clear_download_path=True,
        media_request_id=request_id,
        replace_grab_metadata=True,
        magnet_link=source,
        tmdb_id=tmdb_id,
        year=year,
        season=season,
        episodes=episodes,
        media_type=media_type,
    )
    if not claimed:
        await session.rollback()
        record = await download_repo.get_by_hash(torrent_hash)
        if record is None:  # pragma: no cover - the row existed before the CAS
            raise LookupError(f"download for hash {torrent_hash} vanished mid-grab")
        return record, False
    record = await download_repo.get_by_hash(torrent_hash)
    if record is None:  # pragma: no cover - just updated this row
        raise LookupError(f"download for hash {torrent_hash} vanished mid-grab")
    return record, True


async def grab(
    qbt: DownloadClientPort,
    session: AsyncSession,
    *,
    scored: ScoredRelease,
    request_id: int | None = None,
    tmdb_id: int | None = None,
    year: int | None = None,
    season: int | None = None,
    episodes: list[int] | None = None,
    save_path: str = "",
    category: str = DEFAULT_CATEGORY,
) -> DownloadRecord:
    """Grab ``scored``: add it to the client and persist a tracked download.

    Returns the existing record (without re-adding to the client a second time)
    when a non-terminal download for the same hash already exists.

    ``season`` (TV only) scopes the one-active-download guard PER SEASON (a
    whole-series request can have S1 and S2 downloading at once) and is threaded
    onto the persisted ``Download`` row; ``episodes`` (TV only) persists to
    ``Download.episodes_json`` -- ``None`` means import every valid video file
    found for the season, an explicit list scopes the import to those episode
    numbers only (a season-pack grab scoped to specific missing episodes).

    When ``request_id`` resolves to a real request, ``season``/``episodes`` are
    validated (and, for a movie, silently coerced) against the request's ACTUAL
    ``media_type`` rather than trusted at face value: a ``tv`` request with no
    ``season`` raises :class:`SeasonRequiredError` (a grab is always per-season,
    never a whole-series scan), and a non-``tv`` request has ``season``/
    ``episodes`` forced back to ``None`` regardless of what the caller passed, so
    a movie can never spawn a ``SeasonRequest`` row or scope its active-download
    guard to a fake season.
    """
    download_repo = SqlDownloadRepository(session)
    candidate = scored.candidate

    source = candidate.magnet_url or candidate.download_url
    if source is None:
        raise NoGrabSourceError(candidate.guid)

    request_media_type: str | None = None
    # Reject a stale/terminal request id BEFORE handing anything to the client. If
    # this request is already terminal, a newer ACTIVE request for the same media
    # owns the ``uq_media_requests_active`` slot, so re-arming this row to
    # ``downloading`` would be rejected by that index — but only AFTER qbt.add had
    # already created an untracked torrent. Refuse up front so nothing is added.
    if request_id is not None:
        request = await SqlRequestRepository(session).get(request_id)
        if request is not None:
            if request.status in TERMINAL_REQUEST_STATUS_VALUES:
                raise RequestNotActiveError(request_id)
            request_media_type = request.media_type
            # Domain-boundary backstop: branch on the request's ACTUAL media
            # type, never on whether the caller merely passed a ``season``. The
            # endpoint already enforces this (422 before even previewing), but
            # this invariant must hold regardless of caller.
            if request.media_type == "tv":
                if season is None:
                    raise SeasonRequiredError(request_id)
            else:
                # Non-tv (movie): season/episodes are meaningless -- coerce
                # rather than trust the caller, so a movie can never spawn a
                # SeasonRequest row or have its one-active guard scoped to a
                # fake season.
                season = None
                episodes = None

    # Pre-check on the candidate's own hash (when the indexer supplied one) so a
    # known duplicate never even hits the client.
    known_hash = candidate.info_hash.lower() if candidate.info_hash else None
    if known_hash is not None:
        pre = await download_repo.get_by_hash(known_hash)
        if pre is not None and pre.status not in _TERMINAL_STATUS_VALUES:
            if request_id is not None and pre.media_request_id != request_id:
                raise TorrentAlreadyTrackedError(known_hash, pre.media_request_id)
            # Idempotent only when the SCOPE matches. The same physical torrent
            # active for a DIFFERENT season (a multi-season pack re-grabbed per
            # season) must not be returned as a no-op -- that leaves the new season
            # untracked (see DownloadScopeConflictError). A movie (both None) or the
            # same season returns the row unchanged.
            if _reuse_conflicts(pre, season, episodes):
                raise DownloadScopeConflictError(
                    known_hash,
                    active_season=pre.season,
                    active_episodes=pre.episodes,
                    requested_season=season,
                    requested_episodes=episodes,
                )
            return pre

    # Parallel-grab guard: if this request already has an active (non-terminal)
    # download for a DIFFERENT release, refuse rather than create a second active
    # row. The known-hash precheck above already returned for the SAME release, so
    # an active download whose hash differs (or that we can't yet match because the
    # indexer gave no hash) is a genuine second grab. Checked BEFORE handing the
    # torrent to the client, so nothing is added on rejection.
    if request_id is not None:
        active = await download_repo.find_active_for_request(request_id, season=season)
        if active is not None and active.torrent_hash != known_hash:
            raise AlreadyDownloadingError(request_id)

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
        if request_id is not None and existing.media_request_id != request_id:
            raise TorrentAlreadyTrackedError(torrent_hash, existing.media_request_id)
        # Same scope-match guard as the known-hash precheck, for the case the indexer
        # gave no hash so this is the first time we see the real one from qbt.add.
        # Re-adding the same magnet is a qBittorrent no-op, so nothing is orphaned.
        if _reuse_conflicts(existing, season, episodes):
            raise DownloadScopeConflictError(
                torrent_hash,
                active_season=existing.season,
                active_episodes=existing.episodes,
                requested_season=season,
                requested_episodes=episodes,
            )
        return existing

    if existing is not None:
        try:
            record, claimed_reuse = await _reuse_terminal_row(
                session,
                download_repo,
                existing.id,
                torrent_hash,
                request_id,
                source=source,
                tmdb_id=tmdb_id,
                year=year,
                season=season,
                episodes=episodes,
                media_type=request_media_type,
            )
            if not claimed_reuse:
                if record.status not in _TERMINAL_STATUS_VALUES:
                    if request_id is not None and record.media_request_id != request_id:
                        raise TorrentAlreadyTrackedError(torrent_hash, record.media_request_id)
                    return record
                raise TorrentAlreadyTrackedError(torrent_hash, record.media_request_id)
        except IntegrityError:
            await session.rollback()
            if request_id is not None:
                active = await download_repo.find_active_for_request(request_id, season=season)
                if active is not None and active.torrent_hash != torrent_hash:
                    try:
                        await qbt.remove(torrent_hash, delete_files=True)
                    except Exception:
                        _logger.warning(
                            "failed to remove orphaned torrent %s after losing a "
                            "terminal-row reuse race for request %s",
                            torrent_hash,
                            request_id,
                            exc_info=True,
                        )
                    raise AlreadyDownloadingError(request_id) from None
            raise
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
                episodes=episodes,
                media_type=request_media_type,
            )
        except IntegrityError:
            # A concurrent grab won the race. It either grabbed the SAME release
            # (``torrent_hash`` UNIQUE) or a DIFFERENT release for this request
            # (``uq_downloads_active_request`` — the DB backstop to the TOCTOU
            # guard above). Roll back and distinguish, so neither becomes an opaque
            # 500: a different-release conflict is the honest ``already_downloading``.
            await session.rollback()
            if request_id is not None:
                active = await download_repo.find_active_for_request(request_id, season=season)
                if active is not None and active.torrent_hash != torrent_hash:
                    # The other release won the request's single active slot. The
                    # torrent we just added to qBittorrent is now orphaned — nothing
                    # tracks it, so it would seed forever consuming bandwidth.
                    # Best-effort remove it (deleting its files) before refusing the
                    # parallel grab; if the remove fails we still raise, but log it
                    # so the leak is visible (honesty over silence).
                    try:
                        await qbt.remove(torrent_hash, delete_files=True)
                    except Exception:
                        _logger.warning(
                            "failed to remove orphaned torrent %s after losing a "
                            "parallel grab for request %s",
                            torrent_hash,
                            request_id,
                            exc_info=True,
                        )
                    raise AlreadyDownloadingError(request_id) from None
            winner = await download_repo.get_by_hash(torrent_hash)
            if winner is None:  # pragma: no cover - the conflicting row must exist
                raise
            if winner.status not in _TERMINAL_STATUS_VALUES:
                if request_id is not None and winner.media_request_id != request_id:
                    raise TorrentAlreadyTrackedError(
                        torrent_hash, winner.media_request_id
                    ) from None
                # Same scope-conflict guard as the non-race precheck: two grabs for
                # the same hash but a DIFFERENT tv scope can race past the prechecks,
                # the loser hitting UNIQUE(torrent_hash) here. Returning the winner's
                # (first-scope) row as a no-op would leave the loser's season/episodes
                # untracked, so refuse it honestly rather than silently stranding it.
                if _reuse_conflicts(winner, season, episodes):
                    raise DownloadScopeConflictError(
                        torrent_hash,
                        active_season=winner.season,
                        active_episodes=winner.episodes,
                        requested_season=season,
                        requested_episodes=episodes,
                    ) from None
                return winner
            record, claimed_reuse = await _reuse_terminal_row(
                session,
                download_repo,
                winner.id,
                torrent_hash,
                request_id,
                source=source,
                tmdb_id=tmdb_id,
                year=year,
                season=season,
                episodes=episodes,
                media_type=request_media_type,
            )
            if not claimed_reuse:
                if record.status not in _TERMINAL_STATUS_VALUES:
                    if request_id is not None and record.media_request_id != request_id:
                        raise TorrentAlreadyTrackedError(
                            torrent_hash, record.media_request_id
                        ) from None
                    return record
                raise TorrentAlreadyTrackedError(torrent_hash, record.media_request_id) from None
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
        # ``season is not None`` here is exactly "this is a tv request": the guard
        # above raises SeasonRequiredError for a tv request with no season, and
        # coerces season back to None for a non-tv request -- so this never
        # branches on a caller-supplied season alone, only on the media type it
        # was already validated against.
        if season is not None:
            # TV: the request's status is a COMPUTED rollup of its seasons, never a
            # direct target -- route through season_request_service so the season
            # row (created lazily here, like the Download row) moves to
            # 'downloading' and the parent's rollup is recomputed in the same
            # transaction, rather than stomping the request status directly.
            await season_request_service.set_status(
                session,
                media_request_id=request_id,
                season_number=season,
                status=RequestStatus.downloading.value,
            )
        else:
            await SqlRequestRepository(session).set_status(
                request_id, RequestStatus.downloading.value
            )
    await session.commit()
    return record
