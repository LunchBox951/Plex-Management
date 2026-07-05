"""Decision engine — the assembled pure brain (parse -> gate -> filter -> rank).

Mirrors Radarr's decision-engine specification pipeline, in a fixed order:

1. **parse** each candidate title via the injected :class:`ParserPort`;
2. **media-identity gate** (injected ``media_match``) — a release that does not
   name the wanted title (an indexer that ignored the id, a stale mapping) is a
   *permanent* rejection and is never scored; this guards the north star "do NOT
   grab the wrong media" and runs BEFORE quality so a high-quality wrong release
   can never out-rank a correct one;
3. **multi-season-pack gate** (:func:`~plex_manager.domain.season_pack.classify_release_scope`)
   — a release spanning more than one season (``S01-S03``) is a *permanent*
   rejection, mirroring Sonarr's ``MultiSeasonSpecification``: this app's one
   download == one season data model can't satisfy several seasons from a single
   grab without stranding sibling ``SeasonRequest``s, so the beta posture is to
   never grab one, not to prefer it (see ADR/issue #24);
4. **quality hard gate** (:func:`check_quality`) — a disallowed/absent quality is
   a *permanent* rejection and is never scored (north-star hard cutoff);
5. **blocklist filter** — a previously failed/reported release is an
   unconditional skip;
6. **score / sort** the survivors, best-first.

There is deliberately **no relaxed-fallback retry**: if nothing survives, the
result carries ``no_acceptable_release=True`` as an observable state. The engine
never falls back to accepting a blocked or rejected source.

Ranking among allowed releases uses :func:`compare_by_profile` as the primary key
(profile order, not raw resolution), then seeders descending, then size as a
stable final tiebreak. The numeric :attr:`ScoredRelease.score` encodes the same
ordering for display.

Pure domain: ports Protocols + the local quality/release model + stdlib.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import cmp_to_key

from plex_manager.domain.quality_profile import QualityProfile
from plex_manager.domain.quality_service import (
    RejectionReason,
    check_quality,
    compare_by_profile,
)
from plex_manager.domain.release import CandidateRelease, ParsedRelease, ScoredRelease
from plex_manager.domain.season_pack import (
    MultiSeasonRequestIntent,
    classify_release_scope,
    plan_multi_season_pack,
)
from plex_manager.domain.source_mapping import resolve_quality
from plex_manager.ports.parser import ParserPort

# Release scopes that satisfy a WHOLE-season grab and so earn the season-pack
# preference: an exact single-season pack. It must rank ahead of a single-episode
# release for a whole-season request, or a higher-seeded single episode could
# out-rank a release that actually contains the whole requested season.
#
# A multi-season pack (``S01-S03``) is DELIBERATELY excluded: it is permanently
# rejected below (the ``_MULTI_SEASON_SCOPE`` gate) rather than preferred, so it
# never reaches scoring in the first place. See issue #24.
_PACK_SCOPES: frozenset[str] = frozenset({"season_pack"})

# The release scope :func:`classify_release_scope` reports for a pack that spans
# more than one season (``S01-S03``). Kept as a named constant so the gate and its
# tests can't drift from the classifier's string literal.
_MULTI_SEASON_SCOPE = "multi_season_pack"

__all__ = ["BlocklistCheck", "DecisionResult", "MediaMatchCheck", "decide"]

# Returns True when the (candidate, parsed) pair is blocklisted. The caller wires
# this to a BlocklistRepository-backed check; the engine stays pure.
BlocklistCheck = Callable[[CandidateRelease, ParsedRelease], bool]

# Returns True when the (candidate, parsed) pair actually names the wanted media.
# The caller builds this from the request's expected (title, year, tmdb id) via
# the pure ``matches_media`` helper; the engine stays pure and only sees the hook.
MediaMatchCheck = Callable[[CandidateRelease, ParsedRelease], bool]

# Weighting so the composite score reproduces the comparator ordering: profile
# index dominates the season-pack scope preference, which dominates seeders,
# which dominates size. The gaps are far larger than any realistic field value
# (seeders < 1e9, size < 1e15 bytes => contribution < 1e6).
_INDEX_WEIGHT = 1e12
# Only ever added when the caller opts in via ``prefer_season_pack`` (see
# :func:`decide`); with the default ``prefer_season_pack=False`` every candidate's
# contribution is 0, so the score is BYTE-IDENTICAL to the pre-season-pack engine.
_SCOPE_WEIGHT = 1e9
_SEEDER_WEIGHT = 1e3
_SIZE_WEIGHT = 1e-9


@dataclass(frozen=True)
class DecisionResult:
    """Outcome of a decision run over a candidate set.

    ``accepted`` is sorted best-first. ``rejected`` pairs each discarded candidate
    with its (surfaced, never-swallowed) reason. ``no_acceptable_release`` is True
    iff ``accepted`` is empty.
    """

    accepted: list[ScoredRelease]
    rejected: list[tuple[CandidateRelease, RejectionReason]]
    no_acceptable_release: bool


def _score(profile_index: int, candidate: CandidateRelease, *, is_season_pack: bool) -> float:
    seeders = candidate.seeders or 0
    scope_bonus = _SCOPE_WEIGHT if is_season_pack else 0.0
    return (
        profile_index * _INDEX_WEIGHT
        + scope_bonus
        + seeders * _SEEDER_WEIGHT
        + candidate.size_bytes * _SIZE_WEIGHT
    )


def decide(
    candidates: list[CandidateRelease],
    parser: ParserPort,
    profile: QualityProfile,
    media_match: MediaMatchCheck,
    is_blocklisted: BlocklistCheck,
    *,
    prefer_season_pack: bool = False,
    multi_season_intent: MultiSeasonRequestIntent | None = None,
) -> DecisionResult:
    """Run the parse -> match -> gate -> filter -> rank pipeline over ``candidates``.

    ``prefer_season_pack`` (default ``False``, byte-identical to the pre-season-pack
    engine) is set by the caller only when the operator explicitly requested a
    whole TV season: it adds a tiebreak (after profile order, before seeders) that
    prefers a release :func:`~plex_manager.domain.season_pack.classify_release_scope`
    classifies as a ``"season_pack"`` over one it does not. It never overrides the
    quality/identity/blocklist gates -- a season pack that fails the profile gate is
    still rejected.
    """
    accepted: list[ScoredRelease] = []
    rejected: list[tuple[CandidateRelease, RejectionReason]] = []

    for candidate in candidates:
        parsed = parser.parse(candidate.title)

        # Media-identity gate FIRST: a release for a different movie/show is never
        # scored, so a high-quality wrong release can't out-rank a correct one.
        if not media_match(candidate, parsed):
            rejected.append((candidate, RejectionReason.WRONG_MEDIA))
            continue

        scope = classify_release_scope(parsed)
        multi_season_plan = None
        if scope == _MULTI_SEASON_SCOPE and multi_season_intent is None:
            rejected.append((candidate, RejectionReason.MULTI_SEASON_PACK))
            continue

        quality = resolve_quality(parsed.source, parsed.resolution, parsed.modifier)

        verdict = check_quality(quality, profile)
        if not verdict.accepted:
            rejected.append((candidate, verdict.reason or RejectionReason.QUALITY_NOT_WANTED))
            continue

        if scope == _MULTI_SEASON_SCOPE:
            if multi_season_intent is None:  # pragma: no cover - guarded above
                rejected.append((candidate, RejectionReason.MULTI_SEASON_PACK))
                continue
            season_numbers = parsed.season if isinstance(parsed.season, list) else []
            multi_season_plan = plan_multi_season_pack(
                pack_seasons=season_numbers,
                candidate_quality_id=quality.id,
                profile=profile,
                intent=multi_season_intent,
            )
            if not multi_season_plan.accepted:
                rejected.append((candidate, RejectionReason.MULTI_SEASON_PACK))
                continue

        if is_blocklisted(candidate, parsed):
            rejected.append((candidate, RejectionReason.BLOCKLISTED))
            continue

        # The quality passed the gate, so it is present in the profile.
        index = profile.get_index(quality.id)
        profile_index = index if index is not None else -1
        is_season_pack = prefer_season_pack and classify_release_scope(parsed) in _PACK_SCOPES
        accepted.append(
            ScoredRelease(
                candidate=candidate,
                parsed=parsed,
                quality=quality,
                profile_index=profile_index,
                score=_score(profile_index, candidate, is_season_pack=is_season_pack),
                covered_seasons=(
                    multi_season_plan.covered_seasons if multi_season_plan is not None else ()
                ),
                target_seasons=(
                    multi_season_plan.target_seasons if multi_season_plan is not None else ()
                ),
                upgrade_seasons=(
                    multi_season_plan.upgrade_seasons if multi_season_plan is not None else ()
                ),
                waste_seasons=(
                    multi_season_plan.waste_seasons if multi_season_plan is not None else ()
                ),
                ignored_seasons=(
                    multi_season_plan.ignored_seasons if multi_season_plan is not None else ()
                ),
                skipped_seasons=(
                    (multi_season_plan.waste_seasons + multi_season_plan.ignored_seasons)
                    if multi_season_plan is not None
                    else ()
                ),
            )
        )

    def _compare(left: ScoredRelease, right: ScoredRelease) -> int:
        by_quality = compare_by_profile(left.quality, right.quality, profile)
        if by_quality != 0:
            return by_quality
        if prefer_season_pack:
            left_pack = classify_release_scope(left.parsed) in _PACK_SCOPES
            right_pack = classify_release_scope(right.parsed) in _PACK_SCOPES
            if left_pack != right_pack:
                return 1 if left_pack else -1
        left_seeders = left.candidate.seeders or 0
        right_seeders = right.candidate.seeders or 0
        if left_seeders != right_seeders:
            return (left_seeders > right_seeders) - (left_seeders < right_seeders)
        left_size = left.candidate.size_bytes
        right_size = right.candidate.size_bytes
        return (left_size > right_size) - (left_size < right_size)

    accepted.sort(key=cmp_to_key(_compare), reverse=True)
    return DecisionResult(
        accepted=accepted,
        rejected=rejected,
        no_acceptable_release=len(accepted) == 0,
    )
