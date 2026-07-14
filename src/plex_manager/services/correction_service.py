"""Correction verbs (ADR-0014): report-issue and cancel.

Two operator corrections for the two lifecycle stages that come AFTER a grab,
each self-healing so no human ticket is ever needed (north-star #1: a button, not
a terminal):

* :func:`report_issue` — "this imported file is bad, redo it." Composes existing
  primitives IN ORDER, with the active-slot CLAIM deliberately BEFORE any
  irreversible step: (a) blocklist the culprit release (resolved from the imported
  download's history), (b) re-arm the request/season to ``searching`` -- the claim
  of the ``uq_media_requests_active`` slot -- so a racing re-request that grabbed the
  slot collides HERE (surfaced as ``ActiveDuplicateError`` 409) while nothing is yet
  deleted, rather than after the file/torrent are irreversibly gone. Still inside
  that same claim, ANY sibling season of a shared multi-season pack torrent (issue
  #175 -- a partial-import ``import_blocked`` row carrying an ``imported`` scope for
  the reported season and a non-terminal scope for another) is rescued: its scope and
  the pack's download row are terminalized and the sibling season is independently
  re-armed to ``searching``, so it is never silently orphaned when (c) below deletes
  the shared torrent's payload. (c) remove the culprit torrent WITH data, (d) purge
  the library file via the shared root-guarded
  purge primitive (clearing the breadcrumb only when the file was actually removed),
  (e) trigger a Plex scan, (f) write an audit history row + commit, (g) synchronously
  run the SAME decision-engine -> grab path the grab endpoint uses, so the re-search
  happens inline -- the blocklist now excludes the bad release, guaranteeing a
  DIFFERENT one is grabbed (or the honest ``no_acceptable_release`` park if nothing is
  acceptable). Following ``auto_grab_service``'s operational-vs-park taxonomy, ONLY a
  genuinely empty acceptable-release set (or every candidate per-release-unusable)
  parks ``no_acceptable_release``; an OPERATIONAL failure -- the indexer unreachable
  during the re-search, the download client erroring, or a grab that left a live
  untracked torrent -- must NOT park (that would LIE about content exhaustion) and
  instead leaves the scope at the already-committed ``searching`` for the merged
  auto-grab worker to retry. The synchronous re-grab IS the auto re-search AND the undo
  (the content comes back), which is why no recycle bin is needed for the beta.

  Ordering rationale (ADR-0014 race fix): steps (c)/(d) are IRREVERSIBLE, so the
  slot claim (b) runs first and is committed atomically WITH them -- SQLite
  serializes writers, so once (b)'s flush holds the slot no competitor can commit a
  conflicting active row before this transaction's own commit, and the earlier bug
  (the claim happening AFTER the purge, letting a concurrent re-request's collision
  roll the DB back while the deletions stood) cannot recur.

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

Subscriber control (issue #314) adds two collaborative-cancellation verbs on top
of the two above, for a shared (subscriber-having) request:

* :func:`cancel_request_as_owner` — the non-admin flavor of ``POST /cancel``:
  hard-cancels via :func:`cancel_request` when the caller is the SOLE
  participant, otherwise refuses (:class:`HasOtherParticipantsError`) rather
  than nuking co-participants' shared request.

* :func:`withdraw_participant` — "remove ME, not the request" (``DELETE
  /subscription``): a mere subscription removal when others remain (with an
  ownership handoff to the earliest remaining subscriber if the withdrawing
  user was the owner), or -- when the caller is the LAST participant -- the
  same :func:`cancel_request` teardown reused verbatim for a not-yet-imported
  row, else a plain ownerless settle for an already-terminal one.

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

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from plex_manager.adapters.prowlarr.adapter import IndexerError
from plex_manager.adapters.qbittorrent.adapter import QbittorrentError, QbittorrentSourceError
from plex_manager.domain.season_pack import MultiSeasonRequestIntent, SeasonPackSeasonState
from plex_manager.domain.state_machine import DownloadState
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
from plex_manager.services import (
    audit_service,
    blocklist_service,
    decision_service,
    grab_service,
    purge_service,
    queue_service,
    request_service,
    season_request_service,
)
from plex_manager.services.auto_grab_service import MAX_GRAB_ATTEMPTS_PER_SCOPE
from plex_manager.services.import_service import PATH_NOT_VISIBLE_REASON_PREFIX
from plex_manager.services.library_roots import deepest_containing_root
from plex_manager.services.purge_service import PurgeOutcome

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from plex_manager.domain.quality_profile import QualityProfile
    from plex_manager.ports.download_client import DownloadClientPort
    from plex_manager.ports.filesystem import FileSystemPort
    from plex_manager.ports.indexer import IndexerPort
    from plex_manager.ports.library import LibraryPort
    from plex_manager.ports.parser import ParserPort
    from plex_manager.ports.repositories import DownloadRecord, RequestRecord, SeasonRequestRecord
    from plex_manager.services.library_roots import LibraryRoots

__all__ = [
    "CANCELLABLE_REQUEST_STATUS_VALUES",
    "REPORTABLE_STATUS_VALUES",
    "ActiveDuplicateError",
    "DownloadClientRequiredError",
    "DownloadNotFoundError",
    "DownloadsRootUnavailableError",
    "HasOtherParticipantsError",
    "ImportInProgressError",
    "MediaRootUnavailableError",
    "NotCancellableError",
    "NotRelocatableError",
    "NotReportableError",
    "RelocationSupersededError",
    "ReportSeasonRequiredError",
    "RequestNotFoundError",
    "SeasonNotFoundError",
    "WithdrawOutcome",
    "WithdrawalBlockedActiveError",
    "cancel_request",
    "cancel_request_as_owner",
    "relocate_stranded_download",
    "report_issue",
    "withdraw_participant",
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
        RequestStatus.waiting_for_air_date.value,
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
_UNRESOLVED_SCOPE_STATUSES: Final[frozenset[str]] = frozenset({"active", "import_blocked"})

# The PER-RELEASE grab failures the inline re-grab falls through on (mirroring
# ``auto_grab_service``'s per-release set): the ATTEMPTED release is unusable --
# no source, an unresolvable/vetoed/oversized source (``QbittorrentSourceError``
# is raised BEFORE anything reaches the client; it subclasses ``QbittorrentError``
# so it MUST be caught before ``_DOWNLOAD_CLIENT_ERRORS`` or a bad source would be
# mistreated as a client outage and the scope silently left at ``searching``), or
# its hash is already active under a different request -- while a LOWER-ranked
# accepted replacement may still be grabbable. The re-grab tries the next candidate
# (bounded) and only parks when the list/cap exhausts.
_PER_RELEASE_GRAB_ERRORS: Final = (
    grab_service.NoGrabSourceError,
    grab_service.TorrentAlreadyTrackedError,
    QbittorrentSourceError,
)

# The OPERATIONAL grab-pipeline failure the inline re-grab may raise: qBittorrent
# ACCEPTED the torrent but no info-hash could be derived (an opaque URL, and the
# indexer supplied none either), leaving a LIVE, untracked torrent and no
# ``Download`` row. Mirrors ``auto_grab_service``'s ``GrabError`` handling: this is
# NOT "nothing acceptable" -- releases exist; the grab PIPELINE failed -- so parking
# ``no_acceptable_release`` would LIE. The handler leaves the scope at the
# ``searching`` committed at (b) for the merged auto-grab worker to retry, and does
# NOT try another candidate (a second grab against the live orphan would double-
# download).
_GRAB_OPERATIONAL_ERRORS: Final = (grab_service.GrabError,)

# The SCOPE-level grab refusals the inline re-grab may raise (all defined in
# ``grab_service``). Unlike the per-release failures, these apply to the whole SCOPE,
# not this one release, so trying a lower-ranked candidate cannot help: the scope now
# has an active download (``AlreadyDownloadingError``), or is terminal / mis-shaped
# (``RequestNotActiveError`` / ``SeasonRequiredError``), or the replacement resolved
# to a hash whose terminal row is being removed RIGHT NOW by a racing cancel /
# reconcile / operator delete (``TorrentRemovalInFlightError``, #206). The last is
# transient -- by the auto-grab worker's next tick the removal has settled -- so like
# the others it must LEAVE the scope at the ``searching`` committed at (b) for retry,
# not surface an unhandled 500 out of the requests router. Mirrors auto-grab's
# settle-and-leave: discard the partial write and LEAVE the scope's committed
# ``searching`` as-is -- never a ``no_acceptable_release`` park (that would LIE:
# releases exist; the grab was refused for a scope reason, not exhaustion).
_GRAB_SCOPE_REFUSALS: Final = (
    grab_service.AlreadyDownloadingError,
    grab_service.RequestNotActiveError,
    grab_service.SeasonRequiredError,
    grab_service.TorrentRemovalInFlightError,
)

# The indexer failures the inline RE-SEARCH (``decision_service.preview`` ->
# ``prowlarr.search``) may raise. Mirrors ``auto_grab_service``'s raised-search
# handling: a report-issue that has ALREADY blocklisted + purged must not propagate a
# Prowlarr transport/rate-limit/HTTP failure out as a 5xx -- but this is an
# OPERATIONAL failure (Prowlarr down / rate-limited), NOT content exhaustion, so it
# must NOT park ``no_acceptable_release`` either (that would LIE: releases may well
# exist; the INDEXER is unreachable). The scope is LEFT at the ``searching`` committed
# at (b) for the merged auto-grab worker to retry on its next tick.
# ``IndexerRateLimitError`` is a subclass of ``IndexerError`` and so is covered.
_INDEXER_ERRORS: Final = (IndexerError,)

# The download-client failures the inline RE-GRAB (``grab_service.grab`` ->
# ``qbt.add``) may raise. Unlike the SCOPE refusals (``_GRAB_SCOPE_REFUSALS``) and the
# per-release failures, these are an OPERATIONAL client failure -- qBittorrent is
# unreachable / erroring -- AFTER the blocklist/purge/reset already committed.
# Following ADR-0013's park-vs-operational distinction, this must NOT park
# ``no_acceptable_release`` (that would LIE: releases exist; it is the CLIENT that
# failed, exactly like auto-grab's ``GrabError`` handling). The report-issue handler
# catches this family and LEAVES the scope at ``searching`` -- the merged auto-grab
# worker picks up an eager ``searching`` scope on its next tick, so the state
# self-heals -- rather than letting a 502 escape after a successful correction.
# ``QbittorrentSourceError`` is caught FIRST by the inner per-release handler (it
# subclasses ``QbittorrentError`` but is a source veto, not an outage), and
# ``QbittorrentAuthError`` is a subclass of ``QbittorrentError`` and so is covered.
_DOWNLOAD_CLIENT_ERRORS: Final = (QbittorrentError,)


async def _multi_season_intent_for_request(
    session: AsyncSession,
    request: RequestRecord,
) -> MultiSeasonRequestIntent | None:
    if request.media_type != "tv" or request.tv_request_mode not in {
        "whole_show",
        "explicit_seasons",
        "explicit_episodes",
    }:
        return None
    season_rows = await SqlSeasonRequestRepository(session).list_for_request(request.id)
    requested = (
        request.requested_seasons
        or tuple(sorted(request.requested_episodes or {}))
        or tuple(row.season_number for row in season_rows)
    )
    return MultiSeasonRequestIntent(
        mode="whole_show" if request.tv_request_mode == "whole_show" else "explicit_seasons",
        requested_seasons=tuple(requested),
        seasons=tuple(
            SeasonPackSeasonState(
                season_number=row.season_number,
                status=row.status,
                installed_quality_id=row.installed_quality_id,
                installed_profile_index=row.installed_profile_index,
            )
            for row in season_rows
        ),
    )


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


async def _rescue_shared_pack_siblings(
    session: AsyncSession,
    culprit: DownloadRecord,
    *,
    reported_request_id: int,
    reported_season: int | None,
    log_extra: dict[str, object],
) -> None:
    """Rescue sibling seasons of a PARTIAL multi-season pack before its torrent is
    removed (issue #175).

    A single multi-season torrent import runs ONCE at completion
    (``import_service._import_tv``): a partial success leaves the download ROW at
    ``import_blocked`` with one ``imported`` :class:`DownloadScope` for the season(s)
    that made it in and a sibling ``import_blocked``/``active`` scope for the
    season(s) that did not. ``report_issue`` on the GOOD season used to re-arm only
    that season, then remove the shared torrent WITH DATA at step (c) -- deleting the
    sibling season's payload out from under it while its scope/row silently rotted at
    ``import_blocked`` with no live torrent (a zombie: never auto-drained, since
    ``import_blocked`` is retried only on demand).

    This mirrors ``queue_service``'s ``mark_failed`` precedent (the existing manual
    recovery for exactly this shape): terminalize every non-terminal sibling scope,
    terminalize the pack's download row, then re-arm each sibling's OWN season to a
    fresh ``searching`` via the same ``reset_for_research`` idiom report-issue already
    uses for the reported season -- so the sibling is picked up by the merged
    auto-grab worker instead of being silently orphaned.

    A no-op (returns immediately) for movies and any single-season / fully-imported
    pack: it is only sibling ``active``/``import_blocked`` scopes -- OTHER than the
    one being reported -- that trigger the rescue, so the fix is surgical.

    Race safety (two independent CAS layers, both required): ``siblings`` is built
    from ``culprit.scopes``, a DTO snapshot read BEFORE this function's own writes --
    a concurrent import retry can complete a sibling (its scope -> ``imported``)
    anywhere in the gap between that read and this function actually running. Acting
    on the stale snapshot alone would re-arm an already-satisfied season back to
    ``searching``, and a later re-search could grab a needless duplicate.

    1. The DOWNLOAD-ROW CAS below (``update_status_if_in``) must be WON before
       touching any scope: losing it means a different actor (another concurrent
       report-issue redo of the same pack, or the row reaching a terminal status on
       its own) already owns this download, so nothing here is re-derived from a
       stale read of a row we do not control -- this function returns immediately.
    2. Winning the row CAS is NOT enough for any ONE sibling. ``import_service``'s TV
       finalize can process several targets of the SAME retry in one transaction: if
       season 2 (our sibling) succeeds while a DIFFERENT season in that same retry
       fails, the row's own status is committed right back to ``import_blocked`` --
       identical to what it was before, just blocked on a different season now. That
       retry's commit is invisible to the row-level CAS above (``import_blocked ->
       import_blocked`` is not a value change it can detect), yet the sibling's OWN
       scope DID move to ``imported`` in that same commit. So each sibling's
       terminalize-and-rearm is separately gated on its OWN
       :meth:`SqlDownloadRepository.update_scope_status_if_in` CAS -- won only while
       that exact scope is STILL in ``_UNRESOLVED_SCOPE_STATUSES`` at write time. A
       lost scope CAS skips that sibling (honest log, no re-arm); it does not abort
       the others.

    A final sweep (:func:`_mark_download_scopes_terminal`, unconditional and
    already race-safe -- it is a single ``UPDATE ... WHERE status IN (...)``) runs
    AFTER the per-sibling loop to terminalize any stray non-terminal scope this
    function could not individually re-arm (e.g. one whose owning request/season
    was already cleared -- see the ``continue`` below), so nothing is orphaned at
    ``active``/``import_blocked`` once the shared torrent is gone.
    """
    siblings: list[tuple[int, int, int, str]] = []
    for scope in culprit.scopes:
        if scope.status not in _UNRESOLVED_SCOPE_STATUSES:
            continue
        if scope.media_request_id is None or scope.season is None:
            continue
        if (scope.media_request_id, scope.season) == (reported_request_id, reported_season):
            continue
        siblings.append((scope.media_request_id, scope.season, scope.id, scope.status))

    if not siblings:
        return

    download_repo = SqlDownloadRepository(session)
    moved = await download_repo.update_status_if_in(
        culprit.id,
        DownloadState.Failed.value,
        frozenset({DownloadState.ImportBlocked.value}),
        failed_reason="torn down by a report-issue redo of a shared season pack",
        clear_download_path=True,
    )
    _logger.info(
        "report_issue: terminalized shared-pack download row",
        extra={
            "download_id": safe_int(culprit.id),
            "moved": moved,
            **log_extra,
        },
    )
    if not moved:
        # Lost the row CAS: some other actor already owns this download's status
        # (a concurrent redo of the same pack, or a legitimate independent
        # transition). We have no exclusive claim on its scopes -- do NOT
        # terminalize or re-arm anything from the stale ``siblings`` snapshot.
        _logger.info(
            "report_issue: skipped shared-pack sibling rescue -- lost the download-row CAS",
            extra={"download_id": safe_int(culprit.id), **log_extra},
        )
        return

    for sibling_request_id, sibling_season, sibling_scope_id, prior_status in siblings:
        scope_won = await download_repo.update_scope_status_if_in(
            sibling_scope_id,
            RequestStatus.failed.value,
            _UNRESOLVED_SCOPE_STATUSES,
        )
        if not scope_won:
            # Raced by a concurrent import retry that resolved this exact sibling
            # scope (e.g. to ``imported``) between the DTO read and this CAS --
            # honor whoever moved it and never re-arm a season that is no longer
            # actually unresolved.
            _logger.info(
                "report_issue: skipped shared-pack sibling season -- scope left the "
                "non-terminal set before the rescue could claim it",
                extra={
                    "download_id": safe_int(culprit.id),
                    "sibling_request_id": safe_int(sibling_request_id),
                    "sibling_season": safe_int(sibling_season),
                    "prior_scope_status": safe_text(prior_status),
                    **log_extra,
                },
            )
            continue

        await season_request_service.reset_for_research(
            session,
            media_request_id=sibling_request_id,
            season_number=sibling_season,
            clear_library_path=False,
        )
        _logger.info(
            "report_issue: rescued shared-pack sibling season",
            extra={
                "download_id": safe_int(culprit.id),
                "sibling_request_id": safe_int(sibling_request_id),
                "sibling_season": safe_int(sibling_season),
                "prior_scope_status": safe_text(prior_status),
                **log_extra,
            },
        )

    # Sweep any remaining non-terminal scope on this download that the per-sibling
    # loop above could not address (e.g. one whose owning request/season was
    # already cleared to NULL -- filtered out of ``siblings`` since there is
    # nothing to re-arm). Race-safe on its own (WHERE status IN (...)); harmless
    # when the loop above already resolved every sibling.
    await _mark_download_scopes_terminal(session, culprit.id, RequestStatus.failed.value)


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


class HasOtherParticipantsError(Exception):
    """A non-admin owner's ``POST /cancel`` would hard-cancel co-participants too
    (issue #314, HTTP 409).

    ``POST /cancel`` is a hard cancel of the WHOLE request -- never a silent
    self-removal. When other subscribers still want this title, the owner's
    correction path is collaborative self-removal (``DELETE /subscription`` ->
    :func:`withdraw_participant`), which hands ownership off instead of nuking
    everyone else's shared request. Raised BEFORE anything is touched.
    """

    def __init__(self, request_id: int) -> None:
        self.request_id = request_id
        super().__init__(
            f"request {request_id} has other subscribers; a non-admin owner must "
            f"withdraw instead of cancelling the whole request"
        )


class WithdrawalBlockedActiveError(Exception):
    """The LAST participant cannot withdraw from an ACTIVE, non-cancellable
    request (issue #314, HTTP 409).

    ``import_blocked`` and ``partially_available`` are the two statuses that are
    neither cancellable (:data:`CANCELLABLE_REQUEST_STATUS_VALUES` -- there is a
    blocked download or an in-flight season that ``cancel_request`` deliberately
    will not tear down) NOR genuinely settled
    (``request_service.TERMINAL_REQUEST_STATUS_VALUES`` -- they still shadow a
    duplicate via ``uq_media_requests_active``). Letting the last participant
    withdraw here would strand an ACTIVE, dedup-blocking row with zero
    subscribers and no owner -- nobody left to resolve the block or the in-flight
    seasons, yet it keeps blocking a fresh request for the same media. Refused
    BEFORE anything is touched: the correction path is to resolve the import
    (retry/report) or let the in-flight seasons settle first, THEN withdraw from
    the resulting terminal row.
    """

    def __init__(self, request_id: int, status: str) -> None:
        self.request_id = request_id
        self.status = status
        super().__init__(
            f"request {request_id} is {status!r}, an active non-cancellable state; "
            f"resolve it before the last participant withdraws"
        )


class MediaRootUnavailableError(Exception):
    """The breadcrumb's media root is missing/empty, or unknown -- refuse to purge
    (HTTP 409).

    The Radarr-style failsafe: an unmounted drive must never let a report-issue
    blocklist the good release and re-grab a duplicate against content that is
    still really there (``fs.delete`` would silently no-op on the not-present path).

    ADR-0015 fix: the root to verify is derived FROM the stored ``library_path``
    breadcrumb -- the DEEPEST configured root containing it (see
    :func:`~plex_manager.services.library_roots.deepest_containing_root`; nested
    roots must resolve to the most specific owner, never a mounted parent of a
    down child mount), never from the request's ``is_anime`` flag + the
    currently-configured anime root -- a title imported before its anime root
    existed lives under ``movies_root``/``tv_root``. Raised when that owning root
    is unmounted/empty, when the breadcrumb sits under NONE of the configured
    roots (an honest, correctable refusal rather than a silent blocklist+re-grab
    against a file we cannot locate to purge), and when a NO-breadcrumb row that
    HAS a culprit download's media-type-appropriate fallback root is unset/
    unmounted (see the failsafe comment in :func:`report_issue`).

    Issue #131: a row with NEITHER a breadcrumb NOR a culprit download is purely
    presence-derived (recorded available straight from Plex) -- there is nothing
    of ours to protect, so the fallback check is SKIPPED for it and this error is
    never raised on its account; see the reacquire relaxation in
    :func:`report_issue`.
    """

    def __init__(self, request_id: int, root_path: str | None) -> None:
        self.request_id = request_id
        self.root_path = root_path
        super().__init__(
            f"media root for request {request_id} is unavailable "
            f"(unmounted/empty, or the breadcrumb is under no configured root)"
        )


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


class DownloadClientRequiredError(Exception):
    """A cancel that must remove torrent(s) needs qBittorrent, but it is unconfigured.

    ADR-0014 round follow-up: a cancel for a ``pending``/``searching``/
    ``no_acceptable_release`` request with NO active download rows is a pure DB settle
    -- it never touches the client -- so ``cancel_request`` resolves qBittorrent
    OPTIONALLY (``get_qbittorrent_optional``) and still works on an install without the
    client configured. But a cancel that DOES own active torrent(s) genuinely needs the
    client to remove them; skipping that silently would leak a seeding torrent. When
    active rows exist and the client is ``None``, this is raised BEFORE any state
    change (nothing settled, no torrent touched) so the endpoint can surface the honest
    409 ``service_not_configured`` -- mirroring the mark-failed endpoint's own upfront
    refusal when removal is requested without a configured client.
    """

    def __init__(self, request_id: int) -> None:
        self.request_id = request_id
        super().__init__(
            f"request {request_id} has active torrent(s) to remove but qBittorrent "
            f"is not configured"
        )


class DownloadNotFoundError(Exception):
    """No download exists for the given id (HTTP 404)."""

    def __init__(self, download_id: int) -> None:
        self.download_id = download_id
        super().__init__(f"download {download_id} does not exist")


class NotRelocatableError(Exception):
    """The download is not an import-blocked, path-invisible row (HTTP 409).

    :func:`relocate_stranded_download` is scoped EXACTLY to the "download path not
    visible inside the container" block (issues #133/#157) -- never a general-purpose
    mover. A download in any other state (or ``import_blocked`` for a DIFFERENT
    reason, e.g. a genuinely bad/wrong-media file) has nothing a relocate would fix,
    so it is refused rather than silently no-op'd.
    """

    def __init__(self, download_id: int, status: str) -> None:
        self.download_id = download_id
        self.status = status
        super().__init__(
            f"download {download_id} (status={status!r}) is not a path-invisible "
            f"import-blocked row; nothing to relocate"
        )


class DownloadsRootUnavailableError(Exception):
    """No HOST-namespace downloads root could be derived (HTTP 409).

    The root-guard for :func:`relocate_stranded_download`: it may ONLY ever direct
    qBittorrent to move a torrent INTO the app's own derived downloads root
    (``path_visibility.resolve_downloads_host_root``), never a caller-chosen or
    guessed path. When ``PLEX_MANAGER_DOWNLOADS_ROOT`` is unset, there is nothing
    safe to relocate into -- refuse rather than send qBittorrent an
    empty/placeholder location.
    """

    def __init__(self, download_id: int) -> None:
        self.download_id = download_id
        super().__init__(
            f"cannot relocate download {download_id}: no downloads host root could be derived"
        )


class RelocationSupersededError(Exception):
    """The row was re-blocked with a NEWER, different reason while the
    relocation was in flight (HTTP 409).

    :func:`relocate_stranded_download` observes the row's ``failed_reason`` up
    front and issues the (async) ``qbt.set_location`` request; a concurrent
    "Retry import" can re-block the SAME (still ``import_blocked``) row with a
    genuinely different diagnosis (e.g. "no video file found") in the gap
    before this function's own terminal write. The terminal write is a
    compare-and-swap gated on BOTH the status AND the exact ``failed_reason``
    observed at entry, so a losing CAS here means a fresher block reason
    already won -- overwriting it would silently discard that newer diagnosis
    (the bug this guards against). The relocation request was still issued to
    qBittorrent (this is only about which message the row now carries); the
    operator sees the newer, truthful reason rather than a stale "relocation
    requested" message that no longer matches reality.
    """

    def __init__(self, download_id: int, current_reason: str | None) -> None:
        self.download_id = download_id
        self.current_reason = current_reason
        super().__init__(
            f"download {download_id} was re-blocked with a newer reason "
            f"({current_reason!r}) while its relocation was in flight; the move "
            "was still requested, but the row's message was left as-is"
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
    roots: LibraryRoots,
    save_path: str = "",
) -> RequestRecord:
    """Report a bad imported file: blocklist + purge (torrent + library) + re-search.

    ``save_path`` (issues #133/#157) is threaded verbatim into the inline
    replacement :func:`grab_service.grab` call below: the caller (the report-issue
    endpoint) resolves the HOST-namespace downloads root once
    (``path_visibility.resolve_downloads_host_root``) and passes it here, so the
    replacement torrent lands under the mounted ``/downloads`` bind exactly like a
    manual grab. ``""`` (the default) leaves qBittorrent's own default in charge,
    unchanged prior behaviour.

    Returns the updated request record (re-read after the inline re-grab, so its
    status reflects ``downloading`` on a successful replacement grab,
    ``no_acceptable_release`` / the season rollup when nothing acceptable was found, or
    ``searching`` when an OPERATIONAL failure -- see below -- left the scope for the
    auto-grab worker). See the module docstring for the full ordered flow and caveats.

    Re-grab client failure (ADR-0013 park-vs-operational): if the inline re-grab's
    ``qbt.add`` raises a download-client error (``_DOWNLOAD_CLIENT_ERRORS``) AFTER the
    blocklist/purge/reset already committed, it is NOT parked ``no_acceptable_release``
    -- that would LIE (releases exist; the CLIENT failed, exactly like auto-grab's
    ``GrabError`` handling). Instead the scope is LEFT at ``searching`` and the current
    state returned normally (a 200, not a 502): the merged auto-grab worker picks up an
    eager ``searching`` scope on its next tick (~60s), so the state self-heals and the
    operator sees "searching" rather than an error page after a successful correction.

    Re-grab PER-RELEASE failure (mirroring auto-grab): a replacement whose grab
    fails for a reason specific to THAT release (``_PER_RELEASE_GRAB_ERRORS`` --
    no/unresolvable/vetoed source, or a hash already tracked elsewhere; nothing
    live to track in any case) falls through to the next-ranked accepted
    replacement, bounded by the shared :data:`~plex_manager.services.
    auto_grab_service.MAX_GRAB_ATTEMPTS_PER_SCOPE`; only an exhausted list/cap
    parks. In particular a bad HTTP torrent source (``QbittorrentSourceError``,
    a ``QbittorrentError`` subclass) is a RELEASE problem handled here -- never
    mistaken for the client outage its base class signals, which would strand
    the promised synchronous re-grab at ``searching`` (worst with auto-grab
    disabled: nothing would ever retry it).

    Re-search / operational-grab failures (issue #71, mirroring auto-grab's
    operational-vs-park taxonomy): an indexer failure during the inline RE-SEARCH
    (``_INDEXER_ERRORS`` -- Prowlarr down / rate-limited), an operational GRAB failure
    that left a live untracked torrent (``_GRAB_OPERATIONAL_ERRORS`` --
    ``grab_service.GrabError``), or a scope-level grab refusal (``_GRAB_SCOPE_REFUSALS``
    -- the scope now has an active download, or is terminal / mis-shaped) are all
    OPERATIONAL, NOT content exhaustion, and so must NOT park ``no_acceptable_release``
    (a LIE: releases exist / may exist; the PIPELINE failed). Each LEAVES the scope at
    the ``searching`` committed at (b) for the merged auto-grab worker to retry --
    identical to the download-client-outage handling above. ONLY a genuinely empty
    acceptable-release set (or every candidate per-release-unusable) parks.
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

    # Resolve the culprit release from the IMPORTED download for (request, season) --
    # the row that actually placed the file being reported (and whose torrent still
    # hardlink-seeds it), never merely the newest attempt: a season already available
    # can carry a NEWER supplementary/failed row over the older imported one, and
    # blocklisting/removing that would leave the real seed untouched so the purge frees
    # nothing (ADR-0014). ``None`` when the title was recorded available straight from
    # Plex (no download of ours) -- the blocklist/remove steps below are then skipped.
    # Resolved here, BEFORE the Foot-gun failsafe below, because the failsafe's
    # no-breadcrumb fallback needs to know whether there is a culprit to protect.
    download_repo = SqlDownloadRepository(session)
    culprit = await download_repo.find_latest_imported_for_request(request_id, season=target.season)

    # Foot-gun failsafe (ADR-0015 fix): refuse if the breadcrumb's own root is
    # unmounted/empty (see MediaRootUnavailableError). The root to verify is DERIVED
    # from the stored ``library_path`` -- the DEEPEST configured root containing it
    # (nested roots: e.g. an anime root mounted inside movies_root must be verified
    # itself, never its mounted parent) -- never from ``is_anime`` + the currently-
    # configured anime root, so a title imported before its anime root existed (file
    # under movies_root/tv_root) is checked against its REAL root. A breadcrumb under
    # NO configured root fails honestly here (correctable) rather than silently
    # blocklisting + re-grabbing against a file we cannot even locate to purge.
    # Checked BEFORE any blocklist/remove/flip so a missing drive aborts the whole
    # verb rather than firing against content that is not really gone.
    #
    # A row with NO breadcrumb but a CULPRIT download (a legacy row predating the
    # library_path column, whose torrent may still hardlink-seed the file) has no
    # path to derive an owner from, so the failsafe falls back to the media-type-
    # appropriate root (the anime root for an is_anime row when configured, else the
    # normal root -- the pre-fix pick, and the same root the file most plausibly
    # lives under). Skipping the check entirely would let a report against an
    # unmounted library blocklist the good release and re-grab a duplicate of a file
    # that is still really there once the drive returns.
    #
    # Issue #131 relaxation: a row with NEITHER a breadcrumb NOR a culprit is
    # PURELY presence-derived (recorded available straight from Plex; nothing of
    # ours ever placed it). There is nothing to blocklist (culprit is None) and
    # nothing to purge (no breadcrumb), so an unmounted fallback root protects no
    # file of ours -- the mount check is SKIPPED and the verb proceeds straight to
    # the honest re-arm + re-search reacquire semantics instead of a confusing 409
    # dead-end (the operator has no file of ours to lose either way).
    if target.library_path is not None:
        check_root = deepest_containing_root(target.library_path, roots.configured())
        if check_root is None or not await asyncio.to_thread(_root_is_mounted, check_root):
            raise MediaRootUnavailableError(request_id, check_root or target.library_path)
    elif culprit is not None:
        fallback_root = roots.fallback_for(request.media_type, is_anime=request.is_anime)
        if not await asyncio.to_thread(_root_is_mounted, fallback_root):
            raise MediaRootUnavailableError(request_id, fallback_root)

    is_tv = target.season is not None
    media_type = "tv" if is_tv else "movie"
    season_note = f" season {target.season}" if target.season is not None else ""
    log_extra: dict[str, object] = {"request_id": safe_int(request_id), "tmdb_id": request.tmdb_id}

    # (a) blocklist the culprit release (nothing to blocklist if the title was
    # recorded available straight from Plex, with no download of ours). A REVERSIBLE
    # DB write -- rolled back cleanly if the slot claim (b) collides below.
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

    # (b) claim the active slot: re-arm the request/season to 'searching' BEFORE any
    # irreversible step (torrent removal / file purge below). The re-arm flush claims
    # this media's ``uq_media_requests_active`` slot; a racing re-request that already
    # grabbed the slot makes this flush raise IntegrityError -- caught here, rolled back
    # (undoing the reversible blocklist too), and surfaced as ``ActiveDuplicateError``
    # (409) with NOTHING yet deleted, rather than after the file/torrent are irreversibly
    # gone and the rollback undoes only the DB (the earlier bug). The upfront
    # ``find_active`` check above rejects the common case cheaply; this is the
    # AUTHORITATIVE guard for a sibling appearing in the check->claim gap. The breadcrumb
    # is deliberately KEPT here (``clear_library_path=False``): ``purge_ok`` is not known
    # until (d), which clears it only if the file was actually removed.
    try:
        if is_tv and target.season is not None:
            await season_request_service.reset_for_research(
                session,
                media_request_id=request_id,
                season_number=target.season,
                clear_library_path=False,
            )
        else:
            await request_repo.reset_for_research(request_id, clear_library_path=False)

        # Rescue any sibling season(s) of a shared multi-season pack BEFORE the
        # torrent-with-data removal at (c) below deletes their payload out from
        # under them (issue #175) -- inside this try/except so a collision here
        # rolls back everything (blocklist + partial re-arm + rescue) with
        # NOTHING yet deleted, same as the target re-arm above.
        if culprit is not None:
            await _rescue_shared_pack_siblings(
                session,
                culprit,
                reported_request_id=request_id,
                reported_season=target.season,
                log_extra=log_extra,
            )
    except IntegrityError as exc:
        # The re-arm collided on ``uq_media_requests_active`` -- a newer active sibling
        # grabbed the slot between the upfront check and this flush. Roll back (undoing
        # the blocklist + partial re-arm) so NOTHING is left half-written, then surface
        # the honest 409. Re-read the sibling for the error's id (best-effort -- it is
        # informational; the endpoint keys only on the type).
        await session.rollback()
        sibling = await request_repo.find_active(request.tmdb_id, request.media_type)
        raise ActiveDuplicateError(
            request_id,
            sibling.id if sibling is not None and sibling.id != request_id else request_id,
        ) from exc

    # (c) remove the culprit torrent WITH data (best-effort) -- the hardlink caveat
    # means this must go too, not just the library file. The FIRST irreversible step,
    # so it runs only AFTER the slot claim (b) succeeded.
    if culprit is not None:
        await purge_service.remove_torrent(
            qbt,
            culprit.torrent_hash,
            context="a report-issue",
            extra={"torrent_hash": culprit.torrent_hash, **log_extra},
        )

    # (d) purge the library file via the shared root-guarded primitive. ``purge_ok``
    # tracks whether the file was ACTUALLY removed: only then is the ``library_path``
    # breadcrumb cleared (the claim at (b) kept it). On ``error`` (a genuine delete
    # failure -- permissions, transient I/O, a partial rmtree) or ``refused`` (out-of-
    # root breadcrumb) the file may still be on disk, so the breadcrumb is PRESERVED --
    # it is the only handle a later retry / eviction has to reclaim the orphan; losing
    # it would strand the bad file with no way to purge it (honesty over silence).
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
        elif purge.outcome in {PurgeOutcome.error, PurgeOutcome.deferred}:
            purge_ok = False
            _logger.warning(
                "report-issue purge of %r failed (%s); re-searching anyway but keeping "
                "the breadcrumb so the orphaned file stays reclaimable",
                safe_text(request.title),
                purge.detail,
                extra=log_extra,
            )
        if purge_ok:
            # The file was actually removed, so drop the now-dangling breadcrumb the
            # claim at (b) preserved. A targeted clear (never a second re-arm) -- the
            # status/backoff were already set at (b), and clearing library_path is not a
            # status transition, so it never re-touches ``uq_media_requests_active``.
            if is_tv and target.season is not None:
                await season_request_service.clear_library_path(
                    session, media_request_id=request_id, season_number=target.season
                )
            else:
                await request_repo.clear_library_path(request_id)
    else:
        # No breadcrumb (a title recorded available straight from Plex, or one
        # predating the library_path column): nothing of ours to delete -- honest,
        # never a guessed path, and the re-search below still runs.
        _logger.warning(
            "report-issue: no stored library_path for %r; nothing to purge",
            safe_text(request.title),
            extra=log_extra,
        )

    # (e) trigger a Plex scan so the removed item drops out of the library.
    if target.library_path is not None:
        await purge_service.trigger_library_scan(
            library,
            library_path=target.library_path,
            media_type=media_type,
            context="report-issue",
            extra=log_extra,
        )

    # (f) audit history row + commit. The blocklist (a), slot claim (b), breadcrumb
    # clear (d) and this audit row all commit TOGETHER: because SQLite serializes
    # writers, once (b)'s flush holds the slot no competitor can commit a conflicting
    # active row before this commit, so the commit cannot fail on the dedup index after
    # the irreversible (c)/(d) already ran.
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
    multi_season_intent = await _multi_season_intent_for_request(session, request)
    scope_episodes_by_season = (
        {season: list(values) for season, values in request.requested_episodes.items()}
        if request.requested_episodes
        else None
    )
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
            multi_season_intent=multi_season_intent,
        )
    except _INDEXER_ERRORS as exc:
        # The re-search could not reach the indexer AFTER the blocklist/purge/reset
        # already committed. This is OPERATIONAL (Prowlarr down / rate-limited), NOT
        # content exhaustion -- mirroring auto-grab's raised-search taxonomy -- so it
        # must NOT park ``no_acceptable_release`` (that would LIE: releases may exist;
        # the INDEXER is unreachable) and must NOT propagate a 5xx. Leave the scope at
        # the ``searching`` committed at (b): the merged auto-grab worker picks up an
        # eager ``searching`` scope on its next tick, so the state self-heals. The
        # re-read below then returns 'searching' (a 200).
        _logger.warning(
            "report-issue re-search for %r failed to reach the indexer (%s); leaving the "
            "scope at 'searching' for the auto-grab worker to retry",
            safe_text(request.title),
            type(exc).__name__,
            extra=log_extra,
        )
    else:
        if not result.accepted:
            await _park_no_acceptable(session, request_id, target.season, is_tv=is_tv)
        else:
            # Try the accepted replacements in rank order, mirroring auto-grab's
            # bounded fall-through AND its operational-vs-park taxonomy.
            # ``park_scope`` starts True and is cleared by ANY settling outcome (a
            # grab, an operational grab failure, or a scope-level refusal); only a
            # PER-RELEASE failure on every attempted candidate leaves it True, so the
            # scope parks on the honest, retryable ``no_acceptable_release`` -- exactly
            # the park an empty preview takes. An OPERATIONAL failure (the download
            # client unreachable, or a grab that left a live untracked torrent) must
            # NEVER park -- that would LIE: releases exist; the PIPELINE failed -- so
            # it leaves the scope at the ``searching`` committed at (b) for the merged
            # auto-grab worker to retry.
            try:
                park_scope = True
                for scored in result.accepted[:MAX_GRAB_ATTEMPTS_PER_SCOPE]:
                    try:
                        await grab_service.grab(
                            qbt,
                            session,
                            scored=scored,
                            request_id=request_id,
                            tmdb_id=request.tmdb_id,
                            year=request.year,
                            season=target.season,
                            episodes=None,
                            scope_episodes_by_season=scope_episodes_by_season,
                            save_path=save_path,
                        )
                        park_scope = False
                        break
                    except _PER_RELEASE_GRAB_ERRORS as exc:
                        # This RELEASE is unusable (nothing live to track --
                        # QbittorrentSourceError in particular is raised BEFORE
                        # anything reaches the client, and must not be mistaken
                        # for the client outage its base class signals); discard
                        # the partial write and try the next-ranked replacement.
                        # ``park_scope`` stays True, so an EXHAUSTED list parks.
                        await session.rollback()
                        _logger.warning(
                            "report-issue re-grab for %r: replacement release "
                            "unusable (%s); trying next accepted release",
                            safe_text(request.title),
                            type(exc).__name__,
                            extra=log_extra,
                        )
                    except _GRAB_OPERATIONAL_ERRORS as exc:
                        # qBittorrent ACCEPTED the torrent but no info-hash could be
                        # derived -> a LIVE, untracked torrent now exists. Mirrors
                        # auto-grab's ``GrabError`` handling: OPERATIONAL, NOT "nothing
                        # acceptable". Discard the partial write, do NOT try another
                        # candidate (a second grab against the orphan would double-
                        # download), do NOT park (that would LIE), and leave the scope
                        # at the ``searching`` committed at (b) for the auto-grab worker.
                        await session.rollback()
                        park_scope = False
                        _logger.warning(
                            "report-issue re-grab for %r left a live untracked torrent "
                            "(%s); leaving the scope at 'searching' for the auto-grab "
                            "worker to retry",
                            safe_text(request.title),
                            type(exc).__name__,
                            extra=log_extra,
                        )
                        break
                    except _GRAB_SCOPE_REFUSALS as exc:
                        # A SCOPE-level refusal (the scope now has an active download,
                        # or is terminal / mis-shaped): another candidate cannot help.
                        # Mirrors auto-grab's settle-and-leave -- discard the partial
                        # write, do NOT park (not exhaustion), and leave the scope's
                        # committed ``searching`` as-is.
                        await session.rollback()
                        park_scope = False
                        _logger.warning(
                            "report-issue re-grab for %r refused (%s); leaving the scope "
                            "for the auto-grab worker",
                            safe_text(request.title),
                            type(exc).__name__,
                            extra=log_extra,
                        )
                        break
                if park_scope:
                    # Every attempted replacement was per-release unusable (list or
                    # attempt cap exhausted): the SAME honest park an empty preview
                    # takes -- never a silent 'searching'.
                    await _park_no_acceptable(session, request_id, target.season, is_tv=is_tv)
            except _DOWNLOAD_CLIENT_ERRORS as exc:
                # OPERATIONAL client failure (qBittorrent unreachable/erroring), NOT
                # "nothing acceptable" -- releases exist; the CLIENT failed. Do NOT park
                # (that would LIE + surface a 502 after a successful correction). Roll
                # back the grab's partial write and LEAVE the scope at the ``searching``
                # already committed at (b): the auto-grab worker re-grabs it eagerly next
                # tick (ADR-0013). The re-read below then returns 'searching' (a 200).
                await session.rollback()
                _logger.warning(
                    "report-issue re-grab for %r hit the download client (%s); leaving "
                    "the scope at 'searching' for the auto-grab worker to retry",
                    safe_text(request.title),
                    type(exc).__name__,
                    extra=log_extra,
                )

    updated = await request_repo.get(request_id)
    if updated is None:  # pragma: no cover - just operated on this row
        raise RequestNotFoundError(request_id)
    return updated


async def _park_no_acceptable(
    session: AsyncSession, request_id: int, season: int | None, *, is_tv: bool
) -> None:
    """Land the request/season on the honest ``no_acceptable_release`` dead-end.

    Both branches now go through a genuine compare-and-swap (issue #72) --
    ``request_service`` / ``season_request_service.mark_no_acceptable_release``
    -- so a concurrent writer that already moved the row out of the parkable set
    (e.g. a racing grab landed it on ``downloading``) is left alone rather than
    silently regressed: the CAS's boolean return decides whether to commit this
    write, never whether to raise or retry. This function's own caller
    (``report_issue``) always re-reads the request's ACTUAL row afterward, so the
    response reflects the true state regardless of whether this particular write
    landed.
    """
    if is_tv and season is not None:
        parked = await season_request_service.mark_no_acceptable_release(
            session, media_request_id=request_id, season_number=season
        )
    else:
        parked = await request_service.mark_no_acceptable_release(session, request_id)
    if parked:
        await session.commit()
    else:
        await session.rollback()


async def cancel_request(
    session: AsyncSession,
    qbt: DownloadClientPort | None,
    *,
    request_id: int,
) -> RequestRecord:
    """Cancel a not-yet-imported request: drop active torrent(s) + settle ``cancelled``.

    Removes every active torrent this request still owns WITH its data (best-effort,
    closing the seeding leak), marks each of those download rows terminal, and flips
    the request -- and, for TV, every tracked season -- to the settled ``cancelled``
    status (kept only for history; nothing re-grabbed). Returns the updated record.

    ``qbt`` may be ``None``: a cancel for a ``pending``/``searching``/
    ``no_acceptable_release``/``waiting_for_air_date`` request with NO active download
    rows is a PURE DB settle that never touches the client, so it must still work on an
    install with qBittorrent unconfigured (the endpoint resolves it via
    ``get_qbittorrent_optional``). Active rows ARE discovered first; only if there are
    torrents to remove but ``qbt is None`` is :class:`DownloadClientRequiredError`
    raised -- BEFORE any state change (the endpoint maps it to an honest 409
    ``service_not_configured``), never a silent skip that would leak a seeding torrent.
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

    # A cancel with active rows genuinely needs qBittorrent to remove their torrents;
    # discover them FIRST (above) so a pure-DB settle with NO active rows still works
    # unconfigured, and only require the client when there is actually something to
    # remove. Raised BEFORE any state change (nothing settled, no torrent touched) so
    # the endpoint surfaces the honest 409 ``service_not_configured`` -- never a silent
    # skip that leaks a seeding torrent (see DownloadClientRequiredError).
    if active and qbt is None:
        raise DownloadClientRequiredError(request_id)

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
    removal_ids: list[int] = []
    try:
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
            await _mark_download_scopes_terminal(session, row.id, RequestStatus.cancelled.value)
            hashes_to_remove.append(row.torrent_hash)
            # #206: claim the removal as in-flight BEFORE the terminal commit below.
            # cancel commits the row to terminal ``Failed`` and only THEN removes the
            # torrent; terminality is itself what makes the row reusable, so a
            # concurrent grab's terminal-row reuse could re-own this hash in the
            # commit->delete window. Registering here (not just before the delete
            # await, as reconcile's non-terminal Phase-B does) means the instant the
            # row becomes reusably-terminal to another session it is already claimed,
            # and grab_service._reuse_terminal_row refuses. Released in the finally
            # once removal settles (or on any abort).
            queue_service.register_removal_in_flight(row.id)
            removal_ids.append(row.id)

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

        # Remove each cancelled torrent + its data AFTER the DB cancel has committed,
        # so a client hiccup never undoes the committed settle (mirrors
        # queue_service.mark_failed). Best-effort + already-gone-is-a-no-op (see
        # purge_service.remove_torrent). ``qbt is not None`` is GUARANTEED whenever
        # ``hashes_to_remove`` is non-empty (the active-rows-without-a-client guard
        # above refused that combination); the explicit check narrows the optional
        # type for the checker and is a no-op for the empty pure-DB-settle case
        # (nothing to remove, and qbt may legitimately be None). The in-flight claim
        # registered above keeps the now-terminal row un-reusable across this I/O.
        if qbt is not None:
            for torrent_hash in hashes_to_remove:
                await purge_service.remove_torrent(
                    qbt,
                    torrent_hash,
                    context="a cancel",
                    extra={"torrent_hash": torrent_hash, "request_id": safe_int(request_id)},
                )
    finally:
        # Removal has settled (or the cancel aborted): the row is either gone from the
        # client (a later grab creates a fresh torrent) or, on a removal failure, its
        # data is intact and reuse is safe again. Release every claim we registered,
        # including any registered before an ImportInProgressError abort above.
        for download_id in removal_ids:
            queue_service.release_removal_in_flight(download_id)

    updated = await request_repo.get(request_id)
    if updated is None:  # pragma: no cover - just operated on this row
        raise RequestNotFoundError(request_id)
    return updated


async def cancel_request_as_owner(
    session: AsyncSession,
    qbt: DownloadClientPort | None,
    *,
    request_id: int,
    user_id: int,
) -> RequestRecord:
    """``POST /cancel`` for a non-admin owner (issue #314).

    ``POST /cancel``'s contract is unchanged for a SOLE participant: cancel the
    whole request via :func:`cancel_request` (teardown + settle ``cancelled``),
    keeping the caller's own subscription so they see the ``cancelled`` row as
    history -- exactly like the admin path.

    But when OTHER subscribers still hold this request, a non-admin owner
    hard-cancelling the whole row would silently kill a download co-participants
    still want. Refused UP FRONT (before anything is touched) as
    :class:`HasOtherParticipantsError` -- the correction path for that case is
    collaborative self-removal (:func:`withdraw_participant` via
    ``DELETE /subscription``), which hands ownership off instead.
    """
    request_repo = SqlRequestRepository(session)
    request = await request_repo.get(request_id)
    if request is None:
        raise RequestNotFoundError(request_id)
    subscribers = await request_repo.list_subscribers(request_id)
    if any(uid != user_id for uid in subscribers):
        raise HasOtherParticipantsError(request_id)
    return await cancel_request(session, qbt, request_id=request_id)


@dataclass(frozen=True)
class WithdrawOutcome:
    """Result of :func:`withdraw_participant` (issue #314).

    ``settled`` tells the caller (the router, choosing an SSE ``reason``)
    whether this withdrawal was a mere subscription removal (``False`` -- no
    torrent/file touched) or the final-settle branch that reused
    :func:`cancel_request`'s teardown (``True``).
    """

    record: RequestRecord
    settled: bool


async def withdraw_participant(
    session: AsyncSession,
    qbt: DownloadClientPort | None,
    *,
    request_id: int,
    user_id: int,
) -> WithdrawOutcome:
    """Remove ``user_id``'s own subscription from a shared request (issue #314).

    The collaborative counterpart to ``cancel_request_as_owner``'s hard cancel:
    a participant (owner or subscriber) may always remove THEMSELVES without
    touching what remaining participants still want. Three cases, driven by who
    else remains subscribed and (for the last participant) whether the request
    is still in a not-yet-imported ("cancellable") state:

    * **Others remain** -- a mere subscription removal. No torrent/file touched;
      remaining participants keep whatever is in flight. If the withdrawing user
      was the OWNER (``MediaRequest.user_id``), ownership hands off to the
      earliest-subscribed remaining participant (:meth:`SqlRequestRepository.
      list_subscribers`'s ``subscribed_at ASC, user_id ASC`` order) -- their
      admin-only mutation rights (report/pin/cancel) follow automatically.
      ``user_id`` is the CURRENT owner functionally already (``claim_if_unowned``
      mutates it on ownerless-dedup claims), so reassigning it here is
      consistent with existing behavior, not a new hazard.

    * **Last participant, cancellable status** -- withdrawing would otherwise
      orphan a live download nobody wants. Reuses :func:`cancel_request`
      VERBATIM (torrent removal, the TV per-season guard, and its
      :class:`ImportInProgressError` / :class:`DownloadClientRequiredError`
      refusals all apply unchanged and propagate uncaught), then removes the
      subscription and -- if the withdrawing user was the owner -- nulls
      ``user_id`` out: the row ends ``cancelled``, ownerless, zero subscribers.

    * **Last participant, ACTIVE non-cancellable status** (``import_blocked`` /
      ``partially_available``) -- refused with
      :class:`WithdrawalBlockedActiveError` (409). These are the two statuses in
      NEITHER the cancellable NOR the terminal set: still dedup-blocking, but not
      safely tearable-down. Stranding such a row ownerless with zero subscribers
      would leave nobody to correct it, so the caller must resolve the import (or
      let the in-flight seasons settle) first, then withdraw from the terminal
      row.

    * **Last participant, terminal/settled status**
      (``available``/``completed``/``failed``/``evicted``/``cancelled`` --
      ``request_service.TERMINAL_REQUEST_STATUS_VALUES``) -- nothing to tear
      down. The subscription is removed and, if the withdrawing user was the
      owner, ``user_id`` is nulled out -- the row becomes ownerless, exactly like
      automation provenance. The library file is untouched.

    Every branch writes an ``AuditLog`` row (via :mod:`audit_service`) naming
    the withdrawing user, honesty over silence: a handoff is never a silent
    mutation of who owns the request -- see the extra ``request.owner_handoff``
    row the first case writes, naming both the old and new owner.
    """
    request_repo = SqlRequestRepository(session)
    request = await request_repo.get(request_id)
    if request is None:
        raise RequestNotFoundError(request_id)

    # Serialize this withdrawal per-media (issue #314). Take the SAME
    # per-``(tmdb_id, media_type)`` dedup lock ``request_service.create_request``
    # holds (``acquire_media_lock``): two concurrent withdrawals -- or a
    # withdrawal racing a create -- must not both read the pre-removal subscriber
    # snapshot and hand ownership to a user the OTHER just removed, which would
    # leave an active row owned by a non-participant with zero subscribers.
    # ``tmdb_id``/``media_type`` are immutable, so reading them off the
    # (possibly stale) identity-map row above is safe. Lock ordering is
    # deadlock-free: like ``create_request`` this takes the media lock BEFORE any
    # MediaRequest/Download row lock (incl. the teardown branch's
    # ``cancel_request``), so both paths acquire the single media lock first.
    # Under SQLite (ADR-0007, single-writer) ``FOR UPDATE`` is a no-op so the
    # race is theoretical today; the lock keeps it correct under PostgreSQL,
    # where the primitive is real.
    await request_repo.acquire_media_lock(request.tmdb_id, request.media_type)

    # Re-read the row AUTHORITATIVELY under the lock (issue #314). The authz
    # dependency (``_require_subscriber``) already loaded this MediaRequest into
    # the request-scoped session's identity map, possibly BEFORE an import
    # settled it; ``get_fresh`` (``populate_existing=True``) forces a real SELECT
    # so every branch below decides on the CURRENT status/owner, never a stale
    # pre-import snapshot (which could otherwise settle an already-imported row
    # ``cancelled``). ``list_subscribers`` below is a plain SELECT and is
    # likewise fresh; because BOTH reads happen under the media lock, a
    # serialized second withdrawal sees the first's committed subscriber removal
    # and ownership handoff.
    request = await request_repo.get_fresh(request_id)
    if request is None:  # pragma: no cover - the locked row cannot vanish
        raise RequestNotFoundError(request_id)

    subscribers = await request_repo.list_subscribers(request_id)
    others = [uid for uid in subscribers if uid != user_id]

    if others:
        # Mere removal (+ handoff if the withdrawing user is the current owner).
        # No teardown: remaining participant(s) keep whatever is in flight.
        await request_repo.remove_subscriber(request_id, user_id)
        await audit_service.record(
            session,
            actor_user_id=user_id,
            action_type="request.participant_withdrawn",
            entity_type="media_request",
            entity_id=request_id,
            description=f"user {user_id} withdrew from request {request_id}",
        )
        if request.user_id == user_id:
            new_owner = others[0]  # earliest-subscribed remaining (list order)
            await request_repo.set_owner(request_id, new_owner)
            await audit_service.record(
                session,
                actor_user_id=new_owner,
                action_type="request.owner_handoff",
                entity_type="media_request",
                entity_id=request_id,
                old_value={"owner_user_id": user_id},
                new_value={"owner_user_id": new_owner},
                description=(
                    f"ownership of request {request_id} handed off from {user_id} "
                    f"to {new_owner} on withdrawal"
                ),
            )
        await session.commit()
        updated = await request_repo.get(request_id)
        if updated is None:  # pragma: no cover - just operated on this row
            raise RequestNotFoundError(request_id)
        return WithdrawOutcome(record=updated, settled=False)

    if request.status in CANCELLABLE_REQUEST_STATUS_VALUES:
        # Last participant, not-yet-imported: withdrawing would otherwise orphan
        # a live download nobody wants. cancel_request's own refusals
        # (ImportInProgressError / DownloadClientRequiredError / the TV
        # per-season NotCancellableError guard) propagate uncaught -- nothing is
        # removed below if it raises.
        await cancel_request(session, qbt, request_id=request_id)
        await request_repo.remove_subscriber(request_id, user_id)
        if request.user_id == user_id:
            await request_repo.set_owner(request_id, None)
        await audit_service.record(
            session,
            actor_user_id=user_id,
            action_type="request.participant_withdrawn",
            entity_type="media_request",
            entity_id=request_id,
            description=f"user {user_id} withdrew from request {request_id}",
        )
        await session.commit()
        updated = await request_repo.get(request_id)
        if updated is None:  # pragma: no cover - just operated on this row
            raise RequestNotFoundError(request_id)
        return WithdrawOutcome(record=updated, settled=True)

    if request.status not in request_service.TERMINAL_REQUEST_STATUS_VALUES:
        # Last participant on an ACTIVE non-cancellable status -- the two
        # statuses that are in NEITHER CANCELLABLE_REQUEST_STATUS_VALUES nor
        # TERMINAL_REQUEST_STATUS_VALUES: ``import_blocked`` (a blocked download
        # awaiting a retry/report) and ``partially_available`` (a TV rollup with
        # seasons still in flight). Both keep shadowing a duplicate via
        # ``uq_media_requests_active``, and neither can be safely torn down here.
        # Removing the last participant would strand an active, dedup-blocking
        # row ownerless with zero subscribers -- nobody left to correct it.
        # Refuse (nothing touched); the caller resolves the block / lets the
        # seasons settle first, then withdraws from the resulting terminal row
        # (see WithdrawalBlockedActiveError).
        raise WithdrawalBlockedActiveError(request_id, request.status)

    # Last participant, terminal/settled (available/completed/failed/evicted/
    # cancelled): nothing to tear down. Remove the subscription and, if the
    # withdrawing user owned it, null the owner out -- an ownerless row like any
    # other automation-provenance one. The library file is untouched.
    await request_repo.remove_subscriber(request_id, user_id)
    if request.user_id == user_id:
        await request_repo.set_owner(request_id, None)
    await audit_service.record(
        session,
        actor_user_id=user_id,
        action_type="request.participant_withdrawn",
        entity_type="media_request",
        entity_id=request_id,
        description=f"user {user_id} withdrew from request {request_id}",
    )
    await session.commit()
    updated = await request_repo.get(request_id)
    if updated is None:  # pragma: no cover - just operated on this row
        raise RequestNotFoundError(request_id)
    return WithdrawOutcome(record=updated, settled=False)


async def relocate_stranded_download(
    session: AsyncSession,
    qbt: DownloadClientPort,
    *,
    download_id: int,
    downloads_host_root: str,
) -> DownloadRecord:
    """Relocate an import-blocked, path-invisible download into the mounted
    downloads root (issues #133/#157), then leave it retryable so the operator's
    existing "Retry import" (``POST /queue/{id}/import``) picks it up once
    qBittorrent settles the (async) move.

    Scoped EXACTLY to the honest "download path not visible inside the container"
    block :mod:`~plex_manager.services.import_service` stamps when qBittorrent's
    reported content sits outside every mounted ``/downloads`` bind --
    :class:`NotRelocatableError` for any other row (including a DIFFERENT
    ``import_blocked`` reason, e.g. a genuinely bad/wrong-media file, which
    relocating could never fix).

    Root-guarded: the ONLY destination this function will ever hand qBittorrent is
    ``downloads_host_root`` -- the app's OWN derived downloads root
    (``path_visibility.resolve_downloads_host_root``), never an operator- or
    caller-supplied path (there is no path parameter to override). When that root
    could not be derived (``PLEX_MANAGER_DOWNLOADS_ROOT`` unset -- bare metal, no
    Docker split), :class:`DownloadsRootUnavailableError` refuses rather than
    relocating into a guessed/empty location.

    qBittorrent moves content ASYNCHRONOUSLY: :meth:`~plex_manager.ports.
    download_client.DownloadClientPort.set_location` only REQUESTS the move and
    returns -- this function does not wait for, or otherwise verify, completion
    (wait-free, honest: the caller sees the request as ACCEPTED, never as
    already-imported). A ``QbittorrentError`` from that call propagates UNCAUGHT
    (never swallowed) so the operator sees the real client failure rather than a
    falsely "accepted" relocation.

    The row is left ``import_blocked`` -- still retryable by the SAME existing
    import-retry endpoint (``import_download``'s ``_RESUMABLE`` set already
    includes ``import_blocked``) -- but its ``failed_reason`` is refreshed to say a
    relocation was requested, so the operator sees the CURRENT truth instead of the
    stale pre-relocation "not visible" message while the move is in flight. A retry
    attempted before the move settles simply re-blocks (honestly) with a fresh "not
    visible" reason if qBittorrent has not finished moving the content yet.

    The terminal write is a compare-and-swap gated on BOTH the row's status
    still being ``import_blocked`` AND its ``failed_reason`` still being the
    EXACT value observed at entry (:class:`RelocationSupersededError` on a
    miss). Gating on status alone would let a concurrent "Retry import" that
    re-blocks the row -- still ``import_blocked``, but with a NEWER, different
    diagnosis (e.g. "no video file found") -- get silently clobbered by this
    function's stale "relocation requested" message, discarding the fresher,
    truthful reason. The relocation is still requested of qBittorrent either
    way; only which message the row ends up carrying is at stake.
    """
    if not downloads_host_root:
        raise DownloadsRootUnavailableError(download_id)
    row = await session.get(Download, download_id)
    if row is None:
        raise DownloadNotFoundError(download_id)
    if row.status != DownloadState.ImportBlocked.value or not (row.failed_reason or "").startswith(
        PATH_NOT_VISIBLE_REASON_PREFIX
    ):
        raise NotRelocatableError(download_id, row.status)

    torrent_hash = row.torrent_hash
    observed_reason = row.failed_reason
    # qBittorrent errors propagate uncaught (honesty over silence): the operator sees
    # the real client failure rather than a falsely "accepted" relocation.
    await qbt.set_location(torrent_hash, downloads_host_root)

    download_repo = SqlDownloadRepository(session)
    moved = await download_repo.update_status_if_in(
        download_id,
        DownloadState.ImportBlocked.value,
        frozenset({DownloadState.ImportBlocked.value}),
        failed_reason=(
            f"relocation to {downloads_host_root} requested; retry the import once "
            "qBittorrent finishes moving the content"
        ),
        require_failed_reason=observed_reason,
    )
    if not moved:
        # The row was re-blocked (or otherwise changed) with a DIFFERENT reason
        # than the one observed at entry while ``qbt.set_location`` was in
        # flight -- e.g. a concurrent Retry Import's fresher, genuinely
        # different diagnosis. The relocate already happened on qBittorrent's
        # side, but overwriting the row's message now would clobber that newer
        # truth with our stale "relocation requested" text -- surface it
        # honestly instead of silently accepting whichever state won.
        await session.rollback()
        current = await session.get(Download, download_id)
        raise RelocationSupersededError(
            download_id, current.failed_reason if current is not None else None
        )

    await session.commit()
    updated = await download_repo.get_by_hash(torrent_hash)
    if updated is None:  # pragma: no cover - just operated on this row
        raise LookupError(f"download for hash {torrent_hash} vanished mid-relocate")
    return updated
