"""decision_service.preview_episode_fallback — Pass-2 episode-level fallback
search (ADR-0018, issue #178)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.adapters.parser.guessit_adapter import GuessitParser
from plex_manager.domain.quality_profile import default_profile
from plex_manager.domain.quality_service import RejectionReason
from plex_manager.models import Blocklist, BlocklistReason
from plex_manager.repositories.blocklist import SqlBlocklistRepository
from plex_manager.services import decision_service
from tests.web.fakes import FakeProwlarr, candidate

SessionMaker = async_sessionmaker[AsyncSession]


async def test_preview_episode_fallback_accepts_missing_rejects_pack_and_covered(
    sessionmaker_: SessionMaker,
) -> None:
    candidates = [
        candidate("Show.S02.1080p.WEB-DL.x264-GROUP", info_hash="1" * 40),
        candidate("Show.S02E04.1080p.WEB-DL.x264-GROUP", info_hash="2" * 40),
        # Episode 6 is already covered/imported -- not in missing_episodes.
        candidate("Show.S02E06.1080p.WEB-DL.x264-GROUP", info_hash="3" * 40),
    ]

    async with sessionmaker_() as session:
        result = await decision_service.preview_episode_fallback(
            FakeProwlarr(candidates),
            GuessitParser(),
            default_profile(),
            SqlBlocklistRepository(session),
            tmdb_id=901,
            title="Show",
            season=2,
            missing_episodes=frozenset({4, 5}),
        )

    accepted_titles = {s.candidate.title for s in result.accepted}
    assert accepted_titles == {"Show.S02E04.1080p.WEB-DL.x264-GROUP"}
    rejected_reasons = {c.title: reason for c, reason in result.rejected}
    assert (
        rejected_reasons["Show.S02.1080p.WEB-DL.x264-GROUP"] is RejectionReason.EPISODE_NOT_NEEDED
    )
    assert (
        rejected_reasons["Show.S02E06.1080p.WEB-DL.x264-GROUP"]
        is RejectionReason.EPISODE_NOT_NEEDED
    )


async def test_preview_episode_fallback_skips_blocklisted_release(
    sessionmaker_: SessionMaker,
) -> None:
    bad_hash = "4" * 40
    async with sessionmaker_() as session:
        session.add(
            Blocklist(
                source_title="Show.S02E04.1080p.WEB-DL.x264-GROUP",
                reason=BlocklistReason.failed,
                tmdb_id=902,
                media_type="tv",
                torrent_hash=bad_hash,
            )
        )
        await session.commit()

    async with sessionmaker_() as session:
        result = await decision_service.preview_episode_fallback(
            FakeProwlarr([candidate("Show.S02E04.1080p.WEB-DL.x264-GROUP", info_hash=bad_hash)]),
            GuessitParser(),
            default_profile(),
            SqlBlocklistRepository(session),
            tmdb_id=902,
            title="Show",
            season=2,
            missing_episodes=frozenset({4}),
        )

    assert result.accepted == ()
    reasons = {reason.value for _, reason in result.rejected}
    assert "blocklisted" in reasons


async def test_preview_episode_fallback_no_year_gate(sessionmaker_: SessionMaker) -> None:
    """A per-episode release name legitimately omits the show's first-air year --
    the fallback must not reject a correctly-titled episode for lacking one."""
    async with sessionmaker_() as session:
        result = await decision_service.preview_episode_fallback(
            FakeProwlarr([candidate("Show.S02E04.1080p.WEB-DL.x264-GROUP", info_hash="5" * 40)]),
            GuessitParser(),
            default_profile(),
            SqlBlocklistRepository(session),
            tmdb_id=903,
            title="Show",
            season=2,
            missing_episodes=frozenset({4}),
        )

    assert [s.candidate.title for s in result.accepted] == ["Show.S02E04.1080p.WEB-DL.x264-GROUP"]
