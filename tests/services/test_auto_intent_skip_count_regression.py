"""Regression probe: an intent conflict should settle auto-grab as skipped_active."""

from __future__ import annotations

from datetime import UTC, datetime

from plex_manager.adapters.parser.guessit_adapter import GuessitParser
from plex_manager.domain.quality_profile import default_profile
from plex_manager.models import MediaRequest, MediaType, RequestStatus
from plex_manager.ports.repositories import CreateDownloadAddIntent, DownloadAddIntentScopeCreate
from plex_manager.repositories.download_add_intents import SqlDownloadAddIntentRepository
from plex_manager.services import auto_grab_service
from tests.web.fakes import FakeProwlarr, FakeQbittorrent, good_and_cam_candidates


async def test_auto_grab_intent_conflict_counts_as_skipped_active(sessionmaker_) -> None:  # type: ignore[no-untyped-def]
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=603,
            media_type=MediaType.movie,
            title="Some Movie",
            year=2020,
            status=RequestStatus.pending,
        )
        session.add(request)
        await session.commit()
        request_id = request.id

    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        assert request is not None
        await SqlDownloadAddIntentRepository(session).create(
            CreateDownloadAddIntent(
                torrent_hash="intent-hash",
                media_request_id=request_id,
                tmdb_id=603,
                media_type="movie",
                save_path="",
                scopes=(
                    DownloadAddIntentScopeCreate(
                        tmdb_id=603, media_type="movie", scope_key="movie", is_target=True
                    ),
                ),
            )
        )
        await session.commit()

    qbt = FakeQbittorrent()
    async with sessionmaker_() as session:
        result = await auto_grab_service.run_grab_cycle(
            session,
            prowlarr=FakeProwlarr(good_and_cam_candidates()),
            parser=GuessitParser(),
            profile=default_profile(),
            qbt=qbt,
            now=datetime(2026, 7, 1, 12, 0, tzinfo=UTC),
            clock=lambda: datetime(2026, 7, 1, 12, 0, tzinfo=UTC),
        )

    assert qbt.added == []
    assert result.grabbed == 0
    assert result.no_acceptable == 0
    assert result.skipped_active == 1
