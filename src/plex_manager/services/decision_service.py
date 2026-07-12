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
before it can be scored — never silently grabbed. For a TV request naming
specific ``episodes``, the same hook additionally gates on
:func:`plex_manager.domain.season_pack.covers_requested_episodes`, so a
single-episode release for the right show/season but the WRONG episode (a
tracker that ignored or couldn't narrow to the requested episode) -- or one that
covers only PART of the requested episodes -- is rejected before it can
rank/grab, rather than caught only later at import time.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from plex_manager.domain.blocklist import BlocklistedRelease
from plex_manager.domain.blocklist import is_blocklisted as _is_blocklisted
from plex_manager.domain.decision_engine import DecisionResult, decide
from plex_manager.domain.media_match import matches_media
from plex_manager.domain.quality_service import RejectionReason
from plex_manager.domain.release import IndexerSearchRequest, MediaType
from plex_manager.domain.season_pack import covers_requested_episodes
from plex_manager.logsafe import safe_int, safe_text
from plex_manager.services.log_capture_service import DECISION_TELEMETRY_LOGGER_NAME

if TYPE_CHECKING:
    from plex_manager.domain.decision_engine import BlocklistCheck
    from plex_manager.domain.quality_profile import QualityProfile
    from plex_manager.domain.release import CandidateRelease, ParsedRelease
    from plex_manager.domain.season_pack import MultiSeasonRequestIntent
    from plex_manager.ports.indexer import IndexerPort
    from plex_manager.ports.parser import ParserPort
    from plex_manager.ports.repositories import BlocklistRepository

__all__ = ["preview", "preview_episode_fallback"]

# The constant equals this module's dotted path (i.e. ``__name__``); constructing
# the logger FROM it (retention-telemetry precedent) guarantees the emitter can
# never drift from the treatment ``configure_logging``/``prune_once`` key on that
# exact name: the INFO pin (an operator ``log_level`` of WARNING/ERROR would
# otherwise silently drop the issue-#24 beta aggregate below before the durable
# log sink ever saw it) and the 30-day retention floor. Module-logger scope is
# correct HERE -- the #24 aggregate is this module's ONLY log record, so module
# scope IS telemetry-only scope; ``auto_grab_service``, whose module logger also
# carries operational records, uses a dedicated ``.telemetry`` child instead
# (wave-6 finding: operational rows must not dodge the operator's retention).
_logger = logging.getLogger(DECISION_TELEMETRY_LOGGER_NAME)

#: Cap on sample release titles carried by the season-pack-scope rejection
#: telemetry INFOs below (issue #24's ``MULTI_SEASON_PACK`` and issue #167's
#: ``NOT_SEASON_PACK``) -- enough to spot-check which releases are showing up
#: without per-release log spam.
_MULTI_SEASON_PACK_SAMPLE_TITLES = 3


async def _blocklist_hook(
    blocklist_repo: BlocklistRepository, *, tmdb_id: int, media_type: str | None
) -> BlocklistCheck:
    """Fetch this media's blocklist entries once and close a pure check over them.

    Shared by :func:`preview` and :func:`preview_episode_fallback` so the two
    previews can never disagree about what "blocklisted" means -- the engine's
    ``is_blocklisted`` hook is synchronous, so the DB fetch happens here, once per
    preview call.
    """
    records = await blocklist_repo.list_for_media(tmdb_id, media_type=media_type)
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

    return _blocklisted


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
    multi_season_intent: MultiSeasonRequestIntent | None = None,
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
    operator asking for "the whole season" turns this into a *permanent*
    rejection gate (issue #167): any candidate that is not itself a season pack
    is rejected ``NOT_SEASON_PACK`` and never scored, because a single-episode
    release can never satisfy a whole-season request and must never be
    auto-grabbed just because every season pack was exhausted/blocklisted;
    naming specific episodes (or a movie, or an unscoped tv search) leaves the
    engine's default behavior untouched.
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

    # Scope the blocklist to this media's namespace: a movie and a show can share a
    # numeric tmdb_id, so a tmdb-id-only lookup would let one media type's blocklist
    # reject the other's candidates. ``"search"`` (untyped) imposes no scope.
    blocklist_media_type = media_type if media_type in ("movie", "tv") else None
    _blocklisted = await _blocklist_hook(
        blocklist_repo, tmdb_id=tmdb_id, media_type=blocklist_media_type
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
        if not matches_media(
            parsed,
            expected_title=title,
            expected_year=match_year,
            candidate_tmdb_id=candidate.tmdb_id,
            expected_tmdb_id=tmdb_id,
            expected_season=match_season,
        ):
            return False
        # Episode-coverage gate (TV only, specific episodes named): the season
        # gate above only confirms the release covers the right SEASON -- a
        # tracker that ignores (or can't narrow to) the requested episode(s) can
        # still return a single-episode release for a DIFFERENT episode of that
        # same season (e.g. S02E01 when E04 was requested), or a PARTIAL one (only
        # E04 when [4, 5] was requested). Without this, that wrong/partial-episode
        # release would rank/grab like any other accepted candidate, and the
        # importer would only catch it after the wrong torrent was already added --
        # a blocked download (import validation requires EVERY requested episode).
        # ``covers_requested_episodes`` now requires FULL coverage (issue #70), so a
        # single-episode release must contain EVERY requested episode to pass; a
        # whole-season pack always passes (it inherently contains them all). Movies
        # and whole-season requests (no specific episodes named) are unaffected
        # since ``episodes`` is empty/None then.
        if media_type != "tv" or not episodes:
            return True
        return covers_requested_episodes(parsed, episodes)

    # "Whole season" only: a season-scoped tv request with NO specific episodes
    # named. Naming episode(s) means the operator wants those episodes, not
    # necessarily the pack, so the tiebreak stays off.
    prefer_season_pack = media_type == "tv" and season is not None and not episodes
    result = decide(
        candidates,
        parser,
        profile,
        _media_match,
        _blocklisted,
        prefer_season_pack=prefer_season_pack,
        multi_season_intent=multi_season_intent,
    )
    _log_multi_season_pack_rejections(result, tmdb_id=tmdb_id, media_type=media_type, season=season)
    _log_not_season_pack_rejections(result, tmdb_id=tmdb_id, media_type=media_type, season=season)
    return result


async def preview_episode_fallback(
    prowlarr: IndexerPort,
    parser: ParserPort,
    profile: QualityProfile,
    blocklist_repo: BlocklistRepository,
    *,
    tmdb_id: int,
    title: str,
    season: int,
    missing_episodes: frozenset[int],
) -> DecisionResult:
    """Pass-2 episode-level fallback search (ADR-0020, issue #178).

    Only called by the auto-grab worker when Pass 1 (:func:`preview` with the
    derived ``prefer_season_pack=True``) found ZERO acceptable season packs THIS
    cycle -- the issue #167 hard gate stays intact as Pass 1; this is a same-cycle
    fallback, never a relaxed retry of the same search. Runs the SAME season-scoped
    indexer search as a whole-season preview (``episode=None`` -- a multi-value
    missing set has no single-value slot for the indexer query), but decides with
    ``prefer_season_pack=False`` and the engine's ``episode_subset`` gate set to
    ``missing_episodes`` instead: a candidate must cover a non-empty subset of the
    still-missing episodes (single episodes and partial multi-episode releases are
    both fine; anything overlapping an already-imported/downloading episode is
    rejected -- no redundant grabs).

    ``episodes`` is deliberately NOT passed to the media-identity hook (that
    coverage gate is for the explicit-episode request path, :func:`preview`'s
    ``episodes`` parameter) -- the engine's ``episode_subset`` gate is what
    enforces coverage here. No ``year`` gate either (mirrors :func:`preview`'s TV
    branch: a per-episode release name legitimately omits the show's first-air
    year).
    """
    request = IndexerSearchRequest(
        media_type="tv",
        query=title,
        tmdb_id=tmdb_id or None,
        year=None,
        season=season,
        episode=None,
    )
    candidates = await prowlarr.search(request)

    _blocklisted = await _blocklist_hook(blocklist_repo, tmdb_id=tmdb_id, media_type="tv")

    def _media_match(candidate: CandidateRelease, parsed: ParsedRelease) -> bool:
        return matches_media(
            parsed,
            expected_title=title,
            expected_year=None,
            candidate_tmdb_id=candidate.tmdb_id,
            expected_tmdb_id=tmdb_id,
            expected_season=season,
        )

    result = decide(
        candidates,
        parser,
        profile,
        _media_match,
        _blocklisted,
        prefer_season_pack=False,
        episode_subset=missing_episodes,
    )
    _log_episode_not_needed_rejections(result, tmdb_id=tmdb_id, season=season)
    return result


def _log_episode_not_needed_rejections(
    result: DecisionResult,
    *,
    tmdb_id: int,
    season: int | None,
) -> None:
    """Beta-week telemetry (issue #178): one aggregated INFO per fallback preview
    when any release was rejected ``EPISODE_NOT_NEEDED`` (a pack/whole-season
    release reaching Pass 2, a wrong episode, or a redundant already-covered
    one) -- never per-release spam. Same aggregation mechanics as
    :func:`_log_multi_season_pack_rejections` / :func:`_log_not_season_pack_rejections`.
    """
    _log_season_scope_rejections(
        result,
        reason=RejectionReason.EPISODE_NOT_NEEDED,
        label="episode-not-needed",
        count_key="episode_not_needed_rejections",
        sample_limit=_MULTI_SEASON_PACK_SAMPLE_TITLES,
        tmdb_id=tmdb_id,
        media_type="tv",
        season=season,
    )


def _log_multi_season_pack_rejections(
    result: DecisionResult,
    *,
    tmdb_id: int,
    media_type: str,
    season: int | None,
) -> None:
    """Beta-week telemetry (issue #24): one aggregated INFO per preview when any
    release was rejected ``MULTI_SEASON_PACK`` -- never per-release spam.

    See :func:`_log_season_scope_rejections` for the shared aggregation mechanics
    and the rationale for what goes in the message text vs. ``extra=`` only.
    """
    _log_season_scope_rejections(
        result,
        reason=RejectionReason.MULTI_SEASON_PACK,
        label="multi-season-pack",
        count_key="multi_season_pack_rejections",
        sample_limit=_MULTI_SEASON_PACK_SAMPLE_TITLES,
        tmdb_id=tmdb_id,
        media_type=media_type,
        season=season,
    )


def _log_not_season_pack_rejections(
    result: DecisionResult,
    *,
    tmdb_id: int,
    media_type: str,
    season: int | None,
) -> None:
    """Beta-week telemetry (issue #167): one aggregated INFO per preview when any
    release was rejected ``NOT_SEASON_PACK`` -- the observability parity counterpart
    to :func:`_log_multi_season_pack_rejections` for the sibling season-pack-only
    gate (:func:`plex_manager.domain.decision_engine.decide`'s ``prefer_season_pack``
    hard reject) -- never per-release spam.

    See :func:`_log_season_scope_rejections` for the shared aggregation mechanics
    and the rationale for what goes in the message text vs. ``extra=`` only.
    """
    _log_season_scope_rejections(
        result,
        reason=RejectionReason.NOT_SEASON_PACK,
        label="not-season-pack",
        count_key="not_season_pack_rejections",
        sample_limit=_MULTI_SEASON_PACK_SAMPLE_TITLES,
        tmdb_id=tmdb_id,
        media_type=media_type,
        season=season,
    )


def _log_season_scope_rejections(
    result: DecisionResult,
    *,
    reason: RejectionReason,
    label: str,
    count_key: str,
    sample_limit: int,
    tmdb_id: int,
    media_type: str,
    season: int | None,
) -> None:
    """Shared aggregation behind :func:`_log_multi_season_pack_rejections` and
    :func:`_log_not_season_pack_rejections`: one aggregated INFO per preview per
    ``reason`` -- never per-release spam.

    The decision choke point, not :func:`plex_manager.domain.decision_engine.decide`
    itself (domain stays pure/no logging). ``tmdb_id`` is a correlation key
    (``LOG_EVENT_CORRELATION_KEYS``, ADR-0012): ``log_capture_service`` already
    lifts it out of ``extra=`` into the durable row's structured, filterable
    ``context_json``, so it goes in ``extra=`` only -- repeating it in the message
    text would be inert duplication. ``media_type``/``season``/the sample titles
    are NOT correlation keys, though, and ``log_capture_service`` persists only a
    record's rendered message text plus that restricted context -- an
    ``extra=``-only field never reaches ``log_events`` at all. So they ARE
    interpolated into the text, or this telemetry's whole reason for existing
    (answerable from ``log_events`` after the beta week) would silently not hold.
    ``season`` and ``media_type`` can trace from an HTTP request body
    (``/api/v1/search-preview`` accepts an explicit descriptor, not just a stored
    ``request_id``), so ``season`` goes through ``logsafe.safe_int`` and
    ``media_type`` through ``safe_text``; the sample release titles are external
    Prowlarr text, so through ``safe_text`` too -- the same log-hygiene barrier
    (#35) used at every other request/indexer-derived log site, since CodeQL's
    py/log-injection taints ``extra=`` fields exactly like message args. Caller identity (auto-grab
    vs. manual preview/correction) is deliberately omitted: ``preview`` has no
    cheap way to distinguish its callers without a signature change, and this is
    log-only telemetry.
    """
    samples = [
        safe_text(candidate.title)
        for candidate, rejection_reason in result.rejected
        if rejection_reason is reason
    ]
    if not samples:
        return
    _logger.info(
        "decision preview: %d %s rejection(s) (media_type=%s season=%s); samples=%s",
        len(samples),
        label,
        safe_text(media_type),
        season if season is None else safe_int(season),
        samples[:sample_limit],
        extra={
            "tmdb_id": safe_int(tmdb_id),
            "season": season if season is None else safe_int(season),
            "media_type": safe_text(media_type),
            count_key: len(samples),
            "sample_titles": samples[:sample_limit],
        },
    )
