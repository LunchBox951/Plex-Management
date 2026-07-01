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
#
# ``completed`` is deliberately NOT here: it is a DONE state (imported, awaiting
# Plex confirmation), and a done season must never outrank an unstarted/failed
# sibling. If it won precedence, ``[completed, pending]`` would roll the parent up
# to the TERMINAL ``completed`` — both lying that the whole show is finished and
# blocking the pending season's grab (grab_endpoint gates on the parent's terminal
# status). ``completed`` is folded into the done/partial branch below instead.
_PRECEDENCE_STATUSES: tuple[str, ...] = (
    "import_blocked",
    "downloading",
    "searching",
    "no_acceptable_release",
)


def rollup_status(season_statuses: Sequence[str]) -> str:
    """Fold every tracked season's status into the parent request's status.

    Precedence, in order:

    1. Any season in :data:`_PRECEDENCE_STATUSES` (``import_blocked`` /
       ``downloading`` / ``searching`` / ``no_acceptable_release``) wins outright.
    2. Every season ``evicted`` (ADR-0012's disk-pressure sweep reclaimed every
       tracked season's file) -> the (non-terminal, re-requestable) ``"evicted"``,
       mirroring the movie-level ``RequestStatus.evicted`` semantics.
    3. Otherwise every remaining season is one of ``pending``/``available``/
       ``completed``/``failed``/``evicted``. ``available`` and ``completed`` are
       the two REAL-DONE states (``completed`` = imported, Plex-confirmation
       pending) — something is actually watchable/imported right now.
       ``"partially_available"`` must only ever be reported when at least one
       season is REAL-DONE; an ``evicted`` season never earns it on its own,
       because nothing about an eviction leaves anything watchable behind:
       - every season done, all ``available``, none ``evicted`` -> ``"available"``
       - every season done, at least one still ``completed`` (no ``evicted``) ->
         ``"completed"``
       - a done season mixed with an ``evicted`` one (no ``pending``/``failed``) ->
         ``"partially_available"`` (some seasons present, one reclaimed)
       - a REAL-DONE season mixed with any ``pending``/``failed``/``evicted`` ->
         ``"partially_available"`` (honest, and non-terminal so the unfinished
         season stays grabbable)
       - NO real-done season is present (only ``evicted``/``pending``/``failed``,
         e.g. one season evicted and another failed) -> nothing is actually
         watchable, so this must NOT read ``"partially_available"``; ``evicted``
         folds in alongside ``failed`` for this purpose (both mean "nothing on
         disk for this season right now") and the remaining ``pending``/
         ``failed`` rule applies: any ``pending`` -> ``"pending"``, else
         -> ``"failed"``

    Pure and total over the season-status vocabulary: every combination of
    ``pending``/``searching``/``no_acceptable_release``/``downloading``/
    ``completed``/``available``/``failed``/``import_blocked``/``evicted`` resolves
    to exactly one branch above. Raises :class:`ValueError` on an empty sequence —
    a TV request always has at least one season once ``ensure_seasons`` has run, so
    an empty rollup input is a caller bug, never a state to silently guess at.
    """
    if not season_statuses:
        raise ValueError("rollup_status requires at least one season status")

    statuses = set(season_statuses)

    for status in _PRECEDENCE_STATUSES:
        if status in statuses:
            return status

    if statuses == {"evicted"}:
        # The whole show's tracked content was reclaimed by the disk-pressure
        # sweep -- honest, non-terminal, re-requestable at the show level too.
        return "evicted"

    # Only pending/available/completed/failed/evicted remain among the season
    # statuses. ``evicted`` folds alongside available/completed as "done" for the
    # purposes of this branch, EXCEPT it must never let the whole show read as
    # cleanly "available" (its file is gone) -- see the docstring above.
    _DONE = {"available", "completed"}
    _DONE_OR_EVICTED = _DONE | {"evicted"}
    if statuses <= _DONE_OR_EVICTED:
        if statuses == {"available"}:
            return "available"
        if "evicted" in statuses:
            return "partially_available"
        # Every season is done (available/completed, no evicted) but at least one
        # is still awaiting Plex confirmation -> the (terminal) "completed".
        return "completed"
    if statuses & _DONE:
        # At least one REAL-DONE season (available/completed) is mixed with a
        # pending/failed/evicted one: honestly partial -- something is actually
        # watchable right now, and never a terminal status that would mask the
        # unfinished season or block its grab. Deliberately checked against
        # ``_DONE`` (not ``_DONE_OR_EVICTED``): an ``evicted`` season must never
        # by itself earn "partially_available" -- see the docstring.
        return "partially_available"
    if "pending" in statuses:
        return "pending"
    return "failed"
