"""Decision engine — the assembled pure brain (parse -> gate -> filter -> rank).

Mirrors Radarr's decision-engine specification pipeline, in a fixed order:

1. **parse** each candidate title via the injected :class:`ParserPort`;
2. **media-identity gate** (injected ``media_match``) — a release that does not
   name the wanted title (an indexer that ignored the id, a stale mapping) is a
   *permanent* rejection and is never scored; this guards the north star "do NOT
   grab the wrong media" and runs BEFORE quality so a high-quality wrong release
   can never out-rank a correct one;
3. **quality hard gate** (:func:`check_quality`) — a disallowed/absent quality is
   a *permanent* rejection and is never scored (north-star hard cutoff);
4. **blocklist filter** — a previously failed/reported release is an
   unconditional skip;
5. **score / sort** the survivors, best-first.

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
from plex_manager.domain.source_mapping import resolve_quality
from plex_manager.ports.parser import ParserPort

__all__ = ["BlocklistCheck", "DecisionResult", "MediaMatchCheck", "decide"]

# Returns True when the (candidate, parsed) pair is blocklisted. The caller wires
# this to a BlocklistRepository-backed check; the engine stays pure.
BlocklistCheck = Callable[[CandidateRelease, ParsedRelease], bool]

# Returns True when the (candidate, parsed) pair actually names the wanted media.
# The caller builds this from the request's expected (title, year, tmdb id) via
# the pure ``matches_media`` helper; the engine stays pure and only sees the hook.
MediaMatchCheck = Callable[[CandidateRelease, ParsedRelease], bool]

# Weighting so the composite score reproduces the comparator ordering: profile
# index dominates seeders, which dominates size. The gaps are far larger than any
# realistic field value (seeders < 1e9, size < 1e15 bytes => contribution < 1e6).
_INDEX_WEIGHT = 1e12
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


def _score(profile_index: int, candidate: CandidateRelease) -> float:
    seeders = candidate.seeders or 0
    return (
        profile_index * _INDEX_WEIGHT
        + seeders * _SEEDER_WEIGHT
        + candidate.size_bytes * _SIZE_WEIGHT
    )


def decide(
    candidates: list[CandidateRelease],
    parser: ParserPort,
    profile: QualityProfile,
    media_match: MediaMatchCheck,
    is_blocklisted: BlocklistCheck,
) -> DecisionResult:
    """Run the parse -> match -> gate -> filter -> rank pipeline over ``candidates``."""
    accepted: list[ScoredRelease] = []
    rejected: list[tuple[CandidateRelease, RejectionReason]] = []

    for candidate in candidates:
        parsed = parser.parse(candidate.title)

        # Media-identity gate FIRST: a release for a different movie/show is never
        # scored, so a high-quality wrong release can't out-rank a correct one.
        if not media_match(candidate, parsed):
            rejected.append((candidate, RejectionReason.WRONG_MEDIA))
            continue

        quality = resolve_quality(parsed.source, parsed.resolution, parsed.modifier)

        verdict = check_quality(quality, profile)
        if not verdict.accepted:
            rejected.append((candidate, verdict.reason or RejectionReason.QUALITY_NOT_WANTED))
            continue

        if is_blocklisted(candidate, parsed):
            rejected.append((candidate, RejectionReason.BLOCKLISTED))
            continue

        # The quality passed the gate, so it is present in the profile.
        index = profile.get_index(quality.id)
        profile_index = index if index is not None else -1
        accepted.append(
            ScoredRelease(
                candidate=candidate,
                parsed=parsed,
                quality=quality,
                profile_index=profile_index,
                score=_score(profile_index, candidate),
            )
        )

    def _compare(left: ScoredRelease, right: ScoredRelease) -> int:
        by_quality = compare_by_profile(left.quality, right.quality, profile)
        if by_quality != 0:
            return by_quality
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
