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
