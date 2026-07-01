"""Decision preview — the headline path: Prowlarr search -> pure decision engine.

``preview`` runs an indexer search for a request, then hands the candidates to the
pure :func:`plex_manager.domain.decision_engine.decide` with the default quality
profile, the injected parser, and a blocklist-backed identity check. The result is
the ranked ``accepted`` list plus per-release rejection reasons plus the
``no_acceptable_release`` flag — never a swallowed empty list.

The engine's ``is_blocklisted`` hook is synchronous, so this service pre-fetches
the (already media-scoped) blocklist entries once and closes a pure check over
them via :func:`plex_manager.domain.blocklist.is_blocklisted` — the engine stays
pure and the DB is touched exactly once per preview.

The ``media_match`` hook is built from the request's expected (title, year, tmdb
id) and delegates to the pure :func:`plex_manager.domain.media_match.matches_media`
helper, so a release Prowlarr returned for a *different* title (an indexer that
ignored the ``tmdbid`` param, or a stale mapping) is rejected ``WRONG_MEDIA``
before it can be scored — never silently grabbed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from plex_manager.domain.blocklist import BlocklistedRelease
from plex_manager.domain.blocklist import is_blocklisted as _is_blocklisted
from plex_manager.domain.decision_engine import DecisionResult, decide
from plex_manager.domain.media_match import matches_media
from plex_manager.domain.release import IndexerSearchRequest, MediaType

if TYPE_CHECKING:
    from plex_manager.domain.quality_profile import QualityProfile
    from plex_manager.domain.release import CandidateRelease, ParsedRelease
    from plex_manager.ports.indexer import IndexerPort
    from plex_manager.ports.parser import ParserPort
    from plex_manager.ports.repositories import BlocklistRepository

__all__ = ["preview"]


async def preview(
    prowlarr: IndexerPort,
    parser: ParserPort,
    profile: QualityProfile,
    blocklist_repo: BlocklistRepository,
    *,
    tmdb_id: int,
    title: str,
    media_type: str,
    year: int | None = None,
    season: int | None = None,
    episodes: list[int] | None = None,
) -> DecisionResult:
    """Search the indexers and run the decision engine; return the ranked result.

    ``episodes`` (TV only) names the specific episode number(s) an operator wants
    out of ``season`` -- ``None``/empty means "the whole season". When exactly one
    episode is named, it is wired onto ``IndexerSearchRequest.episode`` so the
    indexer search itself narrows (the Prowlarr adapter already forwards
    season/episode params); more than one episode still searches the whole season
    (a multi-episode indexer query has no single-value slot) and relies on the
    later import-time per-file filter instead.

    ``prefer_season_pack`` is derived, never a caller-supplied flag: it is True
    only for a season-scoped tv request with NO specific episodes named -- an
    operator asking for "the whole season" should rank a season-pack release over
    an equivalent-quality single-episode one; naming specific episodes (or a
    movie, or an unscoped tv search) leaves the engine's default ranking
    untouched.
    """
    search_media_type: MediaType
    if media_type == "movie":
        search_media_type = "movie"
    elif media_type == "tv":
        search_media_type = "tv"
    else:
        search_media_type = "search"
    request = IndexerSearchRequest(
        media_type=search_media_type,
        query=title,
        tmdb_id=tmdb_id or None,
        year=year,
        season=season,
        episode=str(episodes[0]) if episodes and len(episodes) == 1 else None,
    )
    candidates = await prowlarr.search(request)

    records = await blocklist_repo.list_for_media(tmdb_id)
    entries = [
        BlocklistedRelease(
            source_title=record.source_title,
            info_hash=record.torrent_hash,
            indexer=record.indexer,
        )
        for record in records
    ]

    def _blocklisted(candidate: CandidateRelease, _parsed: ParsedRelease) -> bool:
        return _is_blocklisted(
            info_hash=candidate.info_hash,
            source_title=candidate.title,
            indexer=candidate.indexer_name,
            entries=entries,
        )

    # ``year`` is the wanted media's reference year. For a movie that is the
    # release year and the matcher should enforce it. For TV it is the show's
    # *first-air* year, which a per-episode release name legitimately omits
    # (``S02E04`` carries no year); gating on it would reject every correctly
    # named episode as WRONG_MEDIA. So only pass the year for movies.
    match_year = year if media_type == "movie" else None
    # Season identity is NOT taken on faith from the Prowlarr ``season`` param: a
    # tracker may ignore it and return another season (whose pack still carries the
    # show's correct tmdb id). For a season-scoped TV request, enforce the parsed
    # release's season in the gate too. ``None`` (movie, or no season requested)
    # leaves behaviour unchanged.
    match_season = season if media_type == "tv" else None

    def _media_match(candidate: CandidateRelease, parsed: ParsedRelease) -> bool:
        return matches_media(
            parsed,
            expected_title=title,
            expected_year=match_year,
            candidate_tmdb_id=candidate.tmdb_id,
            expected_tmdb_id=tmdb_id,
            expected_season=match_season,
        )

    # "Whole season" only: a season-scoped tv request with NO specific episodes
    # named. Naming episode(s) means the operator wants those episodes, not
    # necessarily the pack, so the tiebreak stays off.
    prefer_season_pack = media_type == "tv" and season is not None and not episodes
    return decide(
        candidates,
        parser,
        profile,
        _media_match,
        _blocklisted,
        prefer_season_pack=prefer_season_pack,
    )
