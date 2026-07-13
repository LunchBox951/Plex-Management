"""Disk-pressure eviction — pure candidate selection for the watch-aware sweep.

Mirrors the ADR-0012 policy: a title (movie) or season (TV) is an eviction
CANDIDATE only when it is fully imported (``available``/``partially_available``),
fully watched, its last view is older than a grace floor, it is not pinned
"keep forever", and nothing is in flight for it. Deletion itself only fires
under disk pressure, stalest-``last_viewed_at`` first, until the projected used%
would drop at/under a target floor.

``status`` is duplicated here as bare string literals rather than imported from
``models.RequestStatus`` — the domain-purity rule (``tests/domain/test_domain_purity.py``)
forbids importing the ORM module, mirroring the existing precedent in
``domain/season_rollup.py``.

Each candidate's disk footprint is expressed directly as :attr:`EvictionCandidate.
size_percent` — its estimated share of the ROOT's total capacity (i.e. already
``size_bytes / root_total_bytes * 100``) — rather than raw bytes, so this module
never needs a root's total-byte count threaded through it: :func:`select_evictions`
stays entirely in percentage space, the same space ``used_pct``/``threshold_pct``/
``target_pct`` already live in (see :mod:`plex_manager.domain.disk_usage` for the
byte -> percentage conversion the caller uses to build it). A candidate whose size
is unknown reports ``0.0``: it is still evictable (unwatched/pinned/grace rules are
unaffected), it just never helps close the gap toward the target on its own — an
honest "we don't know how much this saves" rather than a fabricated guess.

Honesty over silence: eviction never touches ``keep_forever`` or unwatched
content, never fires without disk pressure (:func:`select_evictions` returns an
empty list below ``threshold_pct``), and the caller is expected to log + flip
every returned candidate to the non-terminal, re-requestable ``evicted`` status —
never a silent delete.

Pure domain: stdlib only. No I/O, no adapter/web/ORM imports.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

__all__ = [
    "EvictionCandidate",
    "pressure_relieved",
    "rank_eviction_candidates",
    "select_evictions",
]

# The two "fully imported" RequestStatus values eligible for eviction. Bare string
# literals — see the module docstring on domain purity. Mirrors
# ``domain/season_rollup.py``'s ``_DONE`` set: ``available`` (fully present) and
# ``partially_available`` (a TV show rollup with some, but not all, seasons done —
# the DONE seasons are still individually evictable; the per-season candidate
# passed in here carries the SEASON's own status, so in practice a season
# candidate is almost always plain ``available``, but the union is kept so a
# caller that (defensibly) surfaces the parent rollup status for a movie-shaped
# candidate is not silently excluded).
_ELIGIBLE_STATUSES: frozenset[str] = frozenset({"available", "partially_available"})


@dataclass(frozen=True)
class EvictionCandidate:
    """One title (movie) or season (TV) considered for disk-pressure eviction.

    ``request_id`` is the owning row's id — a movie ``MediaRequest.id`` or a TV
    ``SeasonRequest.id``; this module is granularity-agnostic, the caller is not.
    ``season`` is ``None`` for a movie candidate, the season number for TV.

    ``status`` is the candidate's OWN ``RequestStatus`` value (bare ``str`` — see
    module docstring). ``watched``/``last_viewed_at`` come from
    ``LibraryPort.watch_state`` (movie: ``viewCount>0``; season: every episode
    viewed). ``last_viewed_at`` is ``None`` when Plex has never recorded a view —
    such a candidate is NEVER eligible regardless of ``watched`` (defensive: an
    inconsistent watched=True/last_viewed_at=None pair is honestly excluded, never
    guessed at).

    ``keep_forever`` is the operator pin (never evicted while set). ``in_flight``
    is a caller-computed flag — e.g. an active download/import tied to this exact
    request/season — so an eviction can never race a re-request already under way.

    ``library_path`` is the final placed path the caller would ``fs.delete()`` on
    an eviction; carried through unexamined by this module (``None`` is possible
    for older data predating the breadcrumb — the caller decides how to handle
    that honestly, e.g. skip + log, never guess a path).

    ``size_percent`` is this candidate's on-disk footprint as a percentage of its
    root's total capacity (see module docstring); used only to project the
    running used% as candidates are picked.
    """

    request_id: int
    media_type: Literal["movie", "tv"]
    title: str
    season: int | None
    status: str
    watched: bool
    last_viewed_at: datetime | None
    keep_forever: bool
    in_flight: bool
    library_path: str | None
    size_percent: float
    watchlisted: bool = False


def _is_eligible(
    candidate: EvictionCandidate, last_viewed_at: datetime, grace_cutoff: datetime
) -> bool:
    """Return whether ``candidate`` may EVER be evicted, pressure aside.

    ``last_viewed_at`` is passed in already narrowed to non-``None`` by the only
    caller (:func:`rank_eviction_candidates`, which skips a candidate with no
    recorded view before calling this) — that ``None`` check lives there so it
    doubles as the type narrowing the stalest-first sort below needs, rather than
    repeating an ``is not None`` check pyright cannot see across the call.

    All of: a fully-imported status, watched, the view older than
    ``grace_cutoff``, not pinned, and not in flight. Order matches the ADR
    (status -> watched -> grace -> pin -> in-flight); short-circuits on the first
    failing check so the common "not even watched yet" case is cheap.
    """
    return (
        candidate.status in _ELIGIBLE_STATUSES
        and candidate.watched
        and last_viewed_at < grace_cutoff
        and not candidate.keep_forever
        and not candidate.watchlisted
        and not candidate.in_flight
    )


def rank_eviction_candidates(
    candidates: Sequence[EvictionCandidate],
    grace_cutoff: datetime,
) -> list[EvictionCandidate]:
    """Return every EVER-evictable candidate, stalest ``last_viewed_at`` first.

    Pressure-independent: this is the full eligible set regardless of current disk
    usage, so it also backs the ``GET /api/v1/ops/disk`` candidate PREVIEW (an
    operator can see what a pressure sweep *would* pick without one actually
    firing) and a proactive (non-pressure-triggered) sweep, neither of which wants
    :func:`select_evictions`'s target-based early stop.

    A candidate with no recorded view (``last_viewed_at is None`` — Plex has never
    played it) is dropped up front: it can never be eligible regardless of
    ``watched`` (an inconsistent watched=True/no-view pair is honestly excluded,
    never guessed at).

    Sort is stable: candidates with an identical ``last_viewed_at`` keep their
    relative input order rather than being reordered by an arbitrary tiebreaker.
    """
    dated: list[tuple[datetime, EvictionCandidate]] = []
    for candidate in candidates:
        last_viewed_at = candidate.last_viewed_at
        if last_viewed_at is None:
            continue
        if _is_eligible(candidate, last_viewed_at, grace_cutoff):
            dated.append((last_viewed_at, candidate))

    dated.sort(key=lambda pair: pair[0])
    return [candidate for _, candidate in dated]


def select_evictions(
    candidates: Sequence[EvictionCandidate],
    used_pct: float,
    threshold_pct: float,
    target_pct: float,
    grace_cutoff: datetime,
) -> list[EvictionCandidate]:
    """Return the ordered candidates to evict for one pressure-triggered sweep.

    Pressure-gated: below ``threshold_pct`` used, NOTHING is evicted (returns
    ``[]``) even if eligible candidates exist — eviction is disk-pressure
    triggered, never automatic just because content has aged past grace (that is
    the separate, opt-in proactive sweep, which should call
    :func:`rank_eviction_candidates` directly rather than this function).

    At/above ``threshold_pct``: candidates are ranked stalest-``last_viewed_at``
    first (:func:`rank_eviction_candidates`) and picked off in that order,
    projecting ``used_pct`` down by each pick's ``size_percent``, stopping as soon
    as the projection is at/under ``target_pct``. If every eligible candidate is
    exhausted before reaching the target, all of them are returned — this
    function never invents a candidate that was not eligible, so under-shooting
    the target is a possible, honestly-reported outcome (the caller's disk gauge
    will show the sweep did not fully relieve pressure).
    """
    if used_pct < threshold_pct:
        return []

    ranked = rank_eviction_candidates(candidates, grace_cutoff)

    selected: list[EvictionCandidate] = []
    projected = used_pct
    for candidate in ranked:
        if projected <= target_pct:
            break
        selected.append(candidate)
        projected -= candidate.size_percent

    return selected


def pressure_relieved(
    used_pct: float, freed_bytes: int, total_bytes: int, target_pct: float
) -> bool:
    """Whether ``freed_bytes`` reclaimed so far bring projected used% at/under target.

    The single stop condition shared by two callers so neither reimplements it:

    * :func:`~plex_manager.services.eviction_service.run_eviction_sweep`'s
      reclaimable-aware candidate extension — after the estimate-based
      :func:`select_evictions` prefix under-delivers (a hardlinked import frees
      far less than its nominal size), it keeps drawing stalest-first candidates
      until this returns ``True``.
    * the retention-telemetry would-evict SIMULATION
      (:func:`~plex_manager.services.retention_telemetry_service.
      run_retention_telemetry_sweep`), which projects the same extension against
      measured reclaimable bytes without deleting anything — so its would-evict
      count/bytes match what a real pressure sweep would do, hardlinks and all.

    ``freed_bytes`` is expressed relative to ``total_bytes`` (the root's capacity)
    and subtracted from ``used_pct``; the projection is at/under ``target_pct``
    once enough has been freed. A non-positive ``total_bytes`` cannot be projected
    against, so it reports ``True`` ("nothing more can be honestly shown to help —
    stop") rather than looping forever on an unknowable total; every real caller
    additionally guards ``total_bytes > 0`` before extending, so that branch is a
    defensive floor, not the normal path.
    """
    if total_bytes <= 0:
        return True
    freed_pct = (freed_bytes / total_bytes) * 100.0
    return used_pct - freed_pct <= target_pct
