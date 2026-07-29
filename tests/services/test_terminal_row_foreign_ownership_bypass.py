"""Repro: terminal DB row weakens duplicate ownership proof."""

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
from plex_manager.models import Download, DownloadAddIntent, MediaRequest, MediaType, RequestStatus
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


async def test_terminal_row_is_not_ownership_proof_for_foreign_duplicate(
    engine: AsyncEngine,
) -> None:
    torrent_hash = "f" * 40
    async with AsyncSession(engine, expire_on_commit=False) as session:
        request = MediaRequest(
            tmdb_id=1, media_type=MediaType.movie, title="Movie", status=RequestStatus.pending
        )
        session.add(request)
        await session.flush()
        session.add(
            Download(
                torrent_hash=torrent_hash,
                status="failed",
                media_request_id=request.id,
                tmdb_id=1,
                media_type=MediaType.movie,
            )
        )
        await session.commit()
        # Duplicate add is reported, but the immediate status lookup has not yet
        # converged. The old terminal DB row is not one of the three ownership proofs.
        qbt = FakeQbittorrent(statuses=[], pre_existing={torrent_hash})

        with pytest.raises(grab_service.AlreadyDownloadingError):
            await grab_service.grab(
                qbt, session, scored=scored(torrent_hash), request_id=request.id, tmdb_id=1
            )

        intent = await session.scalar(select(DownloadAddIntent))
        assert intent is not None and intent.state == "prepared"
        assert qbt.categories == []
        assert qbt.removed == []
