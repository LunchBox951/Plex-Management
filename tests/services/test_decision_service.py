"""decision_service — preview ranks the good release and rejects CAM/TS."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.adapters.parser.guessit_adapter import GuessitParser
from plex_manager.domain.quality_profile import default_profile
from plex_manager.domain.quality_service import RejectionReason
from plex_manager.domain.release import CandidateRelease
from plex_manager.models import Blocklist, BlocklistReason
from plex_manager.repositories.blocklist import SqlBlocklistRepository
from plex_manager.services import decision_service
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
    assert result.accepted == []
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
    assert result.rejected == []


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
    assert result.rejected == []


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

    assert result.accepted == []
    assert result.no_acceptable_release is True
    assert [reason for _, reason in result.rejected] == [RejectionReason.WRONG_MEDIA]
