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
  It is built from EVERY path-bearing row with a POST-IMPORT recorded Plex view
  (``last_viewed_at is not None``) — which deliberately INCLUDES a
  started-but-unfinished season (``watched=False`` but a view timestamp exists):
  that "began watching" signal is exactly what this dataset exists to capture,
  and the eligibility filter behind the would-evict numbers would drop it. The
  ONE recorded view it excludes is a PRE-IMPORT one (``last_viewed_at`` before the
  title's ``completed_at`` — a re-import keeping Plex's older timestamp), which has
  no post-import interval to measure and would skew the whole dataset; it is
  reported as ``preexisting_watch_count`` instead (see the partition contract
  below). It
  must also cover titles of any idle age, or it captures nothing during the
  one-week beta this sweep exists to serve (in a seven-day window NO title can
  have been idle past a 30-day grace, so a grace-filtered read would return
  ``[]`` on every tick and the whole primary dataset — plus the pre-grace
  <7d/7-14d/14-30d buckets — would be permanent dead weight).

The single raw read feeds all of this: ``rank_eviction_candidates`` /
``select_evictions`` re-derive the would-evict subset in memory over the same
rows (no second Plex pass and no second full-library ``os.walk``), and the
time-to-watch set is those rows with a POST-import recorded view (``last_viewed_at
is not None`` and not pre-import — see the partition contract below). The
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

The partition contract
======================
Every raw candidate is classified EXACTLY ONCE, up front, at a single point
(:func:`_classify_candidates` -> :class:`_Partition`); every metric this sweep
emits then reads a partition field and NONE re-derives its own filter -- so the
watch-activity count, the idle-age buckets and the per-title intervals can never
again disagree about which rows are in the time-to-watch dataset. Three exclusion
rules govern the partition -- each an honest "these rows exist, here is why we set
them aside", reported as a labelled count on the per-root aggregate, never a silent
drop. They act on TWO ORTHOGONAL axes, because a row set aside from one metric
family may still legitimately feed the other:

* The **disk-space axis** -- ``eligible`` / ``would_evict`` and their byte sums,
  answering "what would a pressure sweep delete". Its exclusions:

  - ``no_path_count`` (rule 1) -- a row with NO ``library_path`` breadcrumb (e.g. a
    movie found already in Plex, short-circuited straight to ``available`` by
    ``request_service.create_request``: ``mark_available`` stamps a ``completed_at``
    that is the Plex-verification moment, not an import time, and no file of ours
    was ever placed). Eviction can never touch it, so it is excluded from EVERY
    metric on BOTH axes.
  - ``guard_refused_count`` (rule 3) -- a would-evict candidate whose breadcrumb is
    lexically under ``root_path`` but resolves, via a symlinked component, OUTSIDE
    every configured root. The real ``LocalFileSystem.delete`` refuses it and frees
    nothing, so each would-evict candidate is run through ``fs``'s OWN delete guard
    (:meth:`~plex_manager.ports.filesystem.FileSystemPort.delete_guard_refuses`, the
    very predicate ``delete`` raises on) and a refused row is excluded from the
    ``would_evict`` count/bytes ONLY -- it is still a policy-eligible title (it
    counts in ``eligible``) and, if it has a post-import view, still a valid
    time-to-watch data point.

* The **time-to-watch axis** -- the watch-activity count, the idle-age distribution
  AND the per-title ``completed_to_last_watch`` intervals (one dataset, so they MUST
  agree), answering "how long after import does someone watch". Its exclusions:

  - ``no_path_count`` (rule 1) -- as above; its ``completed_at`` is not an import
    time, so it feeds no interval and is excluded here too.
  - ``preexisting_watch_count`` (rule 2) -- a path-bearing row whose recorded view
    PREDATES its completion (a re-imported / previously-watched title keeps Plex's
    older ``last_viewed_at``), which would yield a negative
    ``completed_to_last_watch``. The interval measures POST-import time-to-watch, and
    a pre-import view has no such interval, so it is excluded from ALL time/idle
    metrics -- the per-title interval, the idle-age buckets AND the watch-activity
    count alike -- and reported only as ``preexisting_watch_count``. It is NOT
    excluded from the disk-space axis: a previously-watched title is still watched,
    past grace, and evictable, so it still counts in ``eligible``/``would_evict``. A
    row whose ``completed_at`` is UNKNOWN is NOT pre-import (its idle age is still
    well defined), so it stays in the watch dataset and its interval logs an honest
    "unknown" rather than being dropped or guessed at.

Why two axes and not one global bucket-per-row: a single mutually-exclusive
membership would be WRONG here. A ``preexisting`` view is still an evictable title
(it must stay in ``eligible``); a ``guard_refused`` title may still have a valid
post-import watch (it must stay in the watch dataset). So the partition is disjoint
WITHIN each axis -- every path-bearing viewed row is exactly one of
{watch-metric-eligible, preexisting}; every would-evict-selected row is exactly one
of {would_evict, guard_refused}; ``no_path`` is disjoint from all path-bearing rows
-- while a single path-bearing row may be a member on BOTH axes at once.

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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final, Literal

from plex_manager.domain.eviction import (
    pressure_relieved,
    rank_eviction_candidates,
    select_evictions,
)
from plex_manager.models import MediaRequest, SeasonRequest
from plex_manager.services import eviction_service, purge_service
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


#: Max per-title ``completed_to_last_watch`` rows a SINGLE sweep will EMIT. Chosen
#: to stay WELL under ``log_capture_service.QUEUE_MAXSIZE`` (2000) -- it MUST, and
#: this is why: the ``LogCaptureHandler``'s durable-sink queue is bounded and, when
#: a burst overruns it between drain ticks, silently drops the NEWEST record
#: (``_enqueue`` -> ``QueueFull`` -> ``dropped_count += 1``). The ``logging`` API
#: gives the caller here NO signal that a record failed to enqueue, so a per-title
#: burst larger than the queue's remaining headroom would be recorded in
#: ``_last_emitted_watch`` as "emitted" yet never reach ``log_events`` -- the row
#: is then silently lost until that title's ``last_viewed_at`` next advances. This
#: budget bounds each sweep's burst so it cannot by itself overrun the queue, and
#: -- crucially -- the emission loop caches ONLY the rows it actually emits this
#: sweep, leaving any overflow past the budget UN-cached so the next tick (default
#: 30m) re-attempts it. Self-pacing, no logging-API surgery: nothing is lost, only
#: paced. When a sweep truncates, the aggregate reports the honest ``deferred_rows``
#: count so the pacing is visible, never swallowed.
_PER_SWEEP_EMISSION_BUDGET: Final = 200


#: Slack held back from the log queue's LIVE free-slot count before this sweep
#: spends any of it on per-title rows (see ``free_slots`` param on
#: :func:`run_retention_telemetry_sweep`). The static ``_PER_SWEEP_EMISSION_BUDGET``
#: above bounds a sweep's burst against an EMPTY queue, but the queue is rarely
#: empty: ordinary INFO chatter and a not-yet-run drain tick leave an ambient
#: backlog occupying it. Sizing each sweep's emission against
#: ``max(0, free_slots() - _QUEUE_SAFETY_MARGIN)`` keeps the burst under the LIVE
#: headroom, so a full-budget burst on top of that backlog no longer silently
#: overruns ``LogCaptureHandler``'s bounded queue while the dedupe cache records
#: the newest rows as "emitted". The margin is deliberately GENEROUS (2.5x the
#: full budget): the read is inherently racy -- concurrent loggers and the drain
#: task mutate the queue between :meth:`LogCaptureHandler.free_slots` and this
#: sweep's actual ``_logger.info`` calls -- and the margin absorbs that race.
#: Beyond it, any residual loss is NOT swallowed: the handler's existing
#: ``LogCaptureHandler.dropped_count`` (incremented in
#: ``LogCaptureHandler._enqueue`` on ``QueueFull``, and in ``drain_once`` on a
#: failed insert) makes every INFO+ record that missed durable storage VISIBLE on
#: the health/log surfaces. This budget is best-effort-with-visible-drops, never
#: transactional.
_QUEUE_SAFETY_MARGIN: Final = 500


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
    see the module docstring's "no schema change" constraint), so ``completed_at``
    here is the PARENT SHOW's ``MediaRequest.completed_at`` -- stamped by
    ``season_request_service._recompute_parent`` the first time any tracked season
    reaches ``completed``/``available`` (the per-season analogue of the movie
    ``mark_completed`` stamp; before that fix a TV parent's ``completed_at`` was
    NEVER stamped and every TV interval read "unknown"). Exact for a single-season
    show, an honest APPROXIMATION for a later season of a multi-season show (it
    reflects whenever the show's FIRST tracked season completed, not necessarily
    this one). This is a documented, known limitation, not a silently assumed
    precision -- if week-1 telemetry shows this caveat matters for the analysis, a
    follow-up can add a per-season ``completed_at`` column with its own migration.
    A parent still carrying ``None`` (a row imported before the stamp existed)
    resolves to ``completed_at=None`` and the caller logs "unknown" -- see below.

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


@dataclass(frozen=True)
class _Partition:
    """One root's raw candidates classified ONCE (see :func:`_classify_candidates`),
    so every metric reads a field here and none re-derives its own filter.

    The full rationale is the module docstring's "partition contract"; in brief, the
    three exclusion rules act on TWO ORTHOGONAL axes:

    * disk-space axis -- ``eligible`` (the full policy-evictable set, which KEEPS
      pre-import views: a previously-watched title is still evictable) and
      ``would_evict`` (the subset a pressure sweep would actually delete, with its
      measured hardlink-aware ``would_free_bytes``). ``no_path_count`` (rule 1) and
      ``guard_refused_count`` (rule 3) are its exclusions; a guard-refused row is
      dropped from ``would_evict`` ONLY, never from ``eligible``.
    * time-to-watch axis -- ``watch_metric_eligible`` (each ``(candidate,
      last_viewed_at, context)`` resolved once) feeds the watch-activity count, the
      idle-age buckets AND the per-title intervals, so those three always agree.
      ``no_path_count`` (rule 1) and ``preexisting_watch_count`` (rule 2, a
      pre-import view) are its exclusions.

    Disjoint WITHIN each axis (every path-bearing viewed row is exactly one of
    {``watch_metric_eligible``, preexisting}; every would-evict-selected row is
    exactly one of {``would_evict``, guard_refused}; ``no_path`` rows are disjoint
    from all path-bearing rows), while a single path-bearing row may be a member on
    BOTH axes at once -- which is why this is not a single global bucket-per-row.
    """

    no_path_count: int
    eligible: list[EvictionCandidate]
    would_evict: list[EvictionCandidate]
    would_free_bytes: int
    guard_refused_count: int
    watch_metric_eligible: list[tuple[EvictionCandidate, datetime, _CandidateContext]]
    preexisting_watch_count: int


async def _classify_candidates(
    *,
    session: AsyncSession,
    fs: FileSystemPort,
    candidates: Sequence[EvictionCandidate],
    grace_cutoff: datetime,
    threshold_pct: float,
    target_pct: float,
    total_bytes: int,
) -> _Partition:
    """Partition one root's RAW candidates ONCE into the metric-eligible sets and
    the labelled exclusion counts every emitted field consumes.

    This is the SINGLE classification point (see :class:`_Partition` and the module
    docstring's partition contract). The three exclusion rules are applied HERE and
    NOWHERE else, so no downstream metric re-derives its own filter and the
    watch-activity count, idle-age buckets and per-title intervals can never again
    disagree about which rows belong to the time-to-watch dataset.

    Rule 0 (issue #104) -- duplicate ``library_path`` collapse, applied BEFORE rule
    1. Two distinct rows (e.g. a re-request that lands on a title already imported
    at the same path) can carry the identical breadcrumb; both axes must count that
    physical file exactly ONCE, or the eligible/would-evict totals and the
    watch-activity count/idle-age buckets overstate reality for a single title. A
    ``None`` breadcrumb is NEVER collapsed this way (rule 1 below excludes every
    no-path row individually; ``None == None`` would otherwise wrongly fold every
    breadcrumb-less row into one).
    """
    # Rule 0 -- duplicate-path collapse (issue #104): first-seen-wins per distinct
    # ``library_path``, leaving every ``None``-path row untouched (each counted
    # separately by rule 1 below). Applied before path_bearing/no_path_count so
    # every downstream metric on both axes sees each physical file once.
    seen_paths: set[str] = set()
    deduplicated: list[EvictionCandidate] = []
    for candidate in candidates:
        path = candidate.library_path
        if path is not None:
            if path in seen_paths:
                continue
            seen_paths.add(path)
        deduplicated.append(candidate)

    # Rule 1 -- no-path total exclusion, applied first. A None breadcrumb means
    # nothing of ours is on disk (eviction can never touch it) and ``completed_at``
    # is not an import time (no post-import interval), so the row feeds NEITHER axis
    # and is only counted. The two axes are then classified independently over the
    # surviving path-bearing rows (orthogonal, not one global bucket -- see the
    # dataclass docstring).
    path_bearing = [candidate for candidate in deduplicated if candidate.library_path is not None]
    no_path_count = len(deduplicated) - len(path_bearing)

    # Disk-space axis. ``eligible`` is the FULL policy-evictable set at the real
    # grace cutoff (pre-import views INCLUDED: a previously-watched title is still
    # watched + past grace + evictable). ``would_evict`` reuses ``run_eviction_sweep``'s
    # two-phase selection -- the estimate-based ``select_evictions`` prefix plus the
    # reclaimable-aware extension, both gated on the shared ``pressure_relieved``
    # predicate so this can never drift from a real sweep -- simulated AT the
    # threshold (the moment pressure first fires; the telemetry sweep only runs BELOW
    # threshold, where ``select_evictions`` correctly picks nothing). Rule 3 runs each
    # selected candidate through ``fs``'s OWN delete guard and drops a refused row
    # from ``would_evict`` (count/bytes) while LEAVING it in ``eligible``. Phase 1
    # attempts every ``selected`` candidate with no early stop, exactly like the real
    # sweep; phase 2 re-checks pressure at the top of each iteration, so a refused
    # candidate simply frees nothing and the loop keeps drawing.
    eligible = rank_eviction_candidates(path_bearing, grace_cutoff)
    selected = select_evictions(
        path_bearing, threshold_pct, threshold_pct, target_pct, grace_cutoff
    )
    extra_pool = eligible[len(selected) :]
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
        if pressure_relieved(threshold_pct, would_free_bytes, total_bytes, target_pct):
            break
        if await _guard_refuses(fs, candidate):
            guard_refused_count += 1
            continue
        would_evict.append(candidate)
        would_free_bytes += await _reclaimable_bytes(fs, candidate)

    # Time-to-watch axis. Over EVERY path-bearing row with a recorded view (watched
    # OR started-but-unfinished -- the "began watching" signal), rule 2 splits out
    # PRE-import views: a recorded view older than the title's completion has no
    # post-import interval and would skew the whole dataset, so it is excluded from
    # ALL time/idle metrics (interval, buckets AND count alike) and counted only as
    # ``preexisting_watch``. A row whose ``completed_at`` is UNKNOWN is NOT pre-import
    # (its idle age is still well defined), so it stays metric-eligible and logs an
    # honest "unknown" interval. Each context is resolved ONCE here (the id lookup is
    # not paid twice) and reused by the buckets/emission downstream.
    watch_metric_eligible: list[tuple[EvictionCandidate, datetime, _CandidateContext]] = []
    preexisting_watch_count = 0
    for candidate in path_bearing:
        last_viewed = candidate.last_viewed_at
        if last_viewed is None:
            continue
        context = await _candidate_context(session, candidate)
        if context.completed_at is not None and last_viewed < context.completed_at:
            preexisting_watch_count += 1
            continue
        watch_metric_eligible.append((candidate, last_viewed, context))

    return _Partition(
        no_path_count=no_path_count,
        eligible=eligible,
        would_evict=would_evict,
        would_free_bytes=would_free_bytes,
        guard_refused_count=guard_refused_count,
        watch_metric_eligible=watch_metric_eligible,
        preexisting_watch_count=preexisting_watch_count,
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
    free_slots: Callable[[], int] | None = None,
    all_roots: Sequence[str] | None = None,
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
       * the count of titles with a POST-IMPORT recorded view and the watch-idle
         age distribution (now - ``last_viewed_at``, bucketed) over the SAME set --
         pre-import views (``preexisting_watch_count``) are excluded from BOTH, so
         the count, the buckets and the per-title intervals are one consistent
         dataset (see the module docstring's partition contract).
       * ``no_path_count`` / ``guard_refused_count`` / ``preexisting_watch_count``
         -- rows set aside from the metrics above (no breadcrumb, delete-guard
         refusal, a view predating completion), reported so the dataset shows they
         exist without any metric being overstated (see the module docstring).
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

    ``free_slots``, when given, reports the durable log queue's LIVE free-slot
    count (wired by ``web/app.py`` to ``app.state.log_handler.free_slots``,
    which is ``QUEUE_MAXSIZE - qsize()``). It DYNAMICALLY shrinks this sweep's
    per-title emission budget to ``min(_PER_SWEEP_EMISSION_BUDGET, max(0,
    free_slots() - _QUEUE_SAFETY_MARGIN))`` so a burst cannot be recorded as
    emitted (in :data:`_last_emitted_watch`) yet silently dropped by a queue an
    ambient INFO backlog has already partly filled — the exact gap the static
    budget alone left open. Rows past the effective budget are deferred UN-cached
    and surfaced as ``deferred_rows``, identical to the static-budget path.
    ``None`` (a one-shot / test call with no live handler) falls back to the
    static :data:`_PER_SWEEP_EMISSION_BUDGET`. This is a best-effort guarantee
    WITH VISIBLE DROPS, not a transactional one: the read is racy and the margin
    only absorbs so much, so any record that still loses the race is counted in
    ``LogCaptureHandler.dropped_count`` (never silently lost) — see
    :data:`_QUEUE_SAFETY_MARGIN`.
    """
    try:
        # ``shutil.disk_usage`` (a ``statvfs`` syscall) can stall on a hung
        # NFS/SMB mount. Use the shared abandonable substrate (mirrors
        # preview_candidates/run_eviction_sweep) so a wedged probe cannot strand
        # CPython's joined default executor past bounded shutdown — the eviction
        # tick's pressure-pass branch falls straight into this sweep.
        disk = await purge_service.run_abandonable_probe(
            lambda: read_disk_usage(root_path),
            root_path,
            operation_name="retention telemetry disk-usage probe",
        )
    except OSError as exc:
        _logger.warning(
            "retention telemetry sweep skipped for %s root %s (%s)",
            media_type,
            root_path,
            type(exc).__name__,
        )
        return

    moment = now if now is not None else datetime.now(UTC)

    # Computed BEFORE assembly (issue #304) so it can be threaded straight into
    # ``assemble_candidates``'s walk-skip optimization -- this is the EXACT
    # cutoff ``_classify_candidates`` below re-derives its ``eligible``/
    # ``would_evict`` subsets against, so a row the walk skipped there is always
    # exactly a row those subsets exclude anyway. Never narrows the RETURNED
    # superset (see ``assemble_candidates``'s docstring) -- the time-to-watch
    # dataset below still sees every started-but-unfinished/unwatched/pinned row,
    # just with an honest ``size_percent=0.0`` for the ones nothing here ever
    # sums bytes over.
    grace_cutoff = moment - timedelta(days=grace_days)

    # ONE raw read for ALL products: every available title/season, no grace or
    # eligibility filter ON THE RETURNED SET -- the superset from which both the
    # would-evict subsets (ranked/selected in memory) and the time-to-watch
    # dataset (any recorded view) are derived. See the module docstring for the
    # full rationale. ``all_roots`` mirrors ``run_eviction_sweep``'s own
    # nested-root ownership scope (see ``assemble_candidates``): the telemetry
    # for a parent root must not count content a nested child root's REAL sweep
    # would own, or the would-evict numbers double-report the child's bytes
    # under the parent.
    candidates = await eviction_service.assemble_candidates(
        session=session,
        library=library,
        media_type=media_type,
        root_path=root_path,
        root_total_bytes=disk.total_bytes,
        all_roots=all_roots,
        grace_cutoff=grace_cutoff,
    )

    # SINGLE classification pass: partition the raw candidates ONCE into the
    # metric-eligible sets and the labelled exclusion counts (no_path / guard_refused
    # / preexisting) -- see ``_classify_candidates`` / ``_Partition`` and the module
    # docstring's partition contract. Every metric below reads a partition field;
    # none re-derives a filter, so the watch-activity count, the idle-age buckets and
    # the per-title intervals can never disagree about which rows are in the
    # time-to-watch dataset.
    partition = await _classify_candidates(
        session=session,
        fs=fs,
        candidates=candidates,
        grace_cutoff=grace_cutoff,
        threshold_pct=threshold_pct,
        target_pct=target_pct,
        total_bytes=disk.total_bytes,
    )

    # Idle-age (now - last_viewed_at) distribution over the time-to-watch dataset --
    # the SAME ``watch_metric_eligible`` rows the count and per-title intervals use,
    # so a pre-import view excluded from one is excluded from all three (the round-7
    # fix: this pass no longer runs before the pre-import filter).
    bucket_counts: dict[str, int] = dict.fromkeys(_AGE_BUCKET_LABELS, 0)
    for _candidate, last_viewed, _context in partition.watch_metric_eligible:
        age_days = (moment - last_viewed).total_seconds() / 86400.0
        bucket_counts[_bucket_label(age_days)] += 1

    # Decide which per-title rows this sweep will EMIT, in order, BEFORE the
    # aggregate logs -- so the aggregate can honestly report how many the emission
    # budget defers to the next tick (``deferred_rows``). The rows are already the
    # partition's ``watch_metric_eligible`` set (pre-import views were excluded THERE,
    # rule 2, not re-filtered here -- a pre-import view is never cached, so a genuine
    # later post-import play still emits). This pass applies only the cross-sweep
    # dedupe and collapses intra-sweep duplicate keys:
    #   * a row whose watch has NOT ADVANCED since the last row emitted for it is
    #     skipped (the dedupe that stops a steady watch state re-logging every 30m
    #     tick -- see ``_last_emitted_watch``);
    #   * intra-sweep duplicates of a key collapse to the first occurrence
    #     (``queued_keys``), mirroring what the cache would do across the loop below.
    # Nothing is cached HERE (this is a read-only decision pass); the cache is written
    # only as each row is actually emitted, so the deferred tail stays un-cached and
    # retries.
    to_emit: list[tuple[EvictionCandidate, datetime, _CandidateContext]] = []
    queued_keys: set[tuple[int | None, str, int | None]] = set()
    for candidate, last_viewed, context in partition.watch_metric_eligible:
        dedupe_key = (context.tmdb_id, candidate.media_type, candidate.season)
        if _last_emitted_watch.get(dedupe_key) == last_viewed:
            continue
        if dedupe_key in queued_keys:
            continue
        queued_keys.add(dedupe_key)
        to_emit.append((candidate, last_viewed, context))

    # Per-sweep emission budget (see ``_PER_SWEEP_EMISSION_BUDGET``): a burst larger
    # than the log queue's headroom could otherwise be cached as emitted yet silently
    # dropped by the handler. The static budget bounds the burst against an EMPTY
    # queue; ``free_slots`` (when wired) shrinks it to the queue's LIVE headroom
    # minus a safety margin, so an ambient INFO backlog already occupying the queue
    # cannot turn a full-budget burst into silent drops (see ``_QUEUE_SAFETY_MARGIN``
    # and this function's docstring). ``None`` -> the static budget (one-shot/test
    # calls with no live handler). Emit at most the effective budget this tick; the
    # overflow is deferred (un-cached) to the next tick and surfaced here as
    # ``deferred_rows``, identical to the static-budget path.
    effective_budget = _PER_SWEEP_EMISSION_BUDGET
    if free_slots is not None:
        effective_budget = min(
            _PER_SWEEP_EMISSION_BUDGET, max(0, free_slots() - _QUEUE_SAFETY_MARGIN)
        )
    deferred_rows = max(0, len(to_emit) - effective_budget)

    _logger.info(
        "retention telemetry: %s root %s -- %d eligible eviction candidate(s) "
        "(~%d byte(s)); of those, %d would_evict now to relieve %.1f%%->%.1f%% "
        "pressure (~%d byte(s) reclaimable, hardlink-aware); %d title(s) with "
        "recorded watch activity, idle-age distribution: %s; excluded (kept "
        "honest, never deleted): no_path=%d, guard_refused=%d, preexisting_watch=%d; "
        "deferred_rows=%d (per-title rows beyond this sweep's emission budget, "
        "retried next tick)",
        media_type,
        root_path,
        len(partition.eligible),
        _sum_estimated_bytes(partition.eligible, disk.total_bytes),
        len(partition.would_evict),
        threshold_pct,
        target_pct,
        partition.would_free_bytes,
        len(partition.watch_metric_eligible),
        _format_buckets(bucket_counts),
        partition.no_path_count,
        partition.guard_refused_count,
        partition.preexisting_watch_count,
        deferred_rows,
    )

    # Emit at most the effective budget's worth this sweep; the deferred tail
    # (``to_emit[effective_budget:]``) is intentionally left UN-cached so the next
    # tick re-attempts it. The dedupe/pre-import filtering already happened in the
    # decision pass above, so this loop only formats + emits + caches.
    for candidate, last_viewed, context in to_emit[:effective_budget]:
        # Cache ONLY now, as this row is actually emitted (never in the decision
        # pass): the whole point of the budget is that a row recorded as emitted
        # here has been handed to the logging call below, not deferred.
        dedupe_key = (context.tmdb_id, candidate.media_type, candidate.season)
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
