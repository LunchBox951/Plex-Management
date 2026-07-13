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
# ``completed`` (issue #265) IS here, deliberately last: it is the in-flight
# "Finalizing" phase (imported, awaiting Plex's availability confirmation --
# deliberately NOT settled, see ``_SETTLED_REQUEST_STATUSES`` in
# ``repositories/requests.py``), not a quietly-done state like ``available``. A
# season that finished importing but hasn't been Plex-confirmed yet is every bit
# as "active" as one still searching or downloading, so it must win over a
# dormant (``pending``/``waiting_for_air_date``) or settled (``failed``/
# ``evicted``/``cancelled``) sibling the exact same way the four statuses above
# it do -- otherwise the parent reads the honest-sounding but WRONG
# ``partially_available`` while a season is still finalizing, and
# ``frontend/src/lib/status.ts``'s nav badge (which deliberately excludes
# ``partially_available`` from ``IN_FLIGHT_REQUEST_STATUSES``, precisely BECAUSE
# it assumes precedence already promotes every genuinely-active season) goes dark
# for the whole finalizing window. Placed LAST in the tuple (lowest urgency of
# the five): a season that is merely awaiting confirmation must never outrank a
# sibling that is actually blocked/downloading/searching/exhausted -- those four
# are still real problems or real activity; ``completed`` is neither.
#
# This intentionally accepts the same coarse-grained trade-off the rollup already
# has for ``failed``/``evicted``/``cancelled`` (see the module docstring and
# ``test_cancelled_mixed_with_failed_and_no_done_season_is_failed``): the PARENT
# status is a single fold over every season, so a request-level consumer that
# switches on it (e.g. ``grab_service``'s up-front
# ``TERMINAL_REQUEST_STATUS_VALUES`` gate) sees one value for the whole show, not
# a per-season one. That has never been season-precise for TV, and this change
# does not make it any less precise than it already was.
_PRECEDENCE_STATUSES: tuple[str, ...] = (
    "import_blocked",
    "downloading",
    "searching",
    "no_acceptable_release",
    "completed",
)

# The complete, legitimate season-status vocabulary (issue #79): every bare-string
# value a real ``SeasonRequest.status`` can ever hold. Deliberately EXCLUDES
# ``partially_available`` -- that value is PARENT-ONLY, the rollup OUTPUT this
# very function produces for a show, never a value a single season's own status
# column can legitimately carry -- plus anything not in this set at all (a typo,
# a migration gap, or a future ``RequestStatus`` member added here without first
# updating this allowlist). Checked up front so such a value is a loud, honest
# ``ValueError`` at the fold boundary instead of silently falling through the
# branches below to the terminal ``"failed"`` and reporting a false settled
# failure for the whole show.
_VALID_SEASON_STATUSES: frozenset[str] = frozenset(
    {
        "pending",
        "searching",
        "no_acceptable_release",
        "waiting_for_air_date",
        "downloading",
        "completed",
        "available",
        "failed",
        "import_blocked",
        "evicted",
        "cancelled",
    }
)


def rollup_status(season_statuses: Sequence[str]) -> str:
    """Fold every tracked season's status into the parent request's status.

    Precedence, in order:

    1. Any season in :data:`_PRECEDENCE_STATUSES` (``import_blocked`` /
       ``downloading`` / ``searching`` / ``no_acceptable_release`` /
       ``completed``, issue #265) wins outright. ``completed`` is the in-flight
       "Finalizing" phase (imported, awaiting Plex's availability confirmation),
       not a quietly-settled one, so a show with one season still finalizing must
       read that way even while its other seasons are dormant (``pending`` /
       ``waiting_for_air_date``), settled (``failed`` / ``evicted`` /
       ``cancelled``), or genuinely done (``available``) -- exactly like the four
       statuses above it, and for the same reason: understating activity is as
       dishonest as overstating it (see :data:`_PRECEDENCE_STATUSES` for the full
       rationale, including why it is ordered last).
    2. Every season ``evicted`` (ADR-0012's disk-pressure sweep reclaimed every
       tracked season's file) -> the (non-terminal, re-requestable) ``"evicted"``,
       mirroring the movie-level ``RequestStatus.evicted`` semantics.
    3. Otherwise (no ``completed`` season -- rule 1 already claimed those) every
       remaining season is one of ``pending``/``available``/``failed``/
       ``evicted``/``cancelled``. ``available`` is now the ONLY real-done state
       reachable here — something is actually watchable right now.
       ``"partially_available"`` must only ever be reported when at least one
       season is real-done; an ``evicted``/``cancelled`` season never earns it on
       its own, because nothing about an eviction or a cancellation leaves
       anything watchable behind:
       - every season done, all ``available``, none gone -> ``"available"``
       - an ``available`` season mixed with a gone (``evicted``/``cancelled``)
         one (no ``pending``/``failed``) -> ``"partially_available"`` (some
         seasons present, one reclaimed/never fetched)
       - an ``available`` season mixed with any ``pending``/``failed``/gone ->
         ``"partially_available"`` (honest, and non-terminal so the unfinished
         season stays grabbable)
       - NO ``available`` season is present (only ``evicted``/``cancelled``/
         ``pending``/``failed``, e.g. one season evicted and another failed) ->
         nothing is actually watchable, so this must NOT read
         ``"partially_available"``; ``evicted``/``cancelled`` fold in alongside
         ``failed`` for this purpose (all three mean "nothing on disk for this
         season right now") and the remaining ``pending``/``failed`` rule
         applies: any ``pending`` -> ``"pending"``, else -> ``"failed"``

    Pure and total over the season-status vocabulary: every combination of
    ``pending``/``searching``/``no_acceptable_release``/``downloading``/
    ``completed``/``available``/``failed``/``import_blocked``/``evicted``/
    ``cancelled`` resolves to exactly one branch above. Raises :class:`ValueError`
    on an empty sequence —
    a TV request always has at least one season once ``ensure_seasons`` has run, so
    an empty rollup input is a caller bug, never a state to silently guess at.

    Also raises :class:`ValueError` (issue #79) when any status is OUTSIDE the
    ten-member season-status vocabulary above -- most notably the PARENT-ONLY
    ``"partially_available"`` (this function's own rollup OUTPUT, never a value a
    real season row's status column can hold), but equally any unrecognized string
    (a typo, a migration gap, or a future ``RequestStatus`` member added without
    first updating :data:`_VALID_SEASON_STATUSES`). Silently folding such a value
    through the branches below would land it in the terminal ``"failed"`` and
    report a false settled failure for the whole show -- honesty over silence
    means that surfaces as a loud error at the fold boundary instead.
    """
    if not season_statuses:
        raise ValueError("rollup_status requires at least one season status")

    statuses = set(season_statuses)
    unknown = statuses - _VALID_SEASON_STATUSES
    if unknown:
        raise ValueError(
            "rollup_status received unknown or parent-only season status value(s): "
            f"{sorted(unknown)!r}"
        )

    for status in _PRECEDENCE_STATUSES:
        if status in statuses:
            return status

    if statuses == {"evicted"}:
        # The whole show's tracked content was reclaimed by the disk-pressure
        # sweep -- honest, non-terminal, re-requestable at the show level too.
        return "evicted"

    if statuses == {"cancelled"}:
        # The operator cancelled every tracked season (ADR-0014's cancel verb):
        # the whole show rolls up to the SETTLED ``cancelled`` too. Mirrors the
        # all-``evicted`` case just above -- a fresh request re-tracks the show.
        return "cancelled"

    if statuses == {"waiting_for_air_date"}:
        return "waiting_for_air_date"

    # Only pending/available/failed/evicted/cancelled can remain among the season
    # statuses here -- ``completed`` is impossible past this point, the precedence
    # loop above always claims it first (issue #265). ``evicted`` and ``cancelled``
    # fold alongside ``available`` as "gone/done" for the purposes of this branch,
    # EXCEPT neither may ever let the whole show read as cleanly "available"
    # (their file is gone / was never fetched) -- see the docstring above. They
    # are grouped identically here: both mean "nothing on disk for this season
    # right now".
    _DONE = {"available"}
    _GONE = {"evicted", "cancelled"}
    _DONE_OR_GONE = _DONE | _GONE
    # Every season is done-or-gone AND at least one is REAL-DONE (available). A
    # ``statuses & _DONE`` guard is REQUIRED: an all-GONE mix with no done season
    # (e.g. ``{evicted, cancelled}`` -- neither single-type early return above
    # caught it) has nothing watchable and must NOT read partially_available; it
    # falls through to the pending/failed tail below instead.
    if statuses <= _DONE_OR_GONE and statuses & _DONE:
        if statuses == {"available"}:
            return "available"
        # Every season is available/evicted/cancelled, at least one is available,
        # and (since the ``{"available"}`` case was just excluded) at least one is
        # gone -- some seasons present, one reclaimed/never fetched.
        return "partially_available"
    if statuses & _DONE:
        # At least one REAL-DONE (``available``) season is mixed with a
        # pending/failed/evicted/cancelled one: honestly partial -- something is
        # actually watchable right now, and never a terminal status that would mask
        # the unfinished season or block its grab. Deliberately checked against
        # ``_DONE`` (not ``_DONE_OR_GONE``): an ``evicted``/``cancelled`` season
        # must never by itself earn "partially_available" -- see the docstring.
        return "partially_available"
    if "pending" in statuses:
        return "pending"
    if "waiting_for_air_date" in statuses:
        return "waiting_for_air_date"
    return "failed"
