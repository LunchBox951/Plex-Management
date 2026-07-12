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
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from sqlalchemy.exc import IntegrityError

from plex_manager.domain.reconciler import METADATA_STALL_WINDOW
from plex_manager.domain.state_machine import TERMINAL_STATES, DownloadState
from plex_manager.logsafe import safe_int, safe_text
from plex_manager.models import (
    DownloadHistory,
    DownloadHistoryEvent,
    RequestStatus,
)
from plex_manager.repositories.downloads import SqlDownloadRepository
from plex_manager.repositories.requests import SqlRequestRepository
from plex_manager.repositories.season_requests import SqlSeasonRequestRepository
from plex_manager.services import queue_service, season_request_service
from plex_manager.services.request_service import TERMINAL_REQUEST_STATUS_VALUES

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from plex_manager.domain.release import ScoredRelease
    from plex_manager.ports.download_client import DownloadClientPort
    from plex_manager.ports.repositories import DownloadRecord

__all__ = [
    "DEFAULT_CATEGORY",
    "AlreadyDownloadingError",
    "GrabError",
    "NoGrabSourceError",
    "RequestNotActiveError",
    "SeasonRequiredError",
    "TorrentAlreadyTrackedError",
    "TorrentRemovalInFlightError",
    "grab",
]

_logger = logging.getLogger(__name__)

# The qBittorrent category the app tags its torrents with (lets a later import
# pipeline filter to only app-managed downloads).
DEFAULT_CATEGORY: Final = "plex-manager"

# Terminal download states (string values) — a download in one of these is
# finished, so an identical hash may be grabbed afresh.
_TERMINAL_STATUS_VALUES: Final[frozenset[str]] = frozenset(s.value for s in TERMINAL_STATES)

# Post-add status-move CAS fallbacks. ``grab`` awaits ``qbt.add(...)`` BEFORE it
# moves the request/season to ``downloading``, and a concurrent writer -- the
# operator's cancel verb, the eviction restore's redundant-re-grab
# reconciliation, or the eviction recovery's failed-purge FOLD (``pending`` ->
# ``available``: the file never left disk) -- can commit a new status during
# that await. The up-front gate ran before the add, so the post-add move must be
# a compare-and-swap against EXACTLY the status the grab decision OBSERVED
# (``observed_request_status`` / ``observed_season_status`` in :func:`grab`),
# never a broader set: a row that read ``pending`` at decision time and was
# folded to ``available`` mid-add must LOSE (grabbing it would download a
# duplicate of on-disk content), while an INTENTIONAL reopen -- where the
# decision itself observed ``available``/``completed`` (a season re-grab chasing
# one more missing episode) -- still wins, because ``available`` is exactly what
# it compares. A losing CAS is handled like the other post-add losses (see the
# lost-parallel-grab branches): roll back, best-effort remove the just-added
# torrent, raise the honest ``RequestNotActiveError``.
#
# These two sets are only the DEGENERATE fallbacks for when no status could be
# observed (the request row did not exist at decision time): the CAS then
# matches zero rows for a still-missing row regardless of the set, and a row
# minted mid-add is honored, never clobbered. The movie fallback mirrors the
# up-front terminal gate; the season fallback excludes only ``cancelled``.
_GRABBABLE_REQUEST_STATUS_VALUES: Final[frozenset[str]] = (
    frozenset(s.value for s in RequestStatus) - TERMINAL_REQUEST_STATUS_VALUES
)
_GRABBABLE_SEASON_STATUS_VALUES: Final[frozenset[str]] = frozenset(
    s.value for s in RequestStatus if s is not RequestStatus.cancelled
)
# Secondary pack targets must still be SEARCHABLE when the post-add CAS lands.
# Installed ``available``/``completed`` siblings are waste, not targets; accepting
# either here would let a stale plan move settled content back to downloading.
_PACK_TARGET_SEASON_STATUS_VALUES: Final[frozenset[str]] = frozenset(
    {
        RequestStatus.pending.value,
        RequestStatus.searching.value,
        RequestStatus.no_acceptable_release.value,
        RequestStatus.failed.value,
    }
)


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


class TorrentAlreadyTrackedError(Exception):
    """The same torrent hash is already active under another request."""

    def __init__(self, torrent_hash: str, owner_request_id: int | None) -> None:
        self.torrent_hash = torrent_hash
        self.owner_request_id = owner_request_id
        super().__init__(f"torrent {torrent_hash} is already tracked by request {owner_request_id}")


class TorrentRemovalInFlightError(Exception):
    """The terminal row's torrent is being removed right now — refuse to reuse it (#206).

    A concurrent ``cancel_request`` (or a reconcile-driven failure) has claimed this
    download's torrent removal as in flight. Resurrecting the terminal row would
    re-own a torrent whose data is mid-deletion, so the fresh grab would lose its
    payload the instant the removal completes. Surfaced (HTTP 409
    ``removal_in_progress``), never a silent reuse: refuse and let the request retry —
    by the next attempt the removal has settled (the torrent is gone, so a fresh add
    creates a new one, or, on a removal failure, its data is intact and reuse is safe).
    """

    def __init__(self, torrent_hash: str, download_id: int) -> None:
        self.torrent_hash = torrent_hash
        self.download_id = download_id
        super().__init__(f"torrent {torrent_hash} (download {download_id}) is being removed; retry")


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
    release_title: str | None,
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

    The stale ``added_at`` stall anchor is reset the same way (issue #165
    hardening finding): :func:`domain.reconciler.detect_stalls` anchors its
    metadata/stalled-progress windows on ``added_at`` ("when the row was
    grabbed"). Without resetting it here, a resurrected row keeps the ORIGINAL
    grab's timestamp — which, for a row that stalled, got self-healed, and is now
    being re-grabbed under the SAME torrent hash, may already be past the stall
    thresholds, so the very next reconcile tick would immediately misjudge the
    brand-new grab as stalled and mark-fail/remove/blocklist it before it ever had
    a chance to download (a heal-regrab-heal loop). Stamped to the CURRENT time so
    the fresh grab gets a full, honest stall window.

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

    ``release_title`` (issue #134) is refreshed the same way: a resurrected row
    otherwise keeps the PRIOR grab's release name, misleading the queue about
    which release is actually downloading now.

    ``timeout_at`` is reset alongside ``added_at`` for the same reason: a
    resurrected row gets a fresh, honest metadata-fetch deadline matching its
    reset stall-detection anchor (observability only — never read for control).
    """
    # #206: refuse to resurrect a terminal row whose torrent is being removed right
    # now (a concurrent cancel's post-commit delete, or a reconcile-driven removal).
    # Re-owning it would hand this request a torrent whose data is mid-deletion.
    # Checked here so BOTH reuse call sites (the get_by_hash precheck branch and the
    # post-add UNIQUE-conflict branch) are covered in one place; a synchronous read of
    # the in-process registry, no await between it and the CAS below.
    if queue_service.removal_in_flight(download_id):
        raise TorrentRemovalInFlightError(torrent_hash, download_id)
    now = datetime.now(UTC)
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
        release_title=release_title,
        added_at=now,
        timeout_at=now + METADATA_STALL_WINDOW,
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


async def _remove_torrent_if_added(
    qbt: DownloadClientPort,
    torrent_hash: str,
    *,
    actually_added: bool,
    request_id: int | None,
    reason: str,
) -> None:
    """Best-effort cleanup (WITH data) of the torrent THIS grab genuinely
    created, after the grab lost a race/CAS and nothing tracks it.

    Gated on ``actually_added`` (:class:`~plex_manager.ports.download_client.
    AddResult` ``created``): a torrent the client reported ALREADY PRESENT
    predates this grab and is NOT ours to destroy -- in the terminal-row-reuse
    path it is typically a still-seeding imported torrent whose data may back a
    live library file via hardlink, and the DB rollback preserved the old
    terminal row that tracks it. Removing it with ``delete_files=True`` would
    destroy content this grab never owned; it is left untouched, with the
    distinction logged. ``reason`` is a static caller description (log-injection
    convention, #35); request_id/torrent_hash go via ``extra=`` (they trace from
    the /queue/grab body, crossing the same log-safe barriers as every other
    request-derived value -- CodeQL taints extra= fields too).
    """
    log_extra = {
        "request_id": safe_int(request_id) if request_id is not None else None,
        "torrent_hash": safe_text(torrent_hash),
    }
    if not actually_added:
        _logger.info(
            "leaving a pre-existing torrent in place after %s: this grab did not "
            "create it (the client reported it already present), so it is not "
            "ours to remove",
            reason,
            extra=log_extra,
        )
        return
    try:
        await qbt.remove(torrent_hash, delete_files=True)
    except Exception:
        _logger.warning(
            "failed to remove orphaned torrent after %s; it may keep seeding "
            "until removed manually",
            reason,
            exc_info=True,
            extra=log_extra,
        )


def _planned_target_seasons(scored: ScoredRelease, season: int | None) -> tuple[int, ...]:
    if season is None:
        return ()
    return tuple(dict.fromkeys(scored.target_seasons or (season,)))


def _target_episodes(
    *,
    primary_season: int,
    primary_episodes: list[int] | None,
    target_season: int,
    scope_episodes_by_season: Mapping[int, Sequence[int] | None] | None,
) -> list[int] | None:
    if target_season == primary_season:
        return primary_episodes
    if scope_episodes_by_season is None or target_season not in scope_episodes_by_season:
        return None
    episodes = scope_episodes_by_season[target_season]
    return list(episodes) if episodes is not None else None


async def _active_conflict_for_targets(
    download_repo: SqlDownloadRepository,
    *,
    request_id: int | None,
    target_seasons: tuple[int | None, ...],
    torrent_hash: str,
) -> DownloadRecord | None:
    if request_id is None:
        return None
    for target_season in target_seasons:
        active = await download_repo.find_active_for_request(request_id, season=target_season)
        if active is not None and active.torrent_hash != torrent_hash:
            return active
    return None


async def _attach_target_scopes_to_existing_download(
    session: AsyncSession,
    download_repo: SqlDownloadRepository,
    existing: DownloadRecord,
    *,
    request_id: int | None,
    season: int | None,
    episodes: list[int] | None,
    scope_episodes_by_season: Mapping[int, Sequence[int] | None] | None,
    target_seasons: tuple[int, ...],
    observed_season_status: str | None,
    qbt: DownloadClientPort | None = None,
    actually_added: bool = False,
) -> DownloadRecord:
    """Attach logical TV scopes to an already-active physical torrent."""
    if request_id is None or season is None:
        return existing

    target_seasons = target_seasons or (season,)
    conflict = await _active_conflict_for_targets(
        download_repo,
        request_id=request_id,
        target_seasons=target_seasons,
        torrent_hash=existing.torrent_hash,
    )
    if conflict is not None:
        raise AlreadyDownloadingError(request_id)

    try:
        for target_season in target_seasons:
            target_episodes = _target_episodes(
                primary_season=season,
                primary_episodes=episodes,
                target_season=target_season,
                scope_episodes_by_season=scope_episodes_by_season,
            )
            await download_repo.ensure_scope(
                existing.id,
                media_request_id=request_id,
                season=target_season,
                episodes=target_episodes,
            )
            season_row = await SqlSeasonRequestRepository(session).ensure(
                request_id, target_season, status=RequestStatus.pending.value
            )
            allowed_from = (
                frozenset({observed_season_status})
                if target_season == season and observed_season_status is not None
                else _PACK_TARGET_SEASON_STATUS_VALUES
            )
            moved = await season_request_service.set_status_if_in(
                session,
                media_request_id=request_id,
                season_request_id=season_row.id,
                status=RequestStatus.downloading.value,
                allowed_from=allowed_from,
            )
            if not moved:
                await session.rollback()
                if qbt is not None:
                    await _remove_torrent_if_added(
                        qbt,
                        existing.torrent_hash,
                        actually_added=actually_added,
                        request_id=request_id,
                        reason="an attached TV scope moved on mid-grab",
                    )
                raise RequestNotActiveError(request_id)
    except IntegrityError:
        await session.rollback()
        conflict = await _active_conflict_for_targets(
            download_repo,
            request_id=request_id,
            target_seasons=target_seasons,
            torrent_hash=existing.torrent_hash,
        )
        if conflict is not None:
            if qbt is not None:
                await _remove_torrent_if_added(
                    qbt,
                    existing.torrent_hash,
                    actually_added=actually_added,
                    request_id=request_id,
                    reason="losing an active TV scope race",
                )
            raise AlreadyDownloadingError(request_id) from None
        record = await download_repo.get_by_hash(existing.torrent_hash)
        if record is not None:
            return record
        raise

    await session.commit()
    record = await download_repo.get_by_hash(existing.torrent_hash)
    if record is None:  # pragma: no cover - existing row was just read
        raise LookupError(f"download for hash {existing.torrent_hash} vanished mid-grab")
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
    episodes: list[int] | None = None,
    save_path: str = "",
    category: str = DEFAULT_CATEGORY,
    expected_season_status: str | None = None,
    scope_episodes_by_season: Mapping[int, Sequence[int] | None] | None = None,
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
    ``scope_episodes_by_season`` optionally carries the stored per-season episode
    intent for sibling scopes of a multi-season pack; the explicit ``episodes``
    argument remains authoritative for the primary ``season``.

    When ``request_id`` resolves to a real request, ``season``/``episodes`` are
    validated (and, for a movie, silently coerced) against the request's ACTUAL
    ``media_type`` rather than trusted at face value: a ``tv`` request with no
    ``season`` raises :class:`SeasonRequiredError` (a grab is always per-season,
    never a whole-series scan), and a non-``tv`` request has ``season``/
    ``episodes`` forced back to ``None`` regardless of what the caller passed, so
    a movie can never spawn a ``SeasonRequest`` row or scope its active-download
    guard to a fake season.

    ``expected_season_status`` lets the CALLER state its premise -- the
    decision's premise rides with the action, all the way to the write, exactly
    like the observed-status CAS this function already threads to the post-add
    move. Auto-grab selects a scope because it read the season as due
    (``pending``/``searching``/``no_acceptable_release``); if the eviction
    recovery FOLDS that season to ``available`` (its file never left disk)
    before this function's own fresh read, the fresh observation would read
    ``available`` and mistake it for an intentional reopen -- the round-5
    observed-status CAS cannot help because the observation itself post-dates
    the fold. When the fresh read differs from the caller's stated premise, the
    grab is refused UP FRONT (``RequestNotActiveError``, before anything reaches
    the client). ``None`` (the default) states no premise: the manual reopen
    flow keeps observing the live status -- its premise IS "whatever the season
    reads right now", and a decision made on ``available``/``completed``
    continues to reopen exactly as before.
    """
    download_repo = SqlDownloadRepository(session)
    candidate = scored.candidate

    source = candidate.magnet_url or candidate.download_url
    if source is None:
        raise NoGrabSourceError(candidate.guid)

    request_media_type: str | None = None
    # The statuses the GRAB DECISION observed, threaded into the post-add
    # status-move CAS (see _GRABBABLE_*_STATUS_VALUES): the move compares against
    # EXACTLY these, so any status a concurrent writer commits while qbt.add is
    # in flight -- a cancel, or the eviction recovery folding a re-armed
    # ``pending`` season back to ``available`` because its file never left disk
    # -- makes the move lose and the grab clean up, while a decision that itself
    # observed ``available`` (the deliberate reopen of a done season) still wins.
    observed_request_status: str | None = None
    observed_season_status: str | None = None
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
            observed_request_status = request.status
            request_media_type = request.media_type
            # Domain-boundary backstop: branch on the request's ACTUAL media
            # type, never on whether the caller merely passed a ``season``. The
            # endpoint already enforces this (422 before even previewing), but
            # this invariant must hold regardless of caller.
            if request.media_type == "tv":
                if season is None:
                    raise SeasonRequiredError(request_id)
                # Observe the SEASON's decision-time status too -- a plain READ,
                # never ``ensure()`` here: lazily creating the row would open the
                # write transaction before the ``qbt.add`` network call. A season
                # not yet tracked is observed as ``pending``, exactly what the
                # post-add ensure will create it as. A season already
                # ``cancelled`` is refused up front like a terminal request
                # (nothing handed to the client for content the user explicitly
                # stopped) -- the season-level mirror of the request gate above.
                season_rows = await SqlSeasonRequestRepository(session).list_for_request(request_id)
                season_row_now = next((s for s in season_rows if s.season_number == season), None)
                observed_season_status = (
                    season_row_now.status
                    if season_row_now is not None
                    else RequestStatus.pending.value
                )
                if observed_season_status == RequestStatus.cancelled.value:
                    raise RequestNotActiveError(request_id)
                if observed_season_status == RequestStatus.waiting_for_air_date.value:
                    raise RequestNotActiveError(request_id)
                if (
                    expected_season_status is not None
                    and observed_season_status != expected_season_status
                ):
                    # The caller's premise no longer holds (e.g. auto-grab
                    # selected this scope as due, and the eviction recovery
                    # folded the season back to 'available' before we read it).
                    # Refuse BEFORE anything reaches the client -- grabbing
                    # would download a duplicate of on-disk content the caller
                    # never decided to re-fetch.
                    raise RequestNotActiveError(request_id)
            else:
                # Non-tv (movie): season/episodes are meaningless -- coerce
                # rather than trust the caller, so a movie can never spawn a
                # SeasonRequest row or have its one-active guard scoped to a
                # fake season.
                season = None
                episodes = None

    target_seasons = _planned_target_seasons(scored, season)
    active_guard_seasons: tuple[int | None, ...] = target_seasons or (season,)

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
            # season) attaches a logical scope instead of pretending the existing
            # scalar claim covers it.
            if request_id is not None and season is not None:
                return await _attach_target_scopes_to_existing_download(
                    session,
                    download_repo,
                    pre,
                    request_id=request_id,
                    season=season,
                    episodes=episodes,
                    scope_episodes_by_season=scope_episodes_by_season,
                    target_seasons=target_seasons,
                    observed_season_status=observed_season_status,
                )
            return pre

    # Parallel-grab guard: if this request already has an active (non-terminal)
    # download for a DIFFERENT release, refuse rather than create a second active
    # row. The known-hash precheck above already returned for the SAME release, so
    # an active download whose hash differs is a genuine second grab. Checked
    # BEFORE handing the torrent to the client, so nothing is added on rejection.
    #
    # Gated on ``known_hash is not None``: when the indexer omitted info_hash we
    # cannot yet tell whether ``active`` IS this same release (its real hash is
    # only known after ``qbt.add`` returns) or genuinely a different one -- and
    # ``active.torrent_hash != known_hash`` (``None``) is trivially true for EVERY
    # active row, so a hashless candidate would always fail this guard and 409 a
    # legitimate same-release re-grab (a UI double-click retry) before the client
    # even had a chance to resolve the hash and let the post-add reconciliation
    # below (``existing = get_by_hash(torrent_hash)``) recognize it as a no-op.
    # Deferring here is safe: a genuinely different release still active for this
    # request is caught just as honestly after the add, via the
    # ``uq_downloads_active_request`` partial unique index raising IntegrityError
    # (handled below), which resolves to the same ``AlreadyDownloadingError``.
    if request_id is not None and known_hash is not None:
        active = await _active_conflict_for_targets(
            download_repo,
            request_id=request_id,
            target_seasons=active_guard_seasons,
            torrent_hash=known_hash,
        )
        if active is not None:
            raise AlreadyDownloadingError(request_id)

    add_result = await qbt.add(source, save_path, category)
    torrent_hash = add_result.torrent_hash.lower() or (known_hash or "")
    # Whether THIS call genuinely created the torrent. False = the client
    # reported it already present (qBittorrent's 409) and resolved to the
    # pre-existing one -- which the lost-grab cleanups below must then never
    # remove: it predates this grab (e.g. a still-seeding import whose data may
    # back a live library file via hardlink), and the DB rollback preserves
    # whatever row tracked it. See _remove_torrent_if_added.
    actually_added = add_result.created
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
        if request_id is not None and season is not None:
            return await _attach_target_scopes_to_existing_download(
                session,
                download_repo,
                existing,
                request_id=request_id,
                season=season,
                episodes=episodes,
                scope_episodes_by_season=scope_episodes_by_season,
                target_seasons=target_seasons,
                observed_season_status=observed_season_status,
                qbt=qbt,
                actually_added=actually_added,
            )
        return existing

    if request_id is not None:
        active = await _active_conflict_for_targets(
            download_repo,
            request_id=request_id,
            target_seasons=active_guard_seasons,
            torrent_hash=torrent_hash,
        )
        if active is not None:
            await _remove_torrent_if_added(
                qbt,
                torrent_hash,
                actually_added=actually_added,
                request_id=request_id,
                reason="losing a parallel grab for a planned TV scope",
            )
            raise AlreadyDownloadingError(request_id)

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
                release_title=candidate.title,
            )
            if not claimed_reuse:
                if record.status not in _TERMINAL_STATUS_VALUES:
                    if request_id is not None and record.media_request_id != request_id:
                        raise TorrentAlreadyTrackedError(torrent_hash, record.media_request_id)
                    # The reuse race was lost to THIS request's OTHER grab (two
                    # seasons of one multi-season pack racing to resurrect the same
                    # terminal row). The winner's row carries the winner's scope, so
                    # returning it for a NON-covered scope would report this grab as
                    # success while the requested season/episodes stay silently
                    # untracked -- mirror the non-race active paths by attaching the
                    # requested logical scopes to the physical row.
                    if request_id is not None and season is not None:
                        return await _attach_target_scopes_to_existing_download(
                            session,
                            download_repo,
                            record,
                            request_id=request_id,
                            season=season,
                            episodes=episodes,
                            scope_episodes_by_season=scope_episodes_by_season,
                            target_seasons=target_seasons,
                            observed_season_status=observed_season_status,
                            qbt=qbt,
                            actually_added=actually_added,
                        )
                    return record
                raise TorrentAlreadyTrackedError(torrent_hash, record.media_request_id)
        except IntegrityError:
            await session.rollback()
            if request_id is not None:
                active = await _active_conflict_for_targets(
                    download_repo,
                    request_id=request_id,
                    target_seasons=active_guard_seasons,
                    torrent_hash=torrent_hash,
                )
                if active is not None:
                    await _remove_torrent_if_added(
                        qbt,
                        torrent_hash,
                        actually_added=actually_added,
                        request_id=request_id,
                        reason="losing a terminal-row reuse race",
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
                release_title=candidate.title,
                timeout_at=datetime.now(UTC) + METADATA_STALL_WINDOW,
            )
        except IntegrityError:
            # A concurrent grab won the race. It either grabbed the SAME release
            # (``torrent_hash`` UNIQUE) or a DIFFERENT release for this request
            # (``uq_downloads_active_request`` — the DB backstop to the TOCTOU
            # guard above). Roll back and distinguish, so neither becomes an opaque
            # 500: a different-release conflict is the honest ``already_downloading``.
            await session.rollback()
            if request_id is not None:
                active = await _active_conflict_for_targets(
                    download_repo,
                    request_id=request_id,
                    target_seasons=active_guard_seasons,
                    torrent_hash=torrent_hash,
                )
                if active is not None:
                    # The other release won the request's single active slot. A
                    # torrent this grab genuinely created is now orphaned --
                    # nothing tracks it, so it would seed forever consuming
                    # bandwidth. Best-effort remove it (deleting its files)
                    # before refusing the parallel grab -- but ONLY one this
                    # call actually added (see _remove_torrent_if_added).
                    await _remove_torrent_if_added(
                        qbt,
                        torrent_hash,
                        actually_added=actually_added,
                        request_id=request_id,
                        reason="losing a parallel grab for this request",
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
                if request_id is not None and season is not None:
                    return await _attach_target_scopes_to_existing_download(
                        session,
                        download_repo,
                        winner,
                        request_id=request_id,
                        season=season,
                        episodes=episodes,
                        scope_episodes_by_season=scope_episodes_by_season,
                        target_seasons=target_seasons,
                        observed_season_status=observed_season_status,
                        qbt=qbt,
                        actually_added=actually_added,
                    )
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
                release_title=candidate.title,
            )
            if not claimed_reuse:
                if record.status not in _TERMINAL_STATUS_VALUES:
                    if request_id is not None and record.media_request_id != request_id:
                        raise TorrentAlreadyTrackedError(
                            torrent_hash, record.media_request_id
                        ) from None
                    # Mirror of the non-race branch above: a reuse race lost to THIS
                    # request's other grab must still refuse a non-covered scope
                    # instead of returning the winner's row as success (the requested
                    # season/episodes would be silently untracked).
                    if request_id is not None and season is not None:
                        return await _attach_target_scopes_to_existing_download(
                            session,
                            download_repo,
                            record,
                            request_id=request_id,
                            season=season,
                            episodes=episodes,
                            scope_episodes_by_season=scope_episodes_by_season,
                            target_seasons=target_seasons,
                            observed_season_status=observed_season_status,
                            qbt=qbt,
                            actually_added=actually_added,
                        )
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
        #
        # POST-ADD CAS: compare against EXACTLY the status the grab decision
        # observed (see _GRABBABLE_*_STATUS_VALUES' comment). Any status a
        # concurrent writer committed while qbt.add was in flight -- a cancel,
        # or the eviction recovery's fold of a re-armed 'pending' season back to
        # 'available' (its file never left disk; grabbing it now would download
        # a duplicate) -- makes this move match zero rows, while a decision that
        # itself observed 'available' (the deliberate reopen of a done season)
        # compares 'available' and still wins.
        if season is not None:
            # TV: the request's status is a COMPUTED rollup of its seasons, never a
            # direct target -- resolve the season row (created lazily here, like
            # the Download row) and CAS it to 'downloading';
            # season_request_service recomputes the parent's rollup in the same
            # transaction ONLY when the swap actually won.
            season_row = await SqlSeasonRequestRepository(session).ensure(
                request_id, season, status=RequestStatus.pending.value
            )
            moved = await season_request_service.set_status_if_in(
                session,
                media_request_id=request_id,
                season_request_id=season_row.id,
                status=RequestStatus.downloading.value,
                allowed_from=(
                    frozenset({observed_season_status})
                    if observed_season_status is not None
                    else _GRABBABLE_SEASON_STATUS_VALUES
                ),
            )
            if moved:
                try:
                    for target_season in target_seasons:
                        if target_season == season:
                            continue
                        await download_repo.ensure_scope(
                            record.id,
                            media_request_id=request_id,
                            season=target_season,
                            episodes=_target_episodes(
                                primary_season=season,
                                primary_episodes=episodes,
                                target_season=target_season,
                                scope_episodes_by_season=scope_episodes_by_season,
                            ),
                        )
                        target_row = await SqlSeasonRequestRepository(session).ensure(
                            request_id, target_season, status=RequestStatus.pending.value
                        )
                        target_moved = await season_request_service.set_status_if_in(
                            session,
                            media_request_id=request_id,
                            season_request_id=target_row.id,
                            status=RequestStatus.downloading.value,
                            allowed_from=_PACK_TARGET_SEASON_STATUS_VALUES,
                        )
                        if not target_moved:
                            await session.rollback()
                            await _remove_torrent_if_added(
                                qbt,
                                torrent_hash,
                                actually_added=actually_added,
                                request_id=request_id,
                                reason="a planned TV scope moved on mid-grab",
                            )
                            raise RequestNotActiveError(request_id)
                except IntegrityError:
                    await session.rollback()
                    active = await _active_conflict_for_targets(
                        download_repo,
                        request_id=request_id,
                        target_seasons=target_seasons,
                        torrent_hash=torrent_hash,
                    )
                    if active is not None:
                        await _remove_torrent_if_added(
                            qbt,
                            torrent_hash,
                            actually_added=actually_added,
                            request_id=request_id,
                            reason="losing an active TV scope race",
                        )
                        raise AlreadyDownloadingError(request_id) from None
                    raise
        else:
            moved = await SqlRequestRepository(session).set_status_if_in(
                request_id,
                RequestStatus.downloading.value,
                (
                    frozenset({observed_request_status})
                    if observed_request_status is not None
                    else _GRABBABLE_REQUEST_STATUS_VALUES
                ),
            )
        if not moved:
            # The request/season moved off its decision-time status while
            # qbt.add was in flight (cancelled, folded back to 'available' by
            # the eviction recovery, or otherwise advanced). The torrent is
            # already in the client and the Download row + 'grabbed' history are
            # pending in this transaction -- discard them, then best-effort
            # remove the just-added torrent WITH data (the same orphan cleanup
            # the lost-parallel-grab branches above perform; leaving it would
            # seed forever for content nobody wants), and refuse honestly. Every
            # caller already handles RequestNotActiveError: the endpoint as 409,
            # auto-grab by leaving the scope as-is, report-issue via its
            # _GRAB_ERRORS park (whose never-un-terminate guard skips a
            # cancelled/available row).
            await session.rollback()
            await _remove_torrent_if_added(
                qbt,
                torrent_hash,
                actually_added=actually_added,
                request_id=request_id,
                reason="the request was cancelled or moved on mid-grab",
            )
            raise RequestNotActiveError(request_id)
    await session.commit()
    refreshed = await download_repo.get_by_hash(torrent_hash)
    return refreshed if refreshed is not None else record
