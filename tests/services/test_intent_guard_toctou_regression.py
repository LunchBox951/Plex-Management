"""Regression: a committed reservation rejects a racing intent publication."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from plex_manager.adapters import encryption
from plex_manager.config import get_settings
from plex_manager.db import Base, enable_sqlite_fk_enforcement
from plex_manager.domain.quality import WEBDL1080P, QualitySource
from plex_manager.domain.release import ParsedRelease, ScoredRelease
from plex_manager.models import MediaRequest, MediaType, RequestStatus
from plex_manager.ports.download_client import PreparedAdd
from plex_manager.ports.repositories import CreateDownloadAddIntent, DownloadAddIntentScopeCreate
from plex_manager.repositories.download_add_intents import SqlDownloadAddIntentRepository
from plex_manager.services import grab_service
from tests.web.fakes import FakeQbittorrent, candidate


@pytest.fixture(autouse=True)
def key(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("PLEX_MANAGER_FERNET_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()
    encryption.reset_fernet_cache()
    yield
    get_settings.cache_clear()
    encryption.reset_fernet_cache()


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    value = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    enable_sqlite_fk_enforcement(value)
    async with value.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield value
    await value.dispose()


class PublishDuringAdd(FakeQbittorrent):
    def __init__(self, engine: AsyncEngine, request_id: int) -> None:
        super().__init__()
        self.engine = engine
        self.request_id = request_id
        self.conflict_refused = False

    async def add_prepared(self, prepared: PreparedAdd, save_path: str, category: str):  # type: ignore[no-untyped-def]
        # This is a distinct transaction, as another worker would use. The grab
        # committed its reservation before invoking this client seam, so the
        # competing publication must lose before it can mutate qBittorrent.
        async with AsyncSession(self.engine, expire_on_commit=False) as session:
            intent = await SqlDownloadAddIntentRepository(session).try_create(
                CreateDownloadAddIntent(
                    torrent_hash="racing-intent-hash",
                    media_request_id=self.request_id,
                    tmdb_id=1,
                    media_type="movie",
                    save_path="",
                    scopes=(
                        DownloadAddIntentScopeCreate(
                            tmdb_id=1, media_type="movie", scope_key="movie", is_target=True
                        ),
                    ),
                )
            )
            self.conflict_refused = intent is None
            await session.rollback()
        return await super().add_prepared(prepared, save_path, category)


async def test_committed_reservation_refuses_racing_intent_publication(
    engine: AsyncEngine,
) -> None:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        request = MediaRequest(
            tmdb_id=1, media_type=MediaType.movie, title="Movie", status=RequestStatus.pending
        )
        session.add(request)
        await session.commit()
        scored = ScoredRelease(
            candidate=candidate("Movie.Second.Release", info_hash="secondhash"),
            parsed=ParsedRelease(
                raw_title="Movie.Second.Release",
                clean_title="Movie",
                source=QualitySource.WEBDL,
            ),
            quality=WEBDL1080P,
            profile_index=0,
            score=1,
        )
        qbt = PublishDuringAdd(engine, request.id)

        record = await grab_service.grab(
            qbt, session, scored=scored, request_id=request.id, tmdb_id=1
        )

        assert record.torrent_hash == "secondhash"
        assert qbt.conflict_refused
        assert len(qbt.added) == 1
        intents = SqlDownloadAddIntentRepository(session)
        assert await intents.get_by_hash("racing-intent-hash") is None
