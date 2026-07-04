"""Correction verbs (ADR-0014): report-issue and cancel.

Two operator corrections for the two lifecycle stages that come AFTER a grab,
each self-healing so no human ticket is ever needed (north-star #1: a button, not
a terminal):

* :func:`report_issue` — "this imported file is bad, redo it." Composes existing
  primitives IN ORDER: (a) blocklist the culprit release (resolved from the
  imported download's history), (b) remove its torrent WITH data, (c) purge the
  library file via the shared root-guarded purge primitive, (d) trigger a Plex
  scan, (e) re-arm the request/season to ``searching`` and clear the purge
  breadcrumbs, (f) write an audit history row, (g) synchronously run the SAME
  decision-engine -> grab path the grab endpoint uses, so the re-search happens
  inline -- the blocklist now excludes the bad release, guaranteeing a DIFFERENT
  one is grabbed (or the honest ``no_acceptable_release`` park if nothing is
  acceptable). The synchronous re-grab IS the auto re-search AND the undo (the
  content comes back), which is why no recycle bin is needed for the beta.

  Hardlink caveat (ADR-0014): a same-filesystem import hardlinks the library file
  to the download client's seed copy, so purging the library file ALONE frees
  nothing -- BOTH the torrent-with-data (b) AND the library file (c) must go.

  Foot-gun failsafe (mirrors Radarr's MediaFileDeletionService): before touching
  anything, verify the media root is mounted and non-empty. An unmounted drive
  would make ``fs.delete`` a no-op on a not-really-gone file, and we would have
  blocklisted the good release + re-grabbed a duplicate against content that is
  still there once the drive comes back.

* :func:`cancel_request` — "I don't want this anymore", the honest opposite of
  report-issue: for a NOT-yet-imported request, remove any active torrent(s) WITH
  data and settle the request (and every tracked season) to the terminal
  ``cancelled`` status. The row is kept for history; nothing is re-grabbed.

Auto-grab interplay (ADR-0013): both ``reset_for_research`` variants reset the
per-scope search backoff (``search_attempts`` / ``next_search_at``) -- a
report-issue re-search must not inherit the failed culprit's accrued backoff,
and a re-armed ``searching`` scope is picked up eagerly by the worker.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from plex_manager.adapters.prowlarr.adapter import IndexerError
from plex_manager.domain.state_machine import DownloadState
from plex_manager.logsafe import safe_int, safe_text
from plex_manager.models import (
    BlocklistReason,
    DownloadHistory,
    DownloadHistoryEvent,
    RequestStatus,
)
from plex_manager.repositories.blocklist import SqlBlocklistRepository
from plex_manager.repositories.downloads import SqlDownloadRepository
from plex_manager.repositories.requests import SqlRequestRepository
from plex_manager.repositories.season_requests import SqlSeasonRequestRepository
from plex_manager.services import (
    blocklist_service,
    decision_service,
    grab_service,
    purge_service,
    request_service,
    season_request_service,
)
from plex_manager.services.purge_service import PurgeOutcome

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from plex_manager.domain.quality_profile import QualityProfile
    from plex_manager.ports.download_client import DownloadClientPort
    from plex_manager.ports.filesystem import FileSystemPort
    from plex_manager.ports.indexer import IndexerPort
    from plex_manager.ports.library import LibraryPort
    from plex_manager.ports.parser import ParserPort
    from plex_manager.ports.repositories import RequestRecord, SeasonRequestRecord

__all__ = [
    "CANCELLABLE_REQUEST_STATUS_VALUES",
    "REPORTABLE_STATUS_VALUES",
    "ActiveDuplicateError",
    "ImportInProgressError",
    "MediaRootUnavailableError",
    "NotCancellableError",
    "NotReportableError",
    "ReportSeasonRequiredError",
    "RequestNotFoundError",
    "SeasonNotFoundError",
    "cancel_request",
    "report_issue",
]

_logger = logging.getLogger(__name__)

# The imported/available states a report-issue may act on: the file is on disk
# (``completed`` = imported, Plex-confirmation pending; ``available`` = confirmed).
# For TV this is checked against the SEASON's own status (never the rollup, which
# is where ``partially_available`` lives). ``import_blocked`` is handled by the
# queue's mark-failed instead (its download is still an active queue row).
REPORTABLE_STATUS_VALUES: Final[frozenset[str]] = frozenset(
    {RequestStatus.completed.value, RequestStatus.available.value}
)

# The not-yet-imported states a cancel may act on. A request past these (imported/
# available/settled) is NOT cancellable -- report-issue (redo) or keep-forever/
# eviction own that stage.
#
# For TV this parent-rollup guard is NECESSARY but NOT sufficient: ``season_rollup``
# precedence lets an in-flight season (downloading/searching/no_acceptable_release)
# outrank an already-DONE sibling, so the parent can read one of these cancellable
# statuses while a season is actually ``available``/``completed`` (or ``evicted``).
# ``cancel_request`` therefore gates TV cancellability PER-SEASON on top of this --
# see ``_UNCANCELLABLE_SEASON_STATUS_VALUES``.
CANCELLABLE_REQUEST_STATUS_VALUES: Final[frozenset[str]] = frozenset(
    {
        RequestStatus.pending.value,
        RequestStatus.searching.value,
        RequestStatus.no_acceptable_release.value,
        RequestStatus.downloading.value,
    }
)

# Per-season states that make a TV request UN-cancellable regardless of what the
# parent rollup reads. An ``available``/``completed`` season is already DONE
# (imported, file on disk, imported download row whose torrent is still seeding).
# Cancel excludes terminal (imported) download rows from its active-torrent sweep
# (``list_active_for_request``) and eviction ignores ``cancelled``, so settling such
# a season ``cancelled`` would BOTH lie (content still in Plex/on disk) and orphan
# the seeding torrent + leave the file unreclaimable except from a terminal. Refuse
# instead. (An ``evicted`` season is caught separately by the imported-download
# probe in ``cancel_request`` -- its file is gone but its imported torrent may still
# seed, the same orphan risk.)
_UNCANCELLABLE_SEASON_STATUS_VALUES: Final[frozenset[str]] = frozenset(
    {
        RequestStatus.available.value,
        RequestStatus.completed.value,
    }
)

# The grab-path exceptions the inline re-grab may raise (all defined in
# ``grab_service``). A report-issue that has ALREADY purged + blocklisted must not
# then 500/409 on a grab hiccup: it lands on the honest, retryable
# ``no_acceptable_release`` park instead, exactly like the grab endpoint's own
# empty-preview branch -- the request is left visible and re-searchable.
_GRAB_ERRORS: Final = (
    grab_service.NoGrabSourceError,
    grab_service.GrabError,
    grab_service.AlreadyDownloadingError,
    grab_service.DownloadScopeConflictError,
    grab_service.RequestNotActiveError,
    grab_service.SeasonRequiredError,
)

# The indexer failures the inline RE-SEARCH (``decision_service.preview`` ->
# ``prowlarr.search``) may raise. Like ``_GRAB_ERRORS`` for the grab step, a
# report-issue that has ALREADY blocklisted + purged must not then propagate a
# Prowlarr transport/rate-limit/HTTP failure out as a 5xx that leaves the row lying
# as ``searching`` with nothing in flight: it lands on the honest, retryable
# ``no_acceptable_release`` park instead. ``IndexerRateLimitError`` is a subclass of
# ``IndexerError`` and so is covered.
_INDEXER_ERRORS: Final = (IndexerError,)

# The ACTIVE download states a cancel may fail out from under -- every non-terminal
# state EXCEPT ``importing``. An ``importing`` row is mid-copy/scan: failing it would
# race the importer's finalize compare-and-swap and could strand a placed file in the
# library under a ``cancelled`` request (see ``ImportInProgressError``). Cancel's
# per-row transition is a compare-and-swap gated on this set, so a row that raced INTO
# ``importing`` since the active snapshot fails the swap and aborts the cancel rather
# than clobbering the importer.
_CANCELLABLE_DOWNLOAD_STATE_VALUES: Final[frozenset[str]] = frozenset(
    {
        DownloadState.Searching.value,
        DownloadState.Downloading.value,
        DownloadState.MetadataFetching.value,
        DownloadState.ImportPending.value,
        DownloadState.ImportBlocked.value,
        DownloadState.FailedPending.value,
        DownloadState.ClientMissing.value,
    }
)


class RequestNotFoundError(Exception):
    """No request with this id (HTTP 404)."""

    def __init__(self, request_id: int) -> None:
        self.request_id = request_id
        super().__init__(f"request {request_id} does not exist")


class ReportSeasonRequiredError(Exception):
    """A TV report-issue was called with no ``season`` (HTTP 422)."""

    def __init__(self, request_id: int) -> None:
        self.request_id = request_id
        super().__init__(f"request {request_id} is tv and requires a season to report")


class SeasonNotFoundError(Exception):
    """The named season is not tracked on this TV request (HTTP 404)."""

    def __init__(self, request_id: int, season: int) -> None:
        self.request_id = request_id
        self.season = season
        super().__init__(f"request {request_id} does not track season {season}")


class NotReportableError(Exception):
    """The target is not in an imported/available state (HTTP 409)."""

    def __init__(self, request_id: int, status: str) -> None:
        self.request_id = request_id
        self.status = status
        super().__init__(f"request {request_id} is {status!r}, not a reportable state")


class NotCancellableError(Exception):
    """The request is past the not-yet-imported stage (HTTP 409)."""

    def __init__(self, request_id: int, status: str) -> None:
        self.request_id = request_id
        self.status = status
        super().__init__(f"request {request_id} is {status!r}, not a cancellable state")


class MediaRootUnavailableError(Exception):
    """The media root is missing/empty -- refuse to purge (HTTP 409).

    The Radarr-style failsafe: an unmounted drive must never let a report-issue
    blocklist the good release and re-grab a duplicate against content that is
    still really there (``fs.delete`` would silently no-op on the not-present path).
    """

    def __init__(self, request_id: int, root_path: str | None) -> None:
        self.request_id = request_id
        self.root_path = root_path
        super().__init__(f"media root for request {request_id} is unavailable (unmounted/empty)")


class ActiveDuplicateError(Exception):
    """A newer active request for the same media already exists (HTTP 409).

    Report-issue re-arms the reported (SETTLED) request/season to an ACTIVE status
    (``searching`` for a movie, or a partially_available/searching rollup for a tv
    season). If a DIFFERENT active request already occupies this media's
    ``uq_media_requests_active`` slot -- which the partial unique index legitimately
    allows (an older settled ``available`` request can coexist with a newer active
    one for a later season; see ``request_service.set_keep_forever``) -- that re-arm
    would collide on the index, and only AFTER the irreversible blocklist / torrent
    removal / file purge had already run, rolling the DB back while the media is gone
    (a half-corrected state). Refuse UP FRONT, before touching anything: the operator
    acts on the live active request instead.
    """

    def __init__(self, request_id: int, active_request_id: int) -> None:
        self.request_id = request_id
        self.active_request_id = active_request_id
        super().__init__(
            f"request {request_id} has a newer active sibling {active_request_id} "
            f"for the same media; report-issue would collide re-arming it"
        )


class ImportInProgressError(Exception):
    """A download for this request is finalizing its import (HTTP 409, retryable).

    Cancel must never fail an ``importing`` row: the importer may already have placed
    the library file and be mid-scan/finalize. If cancel flips the row to ``failed``,
    the importer's finalize compare-and-swap (``Importing -> Imported``) loses -- and
    it then deliberately leaves the placed file in the library -- so the request would
    settle ``cancelled`` with the media still on disk / in Plex (a dishonest, orphaned
    state). Refuse instead; the operator retries once the import lands (as
    ``completed``/``import_blocked``), where report-issue takes over the redo.
    """

    def __init__(self, request_id: int) -> None:
        self.request_id = request_id
        super().__init__(
            f"request {request_id} has a download finalizing its import; retry shortly"
        )


def _root_is_mounted(root_path: str | None) -> bool:
    """Whether ``root_path`` is a present, non-empty directory (an active mount).

    Synchronous disk I/O (``os.path.isdir`` + ``os.scandir``), so callers offload
    it via ``asyncio.to_thread``. An empty directory reads as "not mounted": a
    freshly-unmounted mountpoint is typically an empty stub dir, and there is
    nothing to have imported into an empty root anyway.
    """
    if not root_path or not os.path.isdir(root_path):
        return False
    try:
        with os.scandir(root_path) as it:
            return any(True for _ in it)
    except OSError:
        return False


@dataclass(frozen=True)
class _ReportTarget:
    """The resolved report-issue target: which season (``None`` for a movie), its
    current status, and its stored purge breadcrumb."""

    season: int | None
    status: str
    library_path: str | None


async def _resolve_report_target(
    session: AsyncSession, request: RequestRecord, season: int | None
) -> _ReportTarget:
    """Resolve the reportable target's status + library_path (movie vs one season)."""
    if request.media_type != "tv":
        return _ReportTarget(None, request.status, request.library_path)
    if season is None:
        raise ReportSeasonRequiredError(request.id)
    seasons = await SqlSeasonRequestRepository(session).list_for_request(request.id)
    srec = next((s for s in seasons if s.season_number == season), None)
    if srec is None:
        raise SeasonNotFoundError(request.id, season)
    return _ReportTarget(season, srec.status, srec.library_path)


async def report_issue(
    session: AsyncSession,
    qbt: DownloadClientPort,
    fs: FileSystemPort,
    library: LibraryPort,
    prowlarr: IndexerPort,
    parser: ParserPort,
    profile: QualityProfile,
    *,
    request_id: int,
    reason: str,
    season: int | None,
    root_path: str | None,
) -> RequestRecord:
    """Report a bad imported file: blocklist + purge (torrent + library) + re-search.

    Returns the updated request record (re-read after the inline re-grab, so its
    status reflects ``downloading`` on a successful replacement grab, or
    ``no_acceptable_release`` / the season rollup when nothing acceptable was
    found). See the module docstring for the full ordered flow and the caveats.
    """
    request_repo = SqlRequestRepository(session)
    request = await request_repo.get(request_id)
    if request is None:
        raise RequestNotFoundError(request_id)

    target = await _resolve_report_target(session, request, season)
    if target.status not in REPORTABLE_STATUS_VALUES:
        raise NotReportableError(request_id, target.status)

    # Active-duplicate failsafe: refuse BEFORE any irreversible side effect if a
    # DIFFERENT active request already owns this media's uq_media_requests_active slot.
    # Re-arming THIS (settled) row to an active status would collide on that partial
    # unique index -- but only AFTER the blocklist/torrent-remove/file-purge below had
    # already run, rolling the DB back while the media is gone (see ActiveDuplicateError).
    # ``find_active`` returns THIS request when it is itself active (``completed`` holds
    # the slot uniquely, so no sibling can exist); it returns a DIFFERENT row only when
    # this request is settled (``available``) and a newer active one coexists.
    active_sibling = await request_repo.find_active(request.tmdb_id, request.media_type)
    if active_sibling is not None and active_sibling.id != request_id:
        raise ActiveDuplicateError(request_id, active_sibling.id)

    # Foot-gun failsafe: refuse if the media root is unmounted/empty (see
    # MediaRootUnavailableError). Checked BEFORE any blocklist/remove/flip so a
    # missing drive aborts the whole verb rather than firing against content that
    # is not really gone.
    if not await asyncio.to_thread(_root_is_mounted, root_path):
        raise MediaRootUnavailableError(request_id, root_path)

    is_tv = target.season is not None
    media_type = "tv" if is_tv else "movie"
    season_note = f" season {target.season}" if target.season is not None else ""
    log_extra: dict[str, object] = {"request_id": safe_int(request_id), "tmdb_id": request.tmdb_id}

    # Resolve the culprit release from the IMPORTED download for (request, season) --
    # the row that actually placed the file being reported (and whose torrent still
    # hardlink-seeds it), never merely the newest attempt: a season already available
    # can carry a NEWER supplementary/failed row over the older imported one, and
    # blocklisting/removing that would leave the real seed untouched so the purge frees
    # nothing (ADR-0014). ``None`` when the title was recorded available straight from
    # Plex (no download of ours) -- the blocklist/remove steps below are then skipped.
    download_repo = SqlDownloadRepository(session)
    culprit = await download_repo.find_latest_imported_for_request(request_id, season=target.season)

    # (a) blocklist the culprit release (nothing to blocklist if the title was
    # recorded available straight from Plex, with no download of ours).
    if culprit is not None:
        source_title = (
            await blocklist_service.source_title_for(session, culprit.torrent_hash)
            or culprit.torrent_hash
        )
        indexer = await blocklist_service.indexer_for(session, culprit.torrent_hash)
        await SqlBlocklistRepository(session).create(
            source_title=source_title,
            reason=BlocklistReason(reason).value,
            tmdb_id=request.tmdb_id,
            torrent_hash=culprit.torrent_hash,
            indexer=indexer,
            media_type=media_type,
        )

        # (b) remove the torrent WITH data (best-effort) -- the hardlink caveat means
        # this must go too, not just the library file.
        await purge_service.remove_torrent(
            qbt,
            culprit.torrent_hash,
            context="a report-issue",
            extra={"torrent_hash": culprit.torrent_hash, **log_extra},
        )

    # (c) purge the library file via the shared root-guarded primitive. ``purge_ok``
    # tracks whether the file was ACTUALLY removed: only then is the ``library_path``
    # breadcrumb cleared at (e). On ``error`` (a genuine delete failure -- permissions,
    # transient I/O, a partial rmtree) or ``refused`` (out-of-root breadcrumb) the file
    # may still be on disk, so the breadcrumb is PRESERVED -- it is the only handle a
    # later retry / eviction has to reclaim the orphan; losing it would strand the bad
    # file with no way to purge it (honesty over silence).
    purge_ok = True
    if target.library_path is not None:
        purge = await purge_service.purge_library_path(fs, target.library_path)
        if purge.outcome is PurgeOutcome.refused:
            purge_ok = False
            _logger.warning(
                "report-issue purge of %r refused by the filesystem guard (%s); "
                "re-searching anyway but keeping the breadcrumb (a stale/misconfigured path)",
                safe_text(request.title),
                purge.detail,
                extra=log_extra,
            )
        elif purge.outcome is PurgeOutcome.error:
            purge_ok = False
            _logger.warning(
                "report-issue purge of %r failed (%s); re-searching anyway but keeping "
                "the breadcrumb so the orphaned file stays reclaimable",
                safe_text(request.title),
                purge.detail,
                extra=log_extra,
            )
    else:
        # No breadcrumb (a title recorded available straight from Plex, or one
        # predating the library_path column): nothing of ours to delete -- honest,
        # never a guessed path, and the re-search below still runs.
        _logger.warning(
            "report-issue: no stored library_path for %r; nothing to purge",
            safe_text(request.title),
            extra=log_extra,
        )

    # (d) trigger a Plex scan so the removed item drops out of the library.
    if target.library_path is not None:
        await purge_service.trigger_library_scan(
            library,
            library_path=target.library_path,
            media_type=media_type,
            context="report-issue",
            extra=log_extra,
        )

    # (e) re-arm the request/season to 'searching'. The purge breadcrumb is cleared
    # ONLY when the file was actually removed (``purge_ok``); a failed/refused purge
    # keeps it so the orphan stays reclaimable (see (c)).
    if is_tv and target.season is not None:
        await season_request_service.reset_for_research(
            session,
            media_request_id=request_id,
            season_number=target.season,
            clear_library_path=purge_ok,
        )
    else:
        await request_repo.reset_for_research(request_id, clear_library_path=purge_ok)

    # (f) audit history row.
    session.add(
        DownloadHistory(
            tmdb_id=request.tmdb_id,
            torrent_hash=culprit.torrent_hash if culprit is not None else None,
            event_type=DownloadHistoryEvent.reported,
            source_title=request.title,
            message=(
                f"reported ({reason}){season_note}: blocklisted the release, "
                f"purged the file, re-searching"
            ),
        )
    )
    await session.commit()

    # (g) synchronous re-search: the SAME decision-engine -> grab path the grab
    # endpoint uses. The blocklist row above now excludes the culprit, so a
    # different release is grabbed; nothing acceptable lands on the honest
    # no_acceptable_release park. (Auto-grab cross-branch: reset backoff here once
    # feat/auto-grab merges -- see the module docstring.)
    #
    # ALWAYS whole-scope (``episodes=None``): the purge removed the entire library
    # target -- for tv that is the whole SEASON directory (``library_path`` is the
    # season dir, never a single episode), so re-fetching only the culprit's episode
    # subset would leave the season with the OTHER (also-deleted) episodes missing while
    # marking it done. A season-directory purge must drive a season-level re-search.
    try:
        result = await decision_service.preview(
            prowlarr,
            parser,
            profile,
            SqlBlocklistRepository(session),
            tmdb_id=request.tmdb_id,
            title=request.title,
            media_type=request.media_type,
            year=request.year,
            season=target.season,
            episodes=None,
        )
    except _INDEXER_ERRORS as exc:
        # The re-search could not reach the indexer AFTER the blocklist/purge/reset
        # already committed. Park honestly (retryable) rather than propagate a 5xx that
        # leaves the row lying as 'searching' with nothing actually in flight -- exactly
        # the posture the empty-preview / grab-failure branches take.
        _logger.warning(
            "report-issue re-search for %r failed to reach the indexer (%s); parking as "
            "no_acceptable_release (retryable)",
            safe_text(request.title),
            type(exc).__name__,
            extra=log_extra,
        )
        await _park_no_acceptable(session, request_id, target.season, is_tv=is_tv)
    else:
        if not result.accepted:
            await _park_no_acceptable(session, request_id, target.season, is_tv=is_tv)
        else:
            try:
                await grab_service.grab(
                    qbt,
                    session,
                    scored=result.accepted[0],
                    request_id=request_id,
                    tmdb_id=request.tmdb_id,
                    year=request.year,
                    season=target.season,
                    episodes=None,
                )
            except _GRAB_ERRORS as exc:
                _logger.warning(
                    "report-issue re-grab for %r failed (%s); parking as "
                    "no_acceptable_release (retryable)",
                    safe_text(request.title),
                    type(exc).__name__,
                    extra=log_extra,
                )
                await _park_no_acceptable(session, request_id, target.season, is_tv=is_tv)

    updated = await request_repo.get(request_id)
    if updated is None:  # pragma: no cover - just operated on this row
        raise RequestNotFoundError(request_id)
    return updated


async def _park_no_acceptable(
    session: AsyncSession, request_id: int, season: int | None, *, is_tv: bool
) -> None:
    """Land the request/season on the honest ``no_acceptable_release`` dead-end."""
    if is_tv and season is not None:
        await season_request_service.mark_no_acceptable_release(
            session, media_request_id=request_id, season_number=season
        )
        await session.commit()
    else:
        await request_service.mark_no_acceptable_release(session, request_id)


async def cancel_request(
    session: AsyncSession,
    qbt: DownloadClientPort,
    *,
    request_id: int,
) -> RequestRecord:
    """Cancel a not-yet-imported request: drop active torrent(s) + settle ``cancelled``.

    Removes every active torrent this request still owns WITH its data (best-effort,
    closing the seeding leak), marks each of those download rows terminal, and flips
    the request -- and, for TV, every tracked season -- to the settled ``cancelled``
    status (kept only for history; nothing re-grabbed). Returns the updated record.
    """
    request_repo = SqlRequestRepository(session)
    request = await request_repo.get(request_id)
    if request is None:
        raise RequestNotFoundError(request_id)
    if request.status not in CANCELLABLE_REQUEST_STATUS_VALUES:
        raise NotCancellableError(request_id, request.status)

    download_repo = SqlDownloadRepository(session)

    seasons: list[SeasonRequestRecord] = []
    if request.media_type == "tv":
        # The parent-rollup guard above is necessary but NOT sufficient for TV:
        # season_rollup precedence lets an in-flight season outrank an already-DONE
        # sibling, so the parent can read a cancellable status while a season is
        # actually done. Gate per-season BEFORE mutating anything -- refusing here
        # leaves the done season's file/torrent untouched rather than orphaning them
        # (see _UNCANCELLABLE_SEASON_STATUS_VALUES). No partial cancel is performed:
        # the whole request is refused so no in-flight sibling is half-cancelled.
        seasons = await SqlSeasonRequestRepository(session).list_for_request(request_id)
        for srec in seasons:
            if srec.status in _UNCANCELLABLE_SEASON_STATUS_VALUES:
                raise NotCancellableError(request_id, request.status)
            # Belt-and-suspenders: a season whose status does not read done (e.g.
            # ``evicted``, or a ``downloading`` supplementary over an already-imported
            # episode) but that still owns an IMPORTED download has a torrent that may
            # still be seeding -- the same orphan risk. Probe for the imported row
            # SPECIFICALLY, not merely the newest attempt: a newer failed/downloading row
            # must not hide an older imported seed underneath it.
            imported = await download_repo.find_latest_imported_for_request(
                request_id, season=srec.season_number
            )
            if imported is not None:
                raise NotCancellableError(request_id, request.status)

    active = await download_repo.list_active_for_request(request_id)
    # Never fail an ``importing`` row (see ImportInProgressError): it is mid-copy/scan,
    # and flipping it to ``failed`` would make the importer's finalize CAS lose and
    # strand the placed file. Refuse up front -- no torrent removed, nothing settled.
    if any(row.status == DownloadState.Importing.value for row in active):
        raise ImportInProgressError(request_id)

    # Move every active row out of the active set (so the reconciler stops tracking it
    # and the queue drops it) BEFORE removing any torrent, via a compare-and-swap gated
    # on the row still being cancellable (not ``importing``/terminal). Doing the whole
    # transition first means a row that raced INTO ``importing`` since the snapshot fails
    # its swap and we abort the WHOLE cancel with nothing irreversible done yet (no
    # torrent removed; the rollback undoes the earlier swaps). Reuses the terminal
    # ``Failed`` state (an honest "not completed") with a cancel reason -- this write does
    # NOT go through the reconciler's failed_download_events, so it triggers no
    # blocklist/re-search (cancel must never re-grab).
    hashes_to_remove: list[str] = []
    for row in active:
        moved = await download_repo.update_status_if_in(
            row.id,
            DownloadState.Failed.value,
            _CANCELLABLE_DOWNLOAD_STATE_VALUES,
            failed_reason="cancelled by operator",
        )
        if not moved:
            # The row left the cancellable set underneath us (an import claimed it
            # ``importing`` during the ``list_active`` -> here gap). Abort the whole
            # cancel: roll back the swaps done so far and surface a retryable refusal
            # rather than half-cancelling around a finalizing import.
            await session.rollback()
            raise ImportInProgressError(request_id)
        hashes_to_remove.append(row.torrent_hash)

    if request.media_type == "tv":
        # Settle every tracked season to cancelled; the parent rollup then folds to
        # cancelled (season_rollup handles all-cancelled). Unconditional (a failed
        # season is cancelled too) -- the per-season guard above already refused if
        # any season was available/completed or still owned an imported torrent, so
        # nothing done is being dishonestly settled or orphaned here. Reuses the
        # ``seasons`` fetched by that guard.
        for srec in seasons:
            await season_request_service.set_status(
                session,
                media_request_id=request_id,
                season_number=srec.season_number,
                status=RequestStatus.cancelled.value,
            )
    else:
        await request_repo.set_status(request_id, RequestStatus.cancelled.value)

    session.add(
        DownloadHistory(
            tmdb_id=request.tmdb_id,
            torrent_hash=None,
            event_type=DownloadHistoryEvent.cancelled,
            source_title=request.title,
            message="cancelled by operator: removed any active torrent, settled cancelled",
        )
    )
    await session.commit()

    # Remove each cancelled torrent + its data AFTER the DB cancel has committed, so a
    # client hiccup never undoes the committed settle (mirrors queue_service.mark_failed).
    # Best-effort + already-gone-is-a-no-op (see purge_service.remove_torrent).
    for torrent_hash in hashes_to_remove:
        await purge_service.remove_torrent(
            qbt,
            torrent_hash,
            context="a cancel",
            extra={"torrent_hash": torrent_hash, "request_id": safe_int(request_id)},
        )

    updated = await request_repo.get(request_id)
    if updated is None:  # pragma: no cover - just operated on this row
        raise RequestNotFoundError(request_id)
    return updated
