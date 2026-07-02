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
eviction sweep would have evicted nothing anyway), it runs the exact same
candidate ranking eviction would have used — :func:`~plex_manager.services.
eviction_service.preview_candidates`, which itself calls :func:`~plex_manager.
domain.eviction.rank_eviction_candidates` (no pressure gate) — and logs what
that ranking found. Nothing here calls ``fs.delete``, flips a status, or writes
``download_history``; the candidate list is read-only input to two ``logging``
calls.

Because :func:`~plex_manager.services.eviction_service.rank_eviction_candidates`
already drops any candidate with no recorded Plex view (``last_viewed_at is
None``), every candidate this sweep sees already has watch state — "where watch
state exists" from the blueprint is true of the whole list by construction, no
separate filter needed.

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
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Literal

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
_logger = logging.getLogger(TELEMETRY_LOGGER_NAME)

# Idle-age (now - last_viewed_at) buckets for the per-root distribution. The
# last boundary is implicit (anything >= the final explicit boundary falls into
# the final label) -- see _bucket_label. Chosen to bracket the beta's own
# default 30-day eviction_grace_days: <7d/7-14d/14-30d cover the pre-grace
# range, 30-60d/60-90d/90d+ cover how far PAST grace watched content tends to
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
    now: datetime | None = None,
) -> None:
    """Log what an eviction sweep of ``root_path`` WOULD do right now — never
    deletes, never flips a status, never writes ``download_history``.

    Intended caller: ``web/app.py``'s ``_eviction_tick``, only for a root whose
    disk usage is currently BELOW ``disk_pressure_threshold_percent`` (i.e. the
    pressure gate did NOT fire this tick, so the real sweep evicted nothing
    anyway) — see that function's docstring for the exact gating. Calling this
    unconditionally would be harmless (it is delete-nothing either way) but
    redundant: when pressure DID fire, the real sweep's own outcome log already
    tells the operator what happened.

    Two log calls per invocation, both through the dedicated
    ``TELEMETRY_LOGGER_NAME`` logger:

    1. ONE aggregate event: candidate count, the summed estimated would-free
       bytes (converted from each candidate's ``size_percent`` back to bytes
       using THIS root's real total capacity — the same estimate basis
       ``eviction_service`` itself uses before a delete), and the watch-idle
       age distribution (now - ``last_viewed_at``, bucketed).
    2. One event PER candidate: its completed_at -> last_viewed_at interval
       (see :func:`_candidate_context` for what "completed_at" means per media
       kind), with ``request_id``/``tmdb_id`` passed via ``extra={}`` (never
       interpolated into the message).

    An unreadable ``root_path`` (missing mount, permission denied) skips the
    whole sweep for this root, logged as a WARNING on this module's own
    (ordinary, not the ordinary services) logger -- mirrors
    ``eviction_service.preview_candidates``'s identical fallback.

    ``now`` defaults to ``datetime.now(UTC)``; overridable for a deterministic
    idle-age distribution in tests (a fake clock), never used in production.
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

    candidates = await eviction_service.preview_candidates(
        session=session,
        library=library,
        media_type=media_type,
        root_path=root_path,
        grace_days=grace_days,
    )

    moment = now if now is not None else datetime.now(UTC)
    bucket_counts: dict[str, int] = dict.fromkeys(_AGE_BUCKET_LABELS, 0)
    would_free_bytes = 0
    for candidate in candidates:
        # rank_eviction_candidates (inside preview_candidates) already dropped
        # every candidate with last_viewed_at is None -- see the module
        # docstring -- so this ``continue`` is unreachable in practice; kept as
        # an honest defensive narrowing rather than a bare assert (never trust
        # an upstream invariant blindly, mirrors domain/eviction.py's own
        # "narrowed by the only caller" pattern).
        last_viewed_at = candidate.last_viewed_at
        if last_viewed_at is None:
            continue
        age_days = (moment - last_viewed_at).total_seconds() / 86400.0
        bucket_counts[_bucket_label(age_days)] += 1
        if disk.total_bytes > 0:
            would_free_bytes += round(candidate.size_percent / 100.0 * disk.total_bytes)

    _logger.info(
        "retention telemetry: %s root %s has %d eviction candidate(s) if pressure "
        "fired right now (~%d byte(s) estimated would free); idle-age distribution: %s",
        media_type,
        root_path,
        len(candidates),
        would_free_bytes,
        _format_buckets(bucket_counts),
    )

    for candidate in candidates:
        last_viewed_at = candidate.last_viewed_at
        if last_viewed_at is None:
            continue  # unreachable per the same invariant noted above
        context = await _candidate_context(session, candidate)
        if context.completed_at is not None:
            interval_repr = f"{(last_viewed_at - context.completed_at).total_seconds():.0f}s"
            completed_repr = context.completed_at.isoformat()
        else:
            interval_repr = "unknown"
            completed_repr = "unknown"
        season_note = f" season {candidate.season}" if candidate.season is not None else ""
        _logger.info(
            "retention telemetry: %r%s completed_at=%s last_viewed_at=%s "
            "completed_to_first_watch=%s",
            candidate.title,
            season_note,
            completed_repr,
            last_viewed_at.isoformat(),
            interval_repr,
            extra={"request_id": context.media_request_id, "tmdb_id": context.tmdb_id},
        )
