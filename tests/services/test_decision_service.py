"""decision_service — preview ranks the good release and rejects CAM/TS."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.adapters.parser.guessit_adapter import GuessitParser
from plex_manager.domain.quality_profile import default_profile
from plex_manager.domain.quality_service import RejectionReason
from plex_manager.domain.release import CandidateRelease
from plex_manager.models import Blocklist, BlocklistReason
from plex_manager.repositories.blocklist import SqlBlocklistRepository
from plex_manager.services import decision_service, log_capture_service
from tests.web.fakes import FakeProwlarr, candidate, good_and_cam_candidates

SessionMaker = async_sessionmaker[AsyncSession]


async def test_preview_accepts_good_rejects_prerelease(sessionmaker_: SessionMaker) -> None:
    async with sessionmaker_() as session:
        result = await decision_service.preview(
            FakeProwlarr(good_and_cam_candidates()),
            GuessitParser(),
            default_profile(),
            SqlBlocklistRepository(session),
            tmdb_id=603,
            title="Some Movie",
            media_type="movie",
            year=2020,
        )

    assert [s.quality.name for s in result.accepted] == ["WEBDL-1080p"]
    assert result.no_acceptable_release is False
    rejected_titles = {c.title for c, _ in result.rejected}
    assert "Some.Movie.2020.CAM.x264-GROUP" in rejected_titles


async def test_preview_skips_blocklisted_release(sessionmaker_: SessionMaker) -> None:
    async with sessionmaker_() as session:
        session.add(
            Blocklist(
                source_title="Some.Movie.2020.1080p.WEB-DL.x264-GROUP",
                reason=BlocklistReason.failed,
                tmdb_id=603,
                media_type="movie",
                torrent_hash="3" * 40,
            )
        )
        await session.commit()

    async with sessionmaker_() as session:
        result = await decision_service.preview(
            FakeProwlarr(good_and_cam_candidates()),
            GuessitParser(),
            default_profile(),
            SqlBlocklistRepository(session),
            tmdb_id=603,
            title="Some Movie",
            media_type="movie",
        )

    # The good release is now blocklisted (by hash) -> nothing acceptable remains.
    assert result.accepted == ()
    assert result.no_acceptable_release is True
    reasons = {reason.value for _, reason in result.rejected}
    assert "blocklisted" in reasons


async def test_preview_does_not_cross_blocklist_movie_and_tv_with_same_tmdb_id(
    sessionmaker_: SessionMaker,
) -> None:
    """TMDB ids are scoped by media type. A failed movie release must not block a TV
    release that happens to use the same numeric id."""
    shared_id = 424242
    tv_hash = "d" * 40
    async with sessionmaker_() as session:
        session.add(
            Blocklist(
                source_title="Some.Movie.2020.1080p.WEB-DL.x264-GROUP",
                reason=BlocklistReason.failed,
                tmdb_id=shared_id,
                media_type="movie",
                torrent_hash=tv_hash,
                indexer="FakeIndexer",
            )
        )
        await session.commit()

    async with sessionmaker_() as session:
        result = await decision_service.preview(
            FakeProwlarr(
                [
                    candidate(
                        "Some.Show.S01.1080p.WEB-DL.x264-GROUP",
                        info_hash=tv_hash,
                    )
                ]
            ),
            GuessitParser(),
            default_profile(),
            SqlBlocklistRepository(session),
            tmdb_id=shared_id,
            title="Some Show",
            media_type="tv",
            season=1,
        )

    assert [s.candidate.title for s in result.accepted] == ["Some.Show.S01.1080p.WEB-DL.x264-GROUP"]
    assert result.rejected == ()


async def test_preview_rejects_wrong_title_even_at_top_quality(
    sessionmaker_: SessionMaker,
) -> None:
    # Prowlarr returned a pristine BluRay-1080p release for a DIFFERENT movie
    # (an indexer that ignored the tmdbid param). It must be rejected WRONG_MEDIA
    # and never grabbed, while the correct lower-quality WEBDL is accepted.
    candidates = [
        candidate("Wrong.Movie.2020.1080p.BluRay.x264-GROUP", info_hash="a" * 40, seeders=900),
        candidate("Some.Movie.2020.1080p.WEB-DL.x264-GROUP", info_hash="b" * 40, seeders=10),
    ]
    async with sessionmaker_() as session:
        result = await decision_service.preview(
            FakeProwlarr(candidates),
            GuessitParser(),
            default_profile(),
            SqlBlocklistRepository(session),
            tmdb_id=603,
            title="Some Movie",
            media_type="movie",
            year=2020,
        )

    assert [s.candidate.title for s in result.accepted] == [
        "Some.Movie.2020.1080p.WEB-DL.x264-GROUP"
    ]
    rejected = {c.title: reason for c, reason in result.rejected}
    assert rejected["Wrong.Movie.2020.1080p.BluRay.x264-GROUP"] is RejectionReason.WRONG_MEDIA


async def test_preview_accepts_tv_episode_despite_series_year(
    sessionmaker_: SessionMaker,
) -> None:
    # A TV request carries the show's first-air year, but a per-episode release
    # name (SxxExx) legitimately omits any year and Prowlarr maps TV via tvdb, so
    # the candidate has tmdb_id=0. The media gate must NOT reject a correctly
    # titled episode as WRONG_MEDIA just because the series year can't be matched.
    candidates = [
        candidate("The.Mandalorian.S02E04.1080p.WEB-DL.x264-GROUP", info_hash="d" * 40),
    ]
    async with sessionmaker_() as session:
        result = await decision_service.preview(
            FakeProwlarr(candidates),
            GuessitParser(),
            default_profile(),
            SqlBlocklistRepository(session),
            tmdb_id=82856,
            title="The Mandalorian",
            media_type="tv",
            year=2019,
            season=2,
        )

    assert [s.candidate.title for s in result.accepted] == [
        "The.Mandalorian.S02E04.1080p.WEB-DL.x264-GROUP"
    ]
    assert result.no_acceptable_release is False
    assert result.rejected == ()


async def test_preview_prefers_season_pack_when_whole_season_requested(
    sessionmaker_: SessionMaker,
) -> None:
    # Same quality, same seeders, and the pack is even SMALLER (so the plain size
    # tiebreak would otherwise rank it second) -- a whole-season request (season
    # set, no specific episodes) must still prefer the pack.
    pack = candidate(
        "The.Mandalorian.S02.1080p.WEB-DL.x264-GROUP",
        info_hash="e" * 40,
        seeders=10,
        size_bytes=1_000_000_000,
    )
    single = candidate(
        "The.Mandalorian.S02E04.1080p.WEB-DL.x264-GROUP",
        info_hash="f" * 40,
        seeders=10,
        size_bytes=2_000_000_000,
    )
    async with sessionmaker_() as session:
        result = await decision_service.preview(
            FakeProwlarr([single, pack]),
            GuessitParser(),
            default_profile(),
            SqlBlocklistRepository(session),
            tmdb_id=82856,
            title="The Mandalorian",
            media_type="tv",
            year=2019,
            season=2,
        )

    assert [s.candidate.title for s in result.accepted] == [
        "The.Mandalorian.S02.1080p.WEB-DL.x264-GROUP",
        "The.Mandalorian.S02E04.1080p.WEB-DL.x264-GROUP",
    ]


async def test_preview_does_not_prefer_season_pack_when_episodes_are_named(
    sessionmaker_: SessionMaker,
) -> None:
    # The SAME two candidates as above, but the operator named a specific episode:
    # prefer_season_pack must NOT fire, so the plain size tiebreak decides (the
    # bigger single-episode release ranks first) -- and the named episode is wired
    # onto the search request itself.
    pack = candidate(
        "The.Mandalorian.S02.1080p.WEB-DL.x264-GROUP",
        info_hash="e" * 40,
        seeders=10,
        size_bytes=1_000_000_000,
    )
    single = candidate(
        "The.Mandalorian.S02E04.1080p.WEB-DL.x264-GROUP",
        info_hash="f" * 40,
        seeders=10,
        size_bytes=2_000_000_000,
    )
    prowlarr = FakeProwlarr([pack, single])
    async with sessionmaker_() as session:
        result = await decision_service.preview(
            prowlarr,
            GuessitParser(),
            default_profile(),
            SqlBlocklistRepository(session),
            tmdb_id=82856,
            title="The Mandalorian",
            media_type="tv",
            year=2019,
            season=2,
            episodes=[4],
        )

    assert [s.candidate.title for s in result.accepted] == [
        "The.Mandalorian.S02E04.1080p.WEB-DL.x264-GROUP",
        "The.Mandalorian.S02.1080p.WEB-DL.x264-GROUP",
    ]
    assert prowlarr.searched[-1].episode == "4"


async def test_preview_rejects_wrong_season_pack(sessionmaker_: SessionMaker) -> None:
    # A tracker ignored Prowlarr's season param and returned an S01 pack for an S02
    # request. The pack still carries the show's correct identity, so only the
    # season gate stops it — it must be rejected WRONG_MEDIA, never grabbed.
    candidates = [
        candidate("The.Mandalorian.S01.1080p.WEB-DL.x264-GROUP", info_hash="e" * 40),
        candidate("The.Mandalorian.S02.1080p.WEB-DL.x264-GROUP", info_hash="f" * 40),
    ]
    async with sessionmaker_() as session:
        result = await decision_service.preview(
            FakeProwlarr(candidates),
            GuessitParser(),
            default_profile(),
            SqlBlocklistRepository(session),
            tmdb_id=82856,
            title="The Mandalorian",
            media_type="tv",
            year=2019,
            season=2,
        )

    # Only the S02 pack survives; the S01 pack is a wrong-season reject.
    assert [s.candidate.title for s in result.accepted] == [
        "The.Mandalorian.S02.1080p.WEB-DL.x264-GROUP"
    ]
    rejected = {c.title: reason for c, reason in result.rejected}
    assert rejected["The.Mandalorian.S01.1080p.WEB-DL.x264-GROUP"] is RejectionReason.WRONG_MEDIA


async def test_preview_rejects_multi_season_pack_for_single_season_request(
    sessionmaker_: SessionMaker,
) -> None:
    # Issue #24 beta posture: an S01-S03 pack that plausibly covers the requested
    # S02 (correct title/tmdb id, season identity gate passes) is still a
    # PERMANENT rejection -- never grabbed, never preferred -- because this app's
    # one-download-one-season model can't satisfy several seasons from one grab.
    # Only the exact single-season pack survives.
    multi = candidate(
        "The.Mandalorian.S01-S03.COMPLETE.1080p.WEB-DL.x264-GROUP",
        info_hash="e" * 40,
        seeders=900,
    )
    single_season_pack = candidate(
        "The.Mandalorian.S02.1080p.WEB-DL.x264-GROUP", info_hash="f" * 40, seeders=10
    )
    async with sessionmaker_() as session:
        result = await decision_service.preview(
            FakeProwlarr([multi, single_season_pack]),
            GuessitParser(),
            default_profile(),
            SqlBlocklistRepository(session),
            tmdb_id=82856,
            title="The Mandalorian",
            media_type="tv",
            year=2019,
            season=2,
        )

    assert [s.candidate.title for s in result.accepted] == [
        "The.Mandalorian.S02.1080p.WEB-DL.x264-GROUP"
    ]
    rejected = {c.title: reason for c, reason in result.rejected}
    assert (
        rejected["The.Mandalorian.S01-S03.COMPLETE.1080p.WEB-DL.x264-GROUP"]
        is RejectionReason.MULTI_SEASON_PACK
    )


async def test_preview_episode_scoped_multi_season_pack_surfaces_multi_season_not_wrong_media(
    sessionmaker_: SessionMaker,
) -> None:
    # Codex PR #33 finding (honesty over silence): an EPISODE-scoped preview
    # (specific episodes named) against an S01-S03 pack that covers the requested
    # S02 must surface RejectionReason.MULTI_SEASON_PACK -- the accurate reason
    # from the decision-engine gate -- NOT WRONG_MEDIA. The episode-overlap check
    # (covers_requested_episodes) runs INSIDE the media-identity gate, which fires
    # BEFORE the multi-season gate; the helper must PASS the pack so the true
    # reason wins instead of the pack mis-surfacing as WRONG_MEDIA. (The whole-
    # season variant above never hits the helper -- episodes is empty there -- so
    # this episode-scoped case is the one the finding is about.) The rejection
    # itself must remain airtight: the pack is still never accepted.
    multi = candidate(
        "The.Mandalorian.S01-S03.COMPLETE.1080p.WEB-DL.x264-GROUP",
        info_hash="e" * 40,
        seeders=900,
    )
    async with sessionmaker_() as session:
        result = await decision_service.preview(
            FakeProwlarr([multi]),
            GuessitParser(),
            default_profile(),
            SqlBlocklistRepository(session),
            tmdb_id=82856,
            title="The Mandalorian",
            media_type="tv",
            year=2019,
            season=2,
            episodes=[4],
        )

    assert result.accepted == ()
    assert result.no_acceptable_release is True
    rejected = {c.title: reason for c, reason in result.rejected}
    assert (
        rejected["The.Mandalorian.S01-S03.COMPLETE.1080p.WEB-DL.x264-GROUP"]
        is RejectionReason.MULTI_SEASON_PACK
    )


async def test_preview_rejects_wrong_episode_even_at_top_quality(
    sessionmaker_: SessionMaker,
) -> None:
    """F4: a request scoped to a specific episode (E04) must never accept/rank a
    same-season release for a DIFFERENT episode (E01), even when a tracker
    returns it at a HIGHER quality that would otherwise win the top pick. The
    right episode and the season pack both still accept."""
    wrong_episode_top_quality = candidate(
        "The.Mandalorian.S02E01.2160p.WEB-DL.x264-GROUP", info_hash="1" * 40, seeders=999
    )
    right_episode = candidate(
        "The.Mandalorian.S02E04.1080p.WEB-DL.x264-GROUP", info_hash="2" * 40, seeders=10
    )
    pack = candidate("The.Mandalorian.S02.1080p.WEB-DL.x264-GROUP", info_hash="3" * 40, seeders=10)
    async with sessionmaker_() as session:
        result = await decision_service.preview(
            FakeProwlarr([wrong_episode_top_quality, right_episode, pack]),
            GuessitParser(),
            default_profile(),
            SqlBlocklistRepository(session),
            tmdb_id=82856,
            title="The Mandalorian",
            media_type="tv",
            year=2019,
            season=2,
            episodes=[4],
        )

    accepted_titles = [s.candidate.title for s in result.accepted]
    assert wrong_episode_top_quality.title not in accepted_titles
    assert right_episode.title in accepted_titles
    assert pack.title in accepted_titles
    # Critically, the top pick (what _select_release/grab would default to) is
    # never the wrong-episode release, even though it is the highest quality.
    assert result.accepted[0].candidate.title != wrong_episode_top_quality.title
    rejected = {c.title: reason for c, reason in result.rejected}
    assert rejected[wrong_episode_top_quality.title] is RejectionReason.WRONG_MEDIA


async def test_preview_multi_episode_request_rejects_partial_single_episode(
    sessionmaker_: SessionMaker,
) -> None:
    """issue #70: a request scoped to BOTH E04 and E05 must NOT accept a single-
    episode S02E04 release that covers only PART of the request -- any-overlap
    would have grabbed it and then blocked import on the missing E05. A release
    that COVERS the whole request (a multi-episode E04-E05 file, or a whole-season
    pack) is still accepted, so the operator is never left with nothing grabbable
    when a complete alternative exists."""
    partial = candidate(
        "The.Mandalorian.S02E04.2160p.WEB-DL.x264-GROUP", info_hash="1" * 40, seeders=999
    )
    complete_multi = candidate(
        "The.Mandalorian.S02E04-E05.1080p.WEB-DL.x264-GROUP", info_hash="2" * 40, seeders=10
    )
    pack = candidate("The.Mandalorian.S02.1080p.WEB-DL.x264-GROUP", info_hash="3" * 40, seeders=10)
    async with sessionmaker_() as session:
        result = await decision_service.preview(
            FakeProwlarr([partial, complete_multi, pack]),
            GuessitParser(),
            default_profile(),
            SqlBlocklistRepository(session),
            tmdb_id=82856,
            title="The Mandalorian",
            media_type="tv",
            year=2019,
            season=2,
            episodes=[4, 5],
        )

    accepted_titles = [s.candidate.title for s in result.accepted]
    # The partial single-episode release is rejected even though it is the highest
    # quality; the complete multi-episode file and the whole-season pack accept.
    assert partial.title not in accepted_titles
    assert complete_multi.title in accepted_titles
    assert pack.title in accepted_titles
    assert result.accepted[0].candidate.title != partial.title
    rejected = {c.title: reason for c, reason in result.rejected}
    assert rejected[partial.title] is RejectionReason.WRONG_MEDIA


async def test_preview_whole_season_request_still_accepts_all_episodes(
    sessionmaker_: SessionMaker,
) -> None:
    """No-regression: with NO specific episodes named (a whole-season request),
    the episode-overlap gate must not fire -- every episode of the season,
    including one that would be "wrong" under an episode-scoped request, is a
    legitimate accept."""
    e01 = candidate("The.Mandalorian.S02E01.2160p.WEB-DL.x264-GROUP", info_hash="1" * 40)
    e04 = candidate("The.Mandalorian.S02E04.1080p.WEB-DL.x264-GROUP", info_hash="2" * 40)
    pack = candidate("The.Mandalorian.S02.1080p.WEB-DL.x264-GROUP", info_hash="3" * 40)
    async with sessionmaker_() as session:
        result = await decision_service.preview(
            FakeProwlarr([e01, e04, pack]),
            GuessitParser(),
            default_profile(),
            SqlBlocklistRepository(session),
            tmdb_id=82856,
            title="The Mandalorian",
            media_type="tv",
            year=2019,
            season=2,
        )
    assert {s.candidate.title for s in result.accepted} == {e01.title, e04.title, pack.title}


async def test_preview_movie_unaffected_by_episode_overlap_gate(
    sessionmaker_: SessionMaker,
) -> None:
    """No-regression: a movie preview (``episodes`` always ``None``) is untouched
    by the TV-only episode-overlap gate."""
    async with sessionmaker_() as session:
        result = await decision_service.preview(
            FakeProwlarr(good_and_cam_candidates()),
            GuessitParser(),
            default_profile(),
            SqlBlocklistRepository(session),
            tmdb_id=603,
            title="Some Movie",
            media_type="movie",
            year=2020,
        )
    assert [s.quality.name for s in result.accepted] == ["WEBDL-1080p"]


async def test_preview_rejects_mismatched_tmdb_id(sessionmaker_: SessionMaker) -> None:
    # A candidate whose own tmdb id disagrees with the request's tmdb id is a
    # definitive wrong-media reject — the title looks right but the id is decisive.
    wrong_id = CandidateRelease(
        guid="g1",
        title="Some.Movie.2020.1080p.WEB-DL.x264-GROUP",
        size_bytes=1_000_000_000,
        magnet_url="magnet:?xt=urn:btih:" + "c" * 40,
        info_hash="c" * 40,
        seeders=50,
        indexer_id=1,
        indexer_name="FakeIndexer",
        tmdb_id=999999,
        publish_date=datetime(2020, 1, 1, tzinfo=UTC),
    )
    async with sessionmaker_() as session:
        result = await decision_service.preview(
            FakeProwlarr([wrong_id]),
            GuessitParser(),
            default_profile(),
            SqlBlocklistRepository(session),
            tmdb_id=603,
            title="Some Movie",
            media_type="movie",
            year=2020,
        )

    assert result.accepted == ()
    assert result.no_acceptable_release is True
    assert [reason for _, reason in result.rejected] == [RejectionReason.WRONG_MEDIA]


async def test_preview_logs_multi_season_pack_rejection_telemetry(
    sessionmaker_: SessionMaker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Beta-week telemetry (issue #24): a preview that rejects a release
    MULTI_SEASON_PACK emits exactly ONE aggregated INFO -- never per-release spam
    -- carrying the count, up to 3 sample titles, tmdb_id, media_type, and season
    via ``extra=`` (CONTRIBUTING.md's logging convention). ``tmdb_id`` is a
    correlation key so it need not (and does not) appear in the message text;
    ``season``/``media_type``/the sample titles are not, so they DO -- otherwise
    this data would never reach ``log_events`` (see the docstring on
    ``_log_multi_season_pack_rejections``)."""
    multi = candidate(
        "The.Mandalorian.S01-S03.COMPLETE.1080p.WEB-DL.x264-GROUP",
        info_hash="e" * 40,
        seeders=900,
    )
    single_season_pack = candidate(
        "The.Mandalorian.S02.1080p.WEB-DL.x264-GROUP", info_hash="f" * 40, seeders=10
    )
    with caplog.at_level(logging.INFO, logger="plex_manager.services.decision_service"):
        async with sessionmaker_() as session:
            result = await decision_service.preview(
                FakeProwlarr([multi, single_season_pack]),
                GuessitParser(),
                default_profile(),
                SqlBlocklistRepository(session),
                tmdb_id=82856,
                title="The Mandalorian",
                media_type="tv",
                year=2019,
                season=2,
            )

    assert [s.candidate.title for s in result.accepted] == [
        "The.Mandalorian.S02.1080p.WEB-DL.x264-GROUP"
    ]
    infos = [r for r in caplog.records if r.levelname == "INFO"]
    assert len(infos) == 1, "expected exactly one aggregated INFO, never per-release spam"
    record = infos[0]
    # ``extra=`` sets each key as a plain LogRecord ATTRIBUTE (not a stdlib-known
    # one), so ``getattr`` -- never direct attribute access -- matches this
    # codebase's other structured-logging tests (e.g. test_request_service.py).
    assert getattr(record, "tmdb_id", None) == 82856
    assert getattr(record, "season", None) == 2
    assert getattr(record, "media_type", None) == "tv"
    assert getattr(record, "multi_season_pack_rejections", None) == 1
    assert getattr(record, "sample_titles", None) == [
        "The.Mandalorian.S01-S03.COMPLETE.1080p.WEB-DL.x264-GROUP"
    ]
    # The count/media_type/season/sample titles are NOT correlation keys, so they
    # must be readable straight off the persisted message text too (see the
    # docstring); tmdb_id is a correlation key and is deliberately NOT repeated.
    message = record.getMessage()
    assert "82856" not in message
    assert "media_type=tv" in message
    assert "season=2" in message
    assert "The.Mandalorian.S01-S03.COMPLETE.1080p.WEB-DL.x264-GROUP" in message


async def test_multi_season_telemetry_reaches_capture_at_warning_operator_floor(
    sessionmaker_: SessionMaker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Codex P2 (log-floor): at ``log_level=WARNING`` the multi-season-pack
    aggregate INFO (the issue-#24 beta dataset) used to be filtered at the
    ``_logger.info`` call before any handler ran -- the whole dataset silently
    never persisted. ``configure_logging`` now pins this module's logger to INFO
    (retention-telemetry precedent), so the record is still CREATED and flows to
    every root handler (the durable ``LogCaptureHandler`` and caplog alike),
    while ordinary INFO chatter from non-pinned loggers stays suppressed."""
    telemetry_name = log_capture_service.DECISION_TELEMETRY_LOGGER_NAME
    # Drift guard: the pin targets exactly the logger this module emits on.
    assert telemetry_name == decision_service.__name__

    multi = candidate(
        "The.Mandalorian.S01-S03.COMPLETE.1080p.WEB-DL.x264-GROUP",
        info_hash="e" * 40,
        seeders=900,
    )
    root = logging.getLogger()
    pinned = logging.getLogger(telemetry_name)
    saved_root_level = root.level
    saved_pinned_level = pinned.level
    # WARNING is the exact operator floor that used to drop the aggregate INFO.
    handler = log_capture_service.configure_logging("WARNING")
    try:
        async with sessionmaker_() as session:
            await decision_service.preview(
                FakeProwlarr([multi]),
                GuessitParser(),
                default_profile(),
                SqlBlocklistRepository(session),
                tmdb_id=82856,
                title="The Mandalorian",
                media_type="tv",
                year=2019,
                season=2,
            )
        # Control: an INFO on a NON-pinned sibling logger must still be dropped
        # by the WARNING floor -- proving the pin (not a permissive root) is what
        # lets the telemetry through.
        logging.getLogger("plex_manager.services.request_service").info("floor control")
    finally:
        log_capture_service.stop_logging(handler)
        root.setLevel(saved_root_level)
        pinned.setLevel(saved_pinned_level)

    aggregates = [
        r
        for r in caplog.records
        if r.name == telemetry_name and "multi-season-pack rejection(s)" in r.getMessage()
    ]
    assert len(aggregates) == 1  # created DESPITE the WARNING floor
    assert getattr(aggregates[0], "multi_season_pack_rejections", None) == 1
    assert not any(r.getMessage() == "floor control" for r in caplog.records)


async def test_preview_emits_no_telemetry_when_no_multi_season_pack_rejections(
    sessionmaker_: SessionMaker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No-regression: a preview with zero MULTI_SEASON_PACK rejections (CAM/TS
    rejects for a different reason entirely) must emit NOTHING from the new
    telemetry -- honesty over silence cuts both ways; an aggregate log firing on
    every preview would be noise, not signal."""
    with caplog.at_level(logging.INFO, logger="plex_manager.services.decision_service"):
        async with sessionmaker_() as session:
            result = await decision_service.preview(
                FakeProwlarr(good_and_cam_candidates()),
                GuessitParser(),
                default_profile(),
                SqlBlocklistRepository(session),
                tmdb_id=603,
                title="Some Movie",
                media_type="movie",
                year=2020,
            )

    assert result.no_acceptable_release is False
    assert caplog.records == []
