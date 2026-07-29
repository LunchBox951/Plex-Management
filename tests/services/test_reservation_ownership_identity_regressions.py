"""Repros for ownership and hash-identity regressions in reservation activation."""

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
from plex_manager.ports.download_client import AddResult, DownloadStatus, PreparedAdd
from plex_manager.repositories.download_add_intents import SqlDownloadAddIntentRepository
from plex_manager.services import grab_service
from plex_manager.services.correction_service import cancel_request_with_outcome
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


def scored(info_hash: str, *, source: str | None = None) -> ScoredRelease:
    release = candidate("Movie.Second.Release", info_hash=info_hash)
    if source is not None:
        release = release.model_copy(update={"magnet_url": None, "download_url": source})
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


class ForeignDuplicate(FakeQbittorrent):
    def __init__(self, torrent_hash: str) -> None:
        super().__init__(
            statuses=[
                DownloadStatus(
                    info_hash=torrent_hash,
                    name="foreign-user-torrent",
                    raw_state="downloading",
                    category="foreign-category",
                )
            ],
            pre_existing={torrent_hash},
        )


async def test_duplicate_foreign_hash_is_parked_without_adoption_or_deletion(
    engine: AsyncEngine,
) -> None:
    torrent_hash = "a" * 40
    async with AsyncSession(engine, expire_on_commit=False) as session:
        request = await seed(session)
        qbt = ForeignDuplicate(torrent_hash)

        with pytest.raises(grab_service.AlreadyDownloadingError):
            await grab_service.grab(
                qbt, session, scored=scored(torrent_hash), request_id=request.id, tmdb_id=1
            )

        intent = await session.scalar(select(DownloadAddIntent))
        assert intent is not None and intent.state == "needs_attention"
        assert await session.scalar(select(Download)) is None
        assert qbt.categories == []

        await cancel_request_with_outcome(session, qbt, request_id=request.id)
        assert qbt.removed == []


class WorkerDied(BaseException):
    pass


class ChangingHttpSource(FakeQbittorrent):
    """The prepared payload stays hash A even if a source would later resolve to B."""

    def __init__(self) -> None:
        super().__init__()
        self.prepare_calls = 0
        self.live: dict[str, DownloadStatus] = {}

    async def prepare_add(self, magnet_or_url: str) -> PreparedAdd:
        self.prepare_calls += 1
        torrent_hash = "a" * 40 if self.prepare_calls == 1 else "b" * 40
        return PreparedAdd(torrent_hash=torrent_hash, submission_url=magnet_or_url)

    async def add_prepared(self, prepared: PreparedAdd, save_path: str, category: str) -> AddResult:
        self.live[prepared.torrent_hash] = DownloadStatus(
            info_hash=prepared.torrent_hash,
            name="prepared-http-source",
            raw_state="downloading",
            category=category,
        )
        raise WorkerDied()

    async def get_status(self, info_hash: str) -> DownloadStatus | None:
        return self.live.get(info_hash)


async def test_prepared_payload_crash_recovers_the_reserved_hash(engine: AsyncEngine) -> None:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        request = await seed(session)
        qbt = ChangingHttpSource()
        with pytest.raises(WorkerDied):
            await grab_service.grab(
                qbt,
                session,
                scored=scored("a" * 40, source="https://example.invalid/release.torrent"),
                request_id=request.id,
                tmdb_id=1,
            )

        intent = await session.scalar(select(DownloadAddIntent))
        assert intent is not None and intent.torrent_hash == "a" * 40
        assert "a" * 40 in qbt.live
        assert qbt.prepare_calls == 1

        result = await recover_all(qbt, session)
        assert result.finalized == 1
        assert await SqlDownloadAddIntentRepository(session).get(intent.id, fresh=True) is None
        download = await session.scalar(select(Download))
        assert download is not None and download.torrent_hash == "a" * 40
