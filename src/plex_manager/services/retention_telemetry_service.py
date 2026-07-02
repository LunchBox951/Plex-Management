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
    would actually delete to relieve pressure, i.e. the
    :func:`~plex_manager.domain.eviction.select_evictions` prefix taken
    stalest-first down from the configured ``threshold_pct`` to the configured
    ``target_pct``. It is simulated AT the threshold (the moment pressure first
    fires), NOT at the current below-threshold usage — the sweep only runs while
    usage is below threshold, where ``select_evictions`` correctly selects
    nothing, so a current-usage simulation would always report zero and tell the
    operator nothing. This is always a prefix of the eligible set, so
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
``select_evictions`` re-derive the two would-evict subsets in memory (no second
Plex/FS pass), and the time-to-watch set is a plain ``last_viewed_at is not
None`` filter over the same rows. A second
:func:`~plex_manager.services.eviction_service.preview_candidates` call would
re-run every title's fresh Plex ``watch_state`` AND a full ``os.walk`` on every
below-pressure tick — exactly the redundant cost ``run_eviction_sweep``'s own
pressure pre-check goes out of its way to avoid. Nothing here calls
``fs.delete``, flips a status, or writes ``download_history``; the candidate
list is read-only input to the ``logging`` calls.

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

from plex_manager.domain.eviction import rank_eviction_candidates, select_evictions
from plex_manager.models import MediaRequest, SeasonRequest
from plex_manager.services import eviction_service
from plex_manager.services.health_service import read_disk_usage
from plex_manager.services.log_capture_service import TELEMETRY_LOGGER_NAME

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from plex_manager.domain.eviction import EvictionCandidate
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
    (see the module docstring for the full rationale); both would-evict subsets
    are re-derived from that superset in memory, no second Plex/FS pass.

    Log calls, all through the dedicated ``TELEMETRY_LOGGER_NAME`` logger:

    1. ONE aggregate event reporting, honestly split:
       * ``eligible_count`` / ``eligible_bytes`` — every ever-evictable candidate
         at the real ``grace_days`` cutoff (:func:`~plex_manager.domain.eviction.
         rank_eviction_candidates`).
       * ``would_evict_count`` / ``would_evict_bytes`` — the
         :func:`~plex_manager.domain.eviction.select_evictions` prefix a pressure
         sweep would actually delete to relieve ``threshold_pct``->``target_pct``,
         simulated AT the threshold (a strict subset of the eligible set).
       * the count of titles with ANY recorded view and the watch-idle age
         distribution (now - ``last_viewed_at``, bucketed) over them.
       Bytes are converted from each candidate's ``size_percent`` back with THIS
       root's real total capacity (the same estimate basis ``eviction_service``
       uses before a delete).
    2. One event PER title/season with any recorded view — INCLUDING a
       started-but-unfinished one (``watched=False`` but a view timestamp
       exists): its completed_at -> last_viewed_at interval (labelled
       ``completed_to_last_watch`` — ``last_viewed_at`` is Plex's most-recent
       play, so this is a watch-recency, not a true time-to-first-watch; see
       :func:`_candidate_context` for what "completed_at" means per media kind),
       with ``request_id``/``tmdb_id`` passed via ``extra={}`` (never
       interpolated into the message).

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

    grace_cutoff = moment - timedelta(days=grace_days)

    # Product 1a -- the FULL ever-evictable set at the real grace cutoff (what a
    # sweep COULD pick, regardless of how much space it would need to free).
    eligible = rank_eviction_candidates(candidates, grace_cutoff)

    # Product 1b -- the SUBSET a pressure sweep would ACTUALLY delete to relieve
    # pressure from the configured threshold down to the configured target,
    # stalest-first (a prefix of ``eligible``). Simulated with used_pct set to
    # ``threshold_pct`` (the moment pressure first fires), NOT the current
    # below-threshold usage: the telemetry sweep only runs BELOW threshold, where
    # select_evictions correctly picks nothing, so simulating at the real usage
    # would always report zero and answer the wrong question. See the module
    # docstring.
    would_evict = select_evictions(
        candidates, threshold_pct, threshold_pct, target_pct, grace_cutoff
    )

    # Product 2's population: EVERY row with any recorded Plex view -- watched OR
    # started-but-unfinished (``watched=False`` with a view timestamp). The
    # walrus narrows ``last_viewed_at`` to non-None for the rest of the sweep, so
    # the loops below never re-check it (a partial watch is exactly the "began
    # watching" signal the eligibility filter above would drop).
    watch_activity: list[tuple[EvictionCandidate, datetime]] = [
        (candidate, last_viewed)
        for candidate in candidates
        if (last_viewed := candidate.last_viewed_at) is not None
    ]

    bucket_counts: dict[str, int] = dict.fromkeys(_AGE_BUCKET_LABELS, 0)
    for _candidate, last_viewed in watch_activity:
        age_days = (moment - last_viewed).total_seconds() / 86400.0
        bucket_counts[_bucket_label(age_days)] += 1

    _logger.info(
        "retention telemetry: %s root %s -- %d eligible eviction candidate(s) "
        "(~%d byte(s)); of those, %d would_evict now to relieve %.1f%%->%.1f%% "
        "pressure (~%d byte(s) estimated would free); %d title(s) with recorded "
        "watch activity, idle-age distribution: %s",
        media_type,
        root_path,
        len(eligible),
        _sum_estimated_bytes(eligible, disk.total_bytes),
        len(would_evict),
        threshold_pct,
        target_pct,
        _sum_estimated_bytes(would_evict, disk.total_bytes),
        len(watch_activity),
        _format_buckets(bucket_counts),
    )

    for candidate, last_viewed in watch_activity:
        context = await _candidate_context(session, candidate)
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
