"""Season-status rollup — a TV request's aggregate lifecycle, computed pure.

A TV ``MediaRequest`` has no lifecycle of its own: unlike a movie request, its
``status`` is a COMPUTED rollup of its per-season ``SeasonRequest`` rows,
re-derived and persisted after every season transition (never itself the target
of a state-machine move). :func:`rollup_status` is that pure fold.

Season status *values* are duplicated here as bare string literals rather than
imported from ``models.RequestStatus`` — the domain-purity rule
(``tests/domain/test_domain_purity.py``) forbids importing the ORM module, so this
mirrors the existing precedent (``repositories/downloads.py``'s duplicated
terminal-state literals).

Pure domain: stdlib only. No I/O, no adapter/web imports.
"""

from __future__ import annotations

from collections.abc import Sequence

__all__ = ["rollup_status"]

# Needs-attention / in-flight statuses. Any one of these present among the season
# statuses wins outright, regardless of what the other seasons are doing — a show
# with one season mid-search or mid-import must never be dishonestly reported as
# merely "pending" or "available" because its other seasons are further along (or
# behind). Mirrors (as bare literals) ``models.RequestStatus`` members of the same
# name; order is for readability only, membership is what is tested.
_PRECEDENCE_STATUSES: tuple[str, ...] = (
    "import_blocked",
    "downloading",
    "searching",
    "no_acceptable_release",
    "completed",
)


def rollup_status(season_statuses: Sequence[str]) -> str:
    """Fold every tracked season's status into the parent request's status.

    Precedence, in order:

    1. Any season in :data:`_PRECEDENCE_STATUSES` (``import_blocked`` /
       ``downloading`` / ``searching`` / ``no_acceptable_release`` /
       ``completed``) wins outright.
    2. Otherwise every remaining season is one of ``pending``/``available``/
       ``failed``:
       - all ``available`` -> ``"available"``
       - ``available`` mixed with ``pending``/``failed`` ->
         ``"partially_available"``
       - any ``pending`` (with no ``available`` present) -> ``"pending"``
       - none of the above -> every season is ``failed`` -> ``"failed"``

    Pure and total over the season-status vocabulary: every combination of
    ``pending``/``searching``/``no_acceptable_release``/``downloading``/
    ``completed``/``available``/``failed``/``import_blocked`` resolves to exactly
    one branch above. Raises :class:`ValueError` on an empty sequence — a TV
    request always has at least one season once ``ensure_seasons`` has run, so an
    empty rollup input is a caller bug, never a state to silently guess at.
    """
    if not season_statuses:
        raise ValueError("rollup_status requires at least one season status")

    statuses = set(season_statuses)

    for status in _PRECEDENCE_STATUSES:
        if status in statuses:
            return status

    # Only pending/available/failed remain among the season statuses.
    if statuses == {"available"}:
        return "available"
    if "available" in statuses:
        return "partially_available"
    if "pending" in statuses:
        return "pending"
    return "failed"
