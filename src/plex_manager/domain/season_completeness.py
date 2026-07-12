"""Episode-level completeness for a whole-season TV request (ADR-0020).

The season-pack-only gate (``decision_engine.decide``'s ``prefer_season_pack``,
issue #167) permanently rejects any non-pack release for a whole-season request —
correct, but it leaves no path to watchable when no acceptable single-season pack
exists (an airing show, a niche show, or every pack blocklisted/failed). This
module is the pure arithmetic behind the Pass-2 episode-level fallback: what
"aired" means, what is still "missing", and when a season is truly complete.

Pure domain: stdlib only (``datetime.date``). No I/O, no adapter/web imports.
"""

from __future__ import annotations

from collections.abc import Mapping, Set
from datetime import date

__all__ = ["aired_target", "compute_missing", "season_is_complete"]


def aired_target(episodes: Mapping[int, date | None], today: date) -> frozenset[int]:
    """Episode numbers whose air date is known and on or before ``today``.

    An episode with a ``None`` air date is treated as NOT yet aired — an unknown
    air date is a conservative exclusion, never a guess — so an unscheduled
    special (or an episode TMDB hasn't dated yet) can never enter the target and
    make a season permanently "incomplete" while it waits on a date that may
    never arrive.
    """
    return frozenset(
        episode
        for episode, air_date in episodes.items()
        if air_date is not None and air_date <= today
    )


def compute_missing(target: Set[int], imported: Set[int], downloading: Set[int]) -> frozenset[int]:
    """Aired episodes still needed: target minus what is imported or in flight."""
    return frozenset(target - imported - downloading)


def season_is_complete(target: Set[int], imported: Set[int]) -> bool:
    """True iff ``target`` is non-empty and ``imported`` covers every episode in it.

    An empty/unknown target returns False unconditionally — the caller degrades
    to the legacy "a season pack import completes the season" behavior in that
    case, never guessing completeness from an empty target.
    """
    return bool(target) and target <= imported
