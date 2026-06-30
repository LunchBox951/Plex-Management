"""decision_service — preview ranks the good release and rejects CAM/TS."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.adapters.parser.guessit_adapter import GuessitParser
from plex_manager.domain.quality_profile import default_profile
from plex_manager.models import Blocklist, BlocklistReason
from plex_manager.repositories.blocklist import SqlBlocklistRepository
from plex_manager.services import decision_service
from tests.web.fakes import FakeProwlarr, good_and_cam_candidates

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
