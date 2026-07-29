from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from plex_manager.adapters import encryption
from plex_manager.config import get_settings
from plex_manager.db import Base, enable_sqlite_fk_enforcement
from plex_manager.domain.quality import WEBDL1080P, QualitySource
from plex_manager.domain.release import ParsedRelease, ScoredRelease
from plex_manager.models import Download
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


def scored(info_hash: str) -> ScoredRelease:
    release = candidate("Movie.Release", info_hash=info_hash)
    return ScoredRelease(
        candidate=release,
        parsed=ParsedRelease(
            raw_title=release.title, clean_title="Movie", source=QualitySource.WEBDL
        ),
        quality=WEBDL1080P,
        profile_index=0,
        score=1,
    )


async def test_unreserved_created_false_refuses_adoption_without_proof(engine: AsyncEngine) -> None:
    torrent_hash = "9" * 40
    qbt = FakeQbittorrent(pre_existing={torrent_hash})
    async with AsyncSession(engine, expire_on_commit=False) as session:
        with pytest.raises(grab_service.ClientHashOwnershipUnprovenError):
            await grab_service.grab(qbt, session, scored=scored(torrent_hash), tmdb_id=1)
        assert (
            await session.scalar(select(Download).where(Download.torrent_hash == torrent_hash))
            is None
        )
