"""Adversarial reservation lifecycle probes for commit 6a5814a3."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from plex_manager.adapters import encryption
from plex_manager.adapters.qbittorrent.adapter import QbittorrentAddRejectedError
from plex_manager.config import get_settings
from plex_manager.db import Base, enable_sqlite_fk_enforcement
from plex_manager.domain.quality import WEBDL1080P, QualitySource
from plex_manager.domain.release import ParsedRelease, ScoredRelease
from plex_manager.models import Download, DownloadAddIntent, MediaRequest, MediaType, RequestStatus
from plex_manager.ports.repositories import CreateDownloadAddIntent, DownloadAddIntentScopeCreate
from plex_manager.repositories.download_add_intents import SqlDownloadAddIntentRepository
from plex_manager.services import grab_service
from plex_manager.services.download_add_intent_service import recover_all
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
    release = candidate("Movie.Second.Release", info_hash=info_hash)
    return ScoredRelease(
        candidate=release,
        parsed=ParsedRelease(
            raw_title=release.title, clean_title="Movie", source=QualitySource.WEBDL
        ),
        quality=WEBDL1080P,
        profile_index=0,
        score=1,
    )


async def seed(session: AsyncSession) -> MediaRequest:
    request = MediaRequest(
        tmdb_id=1, media_type=MediaType.movie, title="Movie", status=RequestStatus.pending
    )
    session.add(request)
    await session.commit()
    return request


class FailingAdd(FakeQbittorrent):
    async def add_prepared(self, prepared, save_path: str, category: str):  # type: ignore[no-untyped-def]
        raise QbittorrentAddRejectedError("simulated add rejection")


async def test_definite_add_rejection_releases_reservation_for_immediate_retry(
    engine: AsyncEngine,
) -> None:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        request = await seed(session)
        with pytest.raises(QbittorrentAddRejectedError, match="simulated add rejection"):
            await grab_service.grab(
                FailingAdd(), session, scored=scored("a" * 40), request_id=request.id, tmdb_id=1
            )

        reservation = await session.scalar(select(DownloadAddIntent))
        assert reservation is None

        retry = FakeQbittorrent()
        record = await grab_service.grab(
            retry, session, scored=scored("b" * 40), request_id=request.id, tmdb_id=1
        )
        assert record.torrent_hash == "b" * 40
        assert len(retry.added) == 1


async def test_needs_attention_releases_scope_for_a_fresh_grab(
    engine: AsyncEngine,
) -> None:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        request = await seed(session)
        repo = SqlDownloadAddIntentRepository(session)
        parked = await repo.create(
            CreateDownloadAddIntent(
                torrent_hash="parked-hash",
                media_request_id=request.id,
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
        assert await repo.mark_state(
            parked.id,
            "needs_attention",
            last_error="client_hash_ownership_unproven",
            expected_state="prepared",
        )
        await session.commit()
        assert not await repo.has_active_scope(tmdb_id=1, media_type="movie", scope_keys=("movie",))

        qbt = FakeQbittorrent()
        record = await grab_service.grab(
            qbt, session, scored=scored("c" * 40), request_id=request.id, tmdb_id=1
        )
        assert record.torrent_hash == "c" * 40
        assert len(qbt.added) == 1


async def test_crash_before_add_is_recovered_and_scope_freed(engine: AsyncEngine) -> None:
    """Control proving a committed prepared reservation is recoverable."""
    async with AsyncSession(engine, expire_on_commit=False) as session:
        request = await seed(session)
        repo = SqlDownloadAddIntentRepository(session)
        await repo.create(
            CreateDownloadAddIntent(
                torrent_hash="d" * 40,
                source=f"magnet:?xt=urn:btih:{'d' * 40}",
                media_request_id=request.id,
                tmdb_id=1,
                media_type="movie",
                save_path="",
                observed_request_status=RequestStatus.pending.value,
                scopes=(
                    DownloadAddIntentScopeCreate(
                        tmdb_id=1, media_type="movie", scope_key="movie", is_target=True
                    ),
                ),
            )
        )
        await session.commit()

        result = await recover_all(FakeQbittorrent(), session)
        assert result.finalized == 1
        assert await session.scalar(select(DownloadAddIntent)) is None
        assert (await session.scalar(select(Download))) is not None
