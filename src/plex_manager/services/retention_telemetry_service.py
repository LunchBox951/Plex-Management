"""Retention telemetry sweep — a DELETE-NOTHING periodic observer (ADR-0012
follow-up, beta blueprint "PR 6: retention telemetry sweep").

For a one-week unattended beta on one machine, disk-pressure eviction
(``domain/eviction.py`` + ``services/eviction_service.py``) may never actually
fire — a roomy disk simply never crosses ``disk_pressure_threshold_percent``.
That is the CORRECT behaviour (eviction is pressure-triggered, never automatic
just because content aged past grace), but it also means the operator learns
nothing about what a real cleanup policy would need to look like: how many
titles WOULD be evicted, how much space that would actually free, and how long
after a title finishes importing someone actually gets around to watching it.

This module answers exactly that, without touching a single byte on disk or a
single database row's status: on the SAME periodic tick the eviction loop
already runs (``web/app.py``'s ``_eviction_loop`` — no new scheduler), whenever
that tick finds a root's disk usage BELOW the pressure threshold (so the real
eviction sweep would have evicted nothing anyway), it assembles the root's
candidates ONCE (:func:`~plex_manager.services.eviction_service.
assemble_candidates` — the RAW set, no grace or eligibility filter) and logs
two DISTINCT products from that one read.

The two products are deliberately DECOUPLED, because they answer different
questions and no single filter serves both:

* The **would-evict aggregate** answers "what would a pressure sweep delete",
  and reports TWO honestly-distinct numbers so it never overstates:

  - ``eligible_count`` / ``eligible_bytes`` — the FULL set of ever-evictable
    candidates at the real ``grace_days`` cutoff
    (:func:`~plex_manager.domain.eviction.rank_eviction_candidates`): every
    watched, past-grace, unpinned, not-in-flight title/season, regardless of
    how much space a sweep would actually need to free.
  - ``would_evict_count`` / ``would_evict_bytes`` — the SUBSET a pressure sweep
    would actually delete to relieve pressure, and how much it would ACTUALLY
    free. This REUSES ``run_eviction_sweep``'s own two-phase selection rather than
    reimplementing it: the stalest-first
    :func:`~plex_manager.domain.eviction.select_evictions` prefix down from the
    configured ``threshold_pct`` toward ``target_pct``, THEN the reclaimable-aware
    extension (R4-6) that keeps drawing further stalest-first candidates while the
    MEASURED reclaimable bytes fall short of the target. Both the sweep and this
    simulation gate that extension on the SAME
    :func:`~plex_manager.domain.eviction.pressure_relieved` predicate, so the two
    can never drift. Without the extension the count/bytes would understate a real
    sweep whenever content is hardlinked: a same-filesystem import frees far less
    than its nominal size, so a nominal-size ``select_evictions`` prefix stops too
    early and the real sweep goes further. It is simulated AT the threshold (the
    moment pressure first fires), NOT at the current below-threshold usage — the
    sweep only runs while usage is below threshold, where ``select_evictions``
    correctly selects nothing, so a current-usage simulation would always report
    zero and tell the operator nothing. Still always a prefix of the eligible set
    (the extension only draws consecutive stalest-first candidates from it), so
    ``would_evict_count <= eligible_count`` by construction.

* The **time-to-watch dataset** (the idle-age distribution + one
  completed_at→last-watch interval per title/season) answers "how long after a
  title finishes importing does someone actually get around to watching it".
  It is built from EVERY row with ANY recorded Plex view
  (``last_viewed_at is not None``) — which deliberately INCLUDES a
  started-but-unfinished season (``watched=False`` but a view timestamp exists):
  that "began watching" signal is exactly what this dataset exists to capture,
  and the eligibility filter behind the would-evict numbers would drop it. It
  must also cover titles of any idle age, or it captures nothing during the
  one-week beta this sweep exists to serve (in a seven-day window NO title can
  have been idle past a 30-day grace, so a grace-filtered read would return
  ``[]`` on every tick and the whole primary dataset — plus the pre-grace
  <7d/7-14d/14-30d buckets — would be permanent dead weight).

The single raw read feeds all of this: ``rank_eviction_candidates`` /
``select_evictions`` re-derive the would-evict subset in memory over the same
rows (no second Plex pass and no second full-library ``os.walk``), and the
time-to-watch set is a plain ``last_viewed_at is not None`` filter over them. The
ONE extra disk touch is a read-only, hardlink-aware
:meth:`~plex_manager.ports.filesystem.FileSystemPort.reclaimable_bytes` stat over
ONLY the would-evict subset (never every candidate) — the price of an honest,
hardlink-aware ``would_evict`` that matches the real sweep; it never deletes. A
second :func:`~plex_manager.services.eviction_service.preview_candidates` call, by
contrast, would re-run every title's fresh Plex ``watch_state`` AND a full
``os.walk`` on every below-pressure tick — exactly the redundant cost
``run_eviction_sweep``'s own pressure pre-check goes out of its way to avoid.
Nothing here calls ``fs.delete``, flips a status, or writes ``download_history``;
the candidate list is read-only input to the ``logging`` calls.

Three classes of row are EXCLUDED from the metrics they would otherwise corrupt
and instead reported as labelled counts on the per-root aggregate -- honest
"these exist, here is why we set them aside", never a silent drop:

* ``no_path_count`` -- a row with NO ``library_path`` breadcrumb (e.g. a movie
  found already in Plex, short-circuited straight to ``available`` by
  ``request_service.create_request``: ``mark_available`` stamps a ``completed_at``
  that is the Plex-verification moment, not an import time, and no file of ours
  was ever placed). Eviction can never touch it, so it is dropped from the
  eligible/would-evict metrics AND the time-to-watch dataset.
* ``guard_refused_count`` -- a would-evict candidate whose breadcrumb is lexically
  under ``root_path`` but resolves, via a symlinked component, OUTSIDE every
  configured root. The real ``LocalFileSystem.delete`` refuses it and frees
  nothing, so the simulation runs each would-evict candidate through ``fs``'s OWN
  delete guard (:meth:`~plex_manager.ports.filesystem.FileSystemPort.
  delete_guard_refuses`, the very predicate ``delete`` raises on) and excludes a
  refused row from the would-evict count/bytes.
* ``preexisting_watch_count`` -- a row whose recorded view PREDATES its completion
  (a re-imported / previously-watched title keeps Plex's older ``last_viewed_at``),
  which would yield a negative ``completed_to_last_watch``. The per-title interval
  measures POST-import time-to-watch, and a pre-import view has no such interval,
  so that per-title row is skipped.

The per-title ``completed_to_last_watch`` rows are de-duplicated in-process (see
:data:`_last_emitted_watch`): a title's row is emitted only when its
``last_viewed_at`` advances (or the process has not seen it yet), so a steady
watch state does not re-log an identical row every 30-minute tick. The per-root
aggregate is the time-series and always emits.

The interval is labelled ``completed_to_last_watch`` (not "first watch"):
``last_viewed_at`` is Plex's MOST-RECENT play, not the first, so for a rewatched
title it is a watch-RECENCY measure, not a true time-to-first-watch. Plex does
not expose first-watch cheaply, so the dataset names what it actually measures
rather than a value it cannot honestly derive.

Logged to a DEDICATED logger (:data:`~plex_manager.services.
log_capture_service.TELEMETRY_LOGGER_NAME`) so the durable ``log_events`` sink's
ordinary retention sweep (``log_retention_days``, default 7 days) can spare
these rows on their own longer cutoff — see that constant's docstring in
``log_capture_service`` for the full "why a dedicated retention, not a
schema change" rationale. Every id (``request_id``/``tmdb_id``) is passed via
``extra={...}``, never interpolated into the message string (the same
CodeQL-safe convention every other log call site in this codebase follows).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final, Literal

from plex_manager.domain.eviction import (
    pressure_relieved,
    rank_eviction_candidates,
    select_evictions,
)
from plex_manager.models import MediaRequest, SeasonRequest
from plex_manager.services import eviction_service
from plex_manager.services.health_service import read_disk_usage
from plex_manager.services.log_capture_service import TELEMETRY_LOGGER_NAME

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from plex_manager.domain.eviction import EvictionCandidate
    from plex_manager.ports.filesystem import FileSystemPort
    from plex_manager.ports.library import LibraryPort

__all__ = ["run_retention_telemetry_sweep"]

# Constructed from the constant ``log_capture_service`` owns (not ``__name__``)
# so the two stay coupled by import, never by two independently-typed string
# literals that could silently drift apart if either module is ever renamed.
#
# This logger's LEVEL is pinned to INFO independently of the operator's
# ``config.log_level`` -- see ``log_capture_service.configure_logging``, which
# does the ``setLevel(logging.INFO)`` at wiring time. Without that, an operator
# running at WARNING/ERROR would have every INFO telemetry record filtered by
# this (child) logger's inherited effective level BEFORE the LogCaptureHandler
# on the root logger ever saw it, and the whole beta dataset would silently
# never persist. Pinning THIS logger (not the root) lets telemetry INFO through
# to the durable ``log_events`` sink at any operator floor without also
# un-quieting the rest of the app's INFO chatter.
_logger = logging.getLogger(TELEMETRY_LOGGER_NAME)


#: In-process dedupe for the per-title ``completed_to_last_watch`` rows, keyed by
#: ``(tmdb_id, media_type, season)`` -> the ``last_viewed_at`` last emitted for
#: that title/season. Without it, a below-pressure install re-emits an IDENTICAL
#: per-title row on EVERY eviction tick (the default 30-minute interval ~= 48
#: duplicate rows/day/title), flooding ``log_events`` and skewing any analysis of
#: the dataset. A per-title row is emitted only when its ``last_viewed_at`` has
#: actually ADVANCED since the last emission (someone watched it again) or the
#: key has not been seen since this process started -- so a steady watch state
#: contributes exactly one row, and a new play contributes exactly one more.
#:
#: Deliberately process-lifetime, not durable: a restart re-emits each watched
#: title's current row ONCE (the cache starts empty). That is an accepted,
#: honest cost -- one duplicate per title per restart is negligible next to one
#: per title per 30 minutes, and persisting a dedupe watermark would need a
#: schema change this delete-nothing observer explicitly avoids. The per-ROOT
#: aggregate row is NOT deduped here: it is the time-series this sweep exists to
#: produce and must emit every sweep. Bounded by the count of distinct
#: ever-watched titles/seasons -- a beta library's worth, never unbounded.
_last_emitted_watch: dict[tuple[int | None, str, int | None], datetime] = {}


async def _reclaimable_bytes(fs: FileSystemPort, candidate: EvictionCandidate) -> int:
    """Best-effort hardlink-aware reclaimable footprint of ``candidate`` in bytes.

    Delegates to :meth:`~plex_manager.ports.filesystem.FileSystemPort.
    reclaimable_bytes` (read-only -- it never deletes, only stats link counts) so
    the would-evict simulation projects against the bytes a real sweep would
    ACTUALLY free, not the nominal on-disk size (a same-filesystem hardlinked
    import frees far less -- often nothing). Offloaded like every other blocking
    FS primitive this subsystem calls from an async function. A candidate with no
    stored ``library_path`` breadcrumb, or any FS error, contributes ``0`` --
    mirroring ``_evict_one``'s own "nothing reclaimed" fallback for the same cases
    (so a no-breadcrumb candidate the real sweep would attempt-and-skip is counted
    as freeing nothing here too, never guessed at).
    """
    library_path = candidate.library_path
    if library_path is None:
        return 0
    try:
        return await asyncio.to_thread(fs.reclaimable_bytes, library_path)
    except OSError:
        return 0


async def _guard_refuses(fs: FileSystemPort, candidate: EvictionCandidate) -> bool:
    """Whether a real sweep's ``fs.delete`` would REFUSE this candidate's breadcrumb
    as resolving outside every configured library root.

    Shares ``delete``'s own containment predicate
    (:meth:`~plex_manager.ports.filesystem.FileSystemPort.delete_guard_refuses`) so
    the would-evict simulation never counts, as freeable, bytes a real sweep would
    decline to touch -- a breadcrumb that is LEXICALLY under ``root_path`` but
    resolves, via a symlinked path component, outside every configured root: the
    real ``LocalFileSystem.delete`` raises and frees nothing, so this simulation
    must exclude it too (and never drift from that guard). Offloaded like every
    other blocking FS primitive here -- realpath resolution stats each path
    component, which can stall on a hung mount. A candidate with no breadcrumb is a
    SEPARATE concern already excluded up front (see ``no_path_count``), never a
    guard refusal, so it reports ``False``."""
    library_path = candidate.library_path
    if library_path is None:
        return False
    return await asyncio.to_thread(fs.delete_guard_refuses, library_path)


def _sum_estimated_bytes(candidates: Sequence[EvictionCandidate], total_bytes: int) -> int:
    """Summed estimated on-disk footprint of ``candidates`` in bytes.

    Each candidate's ``size_percent`` is its share of THIS root's total capacity
    (see :class:`~plex_manager.domain.eviction.EvictionCandidate`), so bytes are
    reconstructed with this root's real ``total_bytes`` -- the same estimate
    basis ``eviction_service`` itself uses before a delete. A non-positive
    ``total_bytes`` (an unreadable/empty root) yields ``0`` rather than a bogus
    figure.
    """
    if total_bytes <= 0:
        return 0
    return sum(round(candidate.size_percent / 100.0 * total_bytes) for candidate in candidates)


# Idle-age (now - last_viewed_at) buckets for the per-root distribution, taken
# over EVERY title with any recorded view (watched OR started-but-unfinished),
# not just the would-evict subset -- so the pre-grace buckets actually populate
# during the beta's own sub-30-day window. The last boundary is implicit (anything >= the
# final explicit boundary falls into the final label) -- see _bucket_label.
# Chosen to bracket the beta's own default 30-day eviction_grace_days:
# <7d/7-14d/14-30d cover the pre-grace range (the primary week-1 time-to-watch
# signal), 30-60d/60-90d/90d+ cover how far PAST grace watched content tends to
# sit unevicted absent disk pressure -- exactly the shape needed to judge
# whether a proactive (non-pressure) sweep is worth turning on.
_AGE_BUCKET_BOUNDARIES_DAYS: Final[tuple[float, ...]] = (7.0, 14.0, 30.0, 60.0, 90.0)
_AGE_BUCKET_LABELS: Final[tuple[str, ...]] = ("<7d", "7-14d", "14-30d", "30-60d", "60-90d", "90d+")


def _bucket_label(age_days: float) -> str:
    """Return which :data:`_AGE_BUCKET_LABELS` bucket ``age_days`` falls into.

    A negative ``age_days`` (a clock skew / ``last_viewed_at`` briefly in the
    future) still lands in the first bucket rather than raising — this is an
    observational sweep, never a hard failure over a borderline timestamp.
    """
    for boundary, label in zip(_AGE_BUCKET_BOUNDARIES_DAYS, _AGE_BUCKET_LABELS, strict=False):
        if age_days < boundary:
            return label
    return _AGE_BUCKET_LABELS[-1]


def _format_buckets(counts: Mapping[str, int]) -> str:
    return ", ".join(f"{label}={counts[label]}" for label in _AGE_BUCKET_LABELS)


def _as_utc(value: datetime) -> datetime:
    """Coerce a stored timestamp to tz-aware UTC.

    SQLite returns naive datetimes even for ``DateTime(timezone=True)`` columns
    (every value this module reads back -- ``MediaRequest.completed_at`` -- was
    originally written as UTC), so a naive value straight off the ORM must be
    re-attached to UTC before it is subtracted against ``candidate.
    last_viewed_at`` (always tz-aware, from ``LibraryPort.watch_state``) --
    otherwise that subtraction raises ``TypeError``. Mirrors ``repositories.
    log_events``/``repositories.downloads``'s identically-named helper.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


@dataclass(frozen=True)
class _CandidateContext:
    """Identifiers + a completion reference time for one candidate, resolved
    once per candidate (see :func:`_candidate_context`)."""

    media_request_id: int
    tmdb_id: int | None
    completed_at: datetime | None


async def _candidate_context(
    session: AsyncSession, candidate: EvictionCandidate
) -> _CandidateContext:
    """Resolve the owning ``MediaRequest`` id/``tmdb_id``, plus a completion
    reference timestamp, for one eviction candidate.

    Movies: ``candidate.request_id`` IS the ``MediaRequest`` id, and
    ``MediaRequest.completed_at`` is this exact title's own import-completion
    timestamp (stamped by ``mark_completed``/``mark_available`` at import time)
    -- an exact reference.

    TV: ``candidate.request_id`` is the ``SeasonRequest`` id. ``SeasonRequest``
    carries no completion timestamp of its own (ADR-0012 never added one, and a
    delete-nothing telemetry sweep is not the place to add a migration for it —
    see the module docstring's "no schema change" constraint), so
    ``completed_at`` here is the PARENT SHOW's ``MediaRequest.completed_at``:
    exact for a single-season show, an honest APPROXIMATION for a later season
    of a multi-season show (it reflects whenever the show's FIRST tracked
    season completed, not necessarily this one). This is a documented, known
    limitation, not a silently assumed precision -- if week-1 telemetry shows
    this caveat matters for the analysis, a follow-up can add a per-season
    ``completed_at`` column with its own migration.

    A missing row (deleted out from under a slow-running sweep) resolves to
    ``completed_at=None`` and ``tmdb_id=None`` -- the caller logs "unknown"
    rather than guessing, mirroring every other honest-fallback pattern in
    ``eviction_service``.
    """
    if candidate.media_type == "movie":
        movie = await session.get(MediaRequest, candidate.request_id)
        if movie is None:
            return _CandidateContext(
                media_request_id=candidate.request_id, tmdb_id=None, completed_at=None
            )
        completed_at = _as_utc(movie.completed_at) if movie.completed_at is not None else None
        return _CandidateContext(
            media_request_id=movie.id, tmdb_id=movie.tmdb_id, completed_at=completed_at
        )

    season = await session.get(SeasonRequest, candidate.request_id)
    if season is None:
        return _CandidateContext(
            media_request_id=candidate.request_id, tmdb_id=None, completed_at=None
        )
    parent = await session.get(MediaRequest, season.media_request_id)
    if parent is None:
        return _CandidateContext(
            media_request_id=season.media_request_id, tmdb_id=None, completed_at=None
        )
    completed_at = _as_utc(parent.completed_at) if parent.completed_at is not None else None
    return _CandidateContext(
        media_request_id=parent.id, tmdb_id=parent.tmdb_id, completed_at=completed_at
    )


async def run_retention_telemetry_sweep(
    *,
    session: AsyncSession,
    library: LibraryPort,
    fs: FileSystemPort,
    media_type: Literal["movie", "tv"],
    root_path: str,
    grace_days: int,
    threshold_pct: float,
    target_pct: float,
    now: datetime | None = None,
) -> None:
    """Log what an eviction sweep of ``root_path`` WOULD do — never deletes,
    never flips a status, never writes ``download_history``.

    Intended caller: ``web/app.py``'s ``_eviction_tick``, only for a root whose
    disk usage is currently BELOW ``disk_pressure_threshold_percent`` (i.e. the
    pressure gate did NOT fire this tick, so the real sweep evicted nothing
    anyway) — see that function's docstring for the exact gating. Calling this
    unconditionally would be harmless (it is delete-nothing either way) but
    redundant: when pressure DID fire, the real sweep's own outcome log already
    tells the operator what happened.

    A SINGLE raw candidate read (:func:`~plex_manager.services.eviction_service.
    assemble_candidates` — every available title/season with fresh watch state +
    walked size, NO grace or eligibility filter) feeds two DECOUPLED products
    (see the module docstring for the full rationale); the would-evict subset is
    re-derived from that superset in memory with no second Plex pass and no second
    full-library walk. ``fs`` is used ONLY for read-only queries -- a hardlink-aware
    :meth:`~plex_manager.ports.filesystem.FileSystemPort.reclaimable_bytes` stat
    over the would-evict subset (so the would-evict numbers match a real sweep's
    hardlink accounting) and its :meth:`~plex_manager.ports.filesystem.
    FileSystemPort.delete_guard_refuses` containment check (so a symlink-escaping
    breadcrumb a real delete would refuse is excluded, not counted as freeable),
    never to delete.

    Log calls, all through the dedicated ``TELEMETRY_LOGGER_NAME`` logger:

    1. ONE aggregate event reporting, honestly split:
       * ``eligible_count`` / ``eligible_bytes`` — every ever-evictable candidate
         at the real ``grace_days`` cutoff (:func:`~plex_manager.domain.eviction.
         rank_eviction_candidates`); bytes from each candidate's ``size_percent``
         and THIS root's real total capacity (the nominal on-disk footprint).
       * ``would_evict_count`` / ``would_evict_bytes`` — the subset a pressure
         sweep would actually delete to relieve ``threshold_pct``->``target_pct``,
         and how much it would ACTUALLY free. Reuses ``run_eviction_sweep``'s
         two-phase selection (the :func:`~plex_manager.domain.eviction.
         select_evictions` prefix plus the reclaimable-aware R4-6 extension, gated
         on the shared :func:`~plex_manager.domain.eviction.pressure_relieved`
         predicate), simulated AT the threshold; ``would_evict_bytes`` is the
         MEASURED hardlink-aware reclaimable total, not the nominal size. Still a
         prefix of the eligible set.
       * the count of titles with ANY recorded view and the watch-idle age
         distribution (now - ``last_viewed_at``, bucketed) over them.
       * ``no_path_count`` / ``guard_refused_count`` / ``preexisting_watch_count``
         -- rows set aside from the metrics above (no breadcrumb, delete-guard
         refusal, a view predating completion), reported so the dataset shows they
         exist without either metric being overstated (see the module docstring).
    2. One event PER title/season with any recorded view — INCLUDING a
       started-but-unfinished one (``watched=False`` but a view timestamp
       exists): its completed_at -> last_viewed_at interval (labelled
       ``completed_to_last_watch`` — ``last_viewed_at`` is Plex's most-recent
       play, so this is a watch-recency, not a true time-to-first-watch; see
       :func:`_candidate_context` for what "completed_at" means per media kind),
       with ``request_id``/``tmdb_id`` passed via ``extra={}`` (never
       interpolated into the message). De-duplicated in-process: emitted only when
       a title's ``last_viewed_at`` advances since the last emission (see
       :data:`_last_emitted_watch`), so a steady watch state does not re-log the
       same row every tick. The aggregate above always emits.

    An unreadable ``root_path`` (missing mount, permission denied) skips the
    whole sweep for this root, logged as a WARNING on the dedicated telemetry
    logger -- mirrors ``eviction_service.preview_candidates``'s identical
    fallback.

    ``threshold_pct``/``target_pct`` are the same configured disk-pressure
    percentages the real sweep uses (passed by the caller so both stay in lock
    step with a web-edited setting). ``now`` defaults to ``datetime.now(UTC)``;
    overridable for a deterministic idle-age distribution in tests (a fake
    clock), never used in production.
    """
    try:
        # Offloaded like every other blocking FS primitive this subsystem calls
        # from an async function (mirrors preview_candidates/run_eviction_sweep) —
        # a hung/unresponsive mount must never freeze the whole event loop.
        disk = await asyncio.to_thread(read_disk_usage, root_path)
    except OSError as exc:
        _logger.warning(
            "retention telemetry sweep skipped for %s root %s (%s)",
            media_type,
            root_path,
            type(exc).__name__,
        )
        return

    moment = now if now is not None else datetime.now(UTC)

    # ONE raw read for ALL products: every available title/season, no grace or
    # eligibility filter -- the superset from which both the would-evict subsets
    # (ranked/selected in memory) and the time-to-watch dataset (any recorded
    # view) are derived. See the module docstring for the full rationale.
    candidates = await eviction_service.assemble_candidates(
        session=session,
        library=library,
        media_type=media_type,
        root_path=root_path,
        root_total_bytes=disk.total_bytes,
    )

    # A row with NO ``library_path`` breadcrumb has nothing of OURS on disk (e.g. a
    # movie found already in Plex, short-circuited straight to ``available`` by
    # ``request_service.create_request`` -- ``mark_available`` stamps a
    # ``completed_at`` that is the Plex-verification moment, NOT an import time, and
    # no file was ever placed): eviction can NEVER touch it (``_evict_one`` skips a
    # None breadcrumb), so counting it as eligible/would-evict would overstate what
    # a sweep frees, and its ``completed_at`` is not a real import->watch reference.
    # Excluded from BOTH products below, reported honestly as ``no_path_count`` on
    # the aggregate so the dataset shows these rows EXIST without polluting either
    # metric -- never silently dropped.
    path_bearing = [candidate for candidate in candidates if candidate.library_path is not None]
    no_path_count = len(candidates) - len(path_bearing)

    grace_cutoff = moment - timedelta(days=grace_days)

    # Product 1a -- the FULL ever-evictable set at the real grace cutoff (what a
    # sweep COULD pick, regardless of how much space it would need to free).
    eligible = rank_eviction_candidates(path_bearing, grace_cutoff)

    # Product 1b -- the SUBSET a pressure sweep would ACTUALLY delete to relieve
    # pressure from the configured threshold down to the configured target,
    # stalest-first. This REUSES ``run_eviction_sweep``'s two-phase selection
    # (its ``select_evictions`` prefix + the reclaimable-aware extension) so the
    # numbers match what a real sweep does, hardlinks and all -- see the module
    # docstring's "why a reclaimable-aware simulation, not select_evictions
    # alone".
    #
    # Simulated with used_pct set to ``threshold_pct`` (the moment pressure first
    # fires), NOT the current below-threshold usage: the telemetry sweep only runs
    # BELOW threshold, where select_evictions correctly picks nothing, so
    # simulating at the real usage would always report zero and answer the wrong
    # question.
    #
    # Phase 1: the estimate-based prefix, ranked stalest-first by NOMINAL size --
    # identical to ``run_eviction_sweep``'s ``selected``. It is a prefix of
    # ``eligible`` (both come from the same stable ``rank_eviction_candidates``
    # ordering), so ``eligible[len(selected):]`` is exactly the same extension
    # pool the real sweep draws from.
    selected = select_evictions(
        path_bearing, threshold_pct, threshold_pct, target_pct, grace_cutoff
    )
    extra_pool = eligible[len(selected) :]

    # Phase 2: extend past that prefix while the MEASURED reclaimable bytes fall
    # short of the target -- the exact R4-6 behaviour ``run_eviction_sweep`` runs
    # (a hardlinked candidate frees less than its nominal size, so more must be
    # drawn). Both loops share ``domain.eviction.pressure_relieved`` as the single
    # stop condition, so this can never drift from the real sweep. Unlike the real
    # sweep (which measures reclaimable bytes LAZILY as it deletes), a
    # delete-nothing observer must measure them up front -- a read-only,
    # hardlink-aware stat pass over ONLY the would-evict subset (never the full
    # candidate set), never a delete.
    #
    # Each candidate is first run through ``fs``'s OWN delete guard
    # (:func:`_guard_refuses`): a breadcrumb lexically under ``root_path`` can still
    # resolve, via a symlinked component, OUTSIDE every configured root, where the
    # real ``LocalFileSystem.delete`` refuses it and frees nothing. Such a row is
    # excluded from ``would_evict`` count/bytes (mirroring the real sweep, whose
    # delete raises and yields no freed bytes for it) and tallied as
    # ``guard_refused_count`` -- so the would-evict figures never overstate what a
    # real sweep could touch. Phase 1 attempts every ``selected`` candidate with no
    # early stop, exactly like the real sweep; phase 2 re-checks pressure at the top
    # of each iteration, so a refused candidate simply frees nothing and the loop
    # keeps drawing -- byte-for-byte the real sweep's behaviour.
    would_evict: list[EvictionCandidate] = []
    would_free_bytes = 0
    guard_refused_count = 0
    for candidate in selected:
        if await _guard_refuses(fs, candidate):
            guard_refused_count += 1
            continue
        would_evict.append(candidate)
        would_free_bytes += await _reclaimable_bytes(fs, candidate)
    for candidate in extra_pool:
        if pressure_relieved(threshold_pct, would_free_bytes, disk.total_bytes, target_pct):
            break
        if await _guard_refuses(fs, candidate):
            guard_refused_count += 1
            continue
        would_evict.append(candidate)
        would_free_bytes += await _reclaimable_bytes(fs, candidate)

    # Product 2's population: EVERY path-bearing row with any recorded Plex view --
    # watched OR started-but-unfinished (``watched=False`` with a view timestamp).
    # The walrus narrows ``last_viewed_at`` to non-None for the rest of the sweep,
    # so the loops below never re-check it (a partial watch is exactly the "began
    # watching" signal the eligibility filter above would drop). No-path rows are
    # already excluded (``path_bearing``): their ``completed_at`` is not an import
    # time, so a completed_at->watch interval over them would be meaningless.
    watch_activity: list[tuple[EvictionCandidate, datetime]] = [
        (candidate, last_viewed)
        for candidate in path_bearing
        if (last_viewed := candidate.last_viewed_at) is not None
    ]

    bucket_counts: dict[str, int] = dict.fromkeys(_AGE_BUCKET_LABELS, 0)
    for _candidate, last_viewed in watch_activity:
        age_days = (moment - last_viewed).total_seconds() / 86400.0
        bucket_counts[_bucket_label(age_days)] += 1

    # Resolve each watched candidate's completion reference ONCE, up front: the
    # aggregate (logged first, and always) must report ``preexisting_watch_count``
    # -- rows whose recorded view PREDATES their completion (a re-imported /
    # previously-watched title: Plex kept the old ``last_viewed_at`` from before
    # this import). Those yield a NEGATIVE completed_to_last_watch that would
    # corrupt the time-to-watch dataset, so the per-title interval row is skipped
    # for them below; the interval measures POST-import time-to-watch, and a
    # pre-import view has no such interval. The resolved contexts are reused by the
    # per-title loop (the id lookup is not paid twice).
    watch_rows: list[tuple[EvictionCandidate, datetime, _CandidateContext]] = []
    for candidate, last_viewed in watch_activity:
        context = await _candidate_context(session, candidate)
        watch_rows.append((candidate, last_viewed, context))
    preexisting_watch_count = sum(
        1
        for _candidate, last_viewed, context in watch_rows
        if context.completed_at is not None and last_viewed < context.completed_at
    )

    _logger.info(
        "retention telemetry: %s root %s -- %d eligible eviction candidate(s) "
        "(~%d byte(s)); of those, %d would_evict now to relieve %.1f%%->%.1f%% "
        "pressure (~%d byte(s) reclaimable, hardlink-aware); %d title(s) with "
        "recorded watch activity, idle-age distribution: %s; excluded (kept "
        "honest, never deleted): no_path=%d, guard_refused=%d, preexisting_watch=%d",
        media_type,
        root_path,
        len(eligible),
        _sum_estimated_bytes(eligible, disk.total_bytes),
        len(would_evict),
        threshold_pct,
        target_pct,
        would_free_bytes,
        len(watch_activity),
        _format_buckets(bucket_counts),
        no_path_count,
        guard_refused_count,
        preexisting_watch_count,
    )

    for candidate, last_viewed, context in watch_rows:
        # In-process dedupe: skip a per-title row whose last_viewed_at has not
        # advanced since the last one emitted for this title/season, so a steady
        # watch state does not re-emit an identical row on every below-pressure
        # tick (see ``_last_emitted_watch``). The per-ROOT aggregate above still
        # emits every sweep.
        dedupe_key = (context.tmdb_id, candidate.media_type, candidate.season)
        if _last_emitted_watch.get(dedupe_key) == last_viewed:
            continue
        # A view that PREDATES completion (re-imported / previously-watched title)
        # has no post-import interval to report -- skip the row (already tallied in
        # the aggregate's ``preexisting_watch_count`` above) rather than emit a
        # negative completed_to_last_watch that corrupts the dataset. Not cached in
        # ``_last_emitted_watch`` (nothing was emitted for it), so if a genuine
        # post-import play later advances ``last_viewed_at`` past completion, that
        # real interval still emits.
        if context.completed_at is not None and last_viewed < context.completed_at:
            continue
        _last_emitted_watch[dedupe_key] = last_viewed
        if context.completed_at is not None:
            interval_repr = f"{(last_viewed - context.completed_at).total_seconds():.0f}s"
            completed_repr = context.completed_at.isoformat()
        else:
            interval_repr = "unknown"
            completed_repr = "unknown"
        season_note = f" season {candidate.season}" if candidate.season is not None else ""
        _logger.info(
            "retention telemetry: %r%s completed_at=%s last_viewed_at=%s "
            "completed_to_last_watch=%s",
            candidate.title,
            season_note,
            completed_repr,
            last_viewed.isoformat(),
            interval_repr,
            extra={"request_id": context.media_request_id, "tmdb_id": context.tmdb_id},
        )
