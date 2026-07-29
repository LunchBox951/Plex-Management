"""Regressions for durable-intent cleanup, guards, and preflight ordering."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from plex_manager.adapters import encryption
from plex_manager.adapters.qbittorrent.adapter import QbittorrentSourceError
from plex_manager.config import get_settings
from plex_manager.db import Base, enable_sqlite_fk_enforcement
from plex_manager.domain.quality import WEBDL1080P, QualitySource
from plex_manager.domain.release import ParsedRelease, ScoredRelease
from plex_manager.models import (
    Download,
    DownloadAddIntent,
    MediaRequest,
    MediaType,
    RequestStatus,
    SeasonRequest,
)
from plex_manager.ports.download_client import AddResult, DownloadStatus, PreparedAdd
from plex_manager.ports.repositories import (
    CreateDownloadAddIntent,
    DownloadAddIntentRecord,
    DownloadAddIntentScopeCreate,
)
from plex_manager.repositories.download_add_intents import SqlDownloadAddIntentRepository
from plex_manager.repositories.downloads import SqlDownloadRepository
from plex_manager.repositories.requests import SqlRequestRepository
from plex_manager.repositories.season_requests import SqlSeasonRequestRepository
from plex_manager.services import correction_service, grab_service
from plex_manager.services.download_add_intent_service import intent_category, recover_all
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
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    value = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'round4.db'}")
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
            raw_title=release.title,
            clean_title="Movie",
            source=QualitySource.WEBDL,
        ),
        quality=WEBDL1080P,
        profile_index=0,
        score=1,
    )


async def make_intent(
    session: AsyncSession,
    request: MediaRequest,
    torrent_hash: str,
    *,
    observed: str = "pending",
    season: int | None = None,
) -> DownloadAddIntentRecord:
    media_type = "tv" if season is not None else "movie"
    scope_key = f"season:{season}" if season is not None else "movie"
    return await SqlDownloadAddIntentRepository(session).create(
        CreateDownloadAddIntent(
            torrent_hash=torrent_hash,
            source=f"magnet:?xt=urn:btih:{torrent_hash}",
            media_request_id=request.id,
            tmdb_id=request.tmdb_id,
            media_type=media_type,
            save_path="",
            observed_request_status=observed,
            observed_season_status=observed if season is not None else None,
            scopes=(
                DownloadAddIntentScopeCreate(
                    tmdb_id=request.tmdb_id,
                    media_type=media_type,
                    scope_key=scope_key,
                    season_number=season,
                    is_target=True,
                ),
            ),
        )
    )


async def test_owned_premise_conflict_transitions_to_cleanup(engine: AsyncEngine) -> None:
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        request = MediaRequest(
            tmdb_id=1,
            media_type=MediaType.movie,
            title="M",
            status=RequestStatus.searching,
        )
        session.add(request)
        await session.flush()
        intent = await make_intent(session, request, "1" * 40, observed="pending")
        await session.commit()
        qbt = FakeQbittorrent(
            statuses=[
                DownloadStatus(
                    info_hash="1" * 40,
                    name="owned",
                    raw_state="downloading",
                    category=intent_category(intent.id),
                )
            ],
            pre_existing={"1" * 40},
        )

        outcome = await recover_all(qbt, session)
        deferred = await session.get(DownloadAddIntent, intent.id)

        assert not outcome.changed
        assert deferred is not None and deferred.state == "cancel_requested"
        assert await session.scalar(select(Download)) is None

        qbt.statuses.clear()
        deferred = await recover_all(qbt, session)

        assert deferred.removed == 1
        assert await session.get(DownloadAddIntent, intent.id) is None


async def test_parked_scopes_do_not_block_movie_or_tv_guards(engine: AsyncEngine) -> None:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        movie = MediaRequest(
            tmdb_id=10,
            media_type=MediaType.movie,
            title="M",
            status=RequestStatus.available,
        )
        tv = MediaRequest(
            tmdb_id=20,
            media_type=MediaType.tv,
            title="T",
            status=RequestStatus.available,
        )
        session.add_all([movie, tv])
        await session.flush()
        season = SeasonRequest(
            media_request_id=tv.id,
            season_number=1,
            status=RequestStatus.available,
        )
        session.add(season)
        await session.flush()
        movie_intent = await make_intent(session, movie, "2" * 40)
        tv_intent = await make_intent(session, tv, "3" * 40, season=1)
        intents = SqlDownloadAddIntentRepository(session)
        assert await intents.mark_state(
            movie_intent.id, "needs_attention", expected_state="prepared"
        )
        assert await intents.mark_state(tv_intent.id, "needs_attention", expected_state="prepared")
        await session.commit()

        assert await SqlRequestRepository(session).set_status_if_in(
            movie.id,
            "evicted",
            frozenset({"available"}),
            require_no_active_download_or_intent=True,
        )
        assert await SqlSeasonRequestRepository(session).set_status_if_in(
            season.id,
            "evicted",
            frozenset({"available"}),
            require_no_active_download_or_intent=True,
        )


async def test_cancel_with_intent_without_client_refuses_before_settle(engine: AsyncEngine) -> None:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        request = MediaRequest(
            tmdb_id=30,
            media_type=MediaType.movie,
            title="M",
            status=RequestStatus.pending,
        )
        session.add(request)
        await session.flush()
        intent = await make_intent(session, request, "4" * 40)
        await session.commit()

        with pytest.raises(correction_service.DownloadClientRequiredError):
            await correction_service.cancel_request_with_outcome(
                session, None, request_id=request.id
            )

        stored = await session.get(DownloadAddIntent, intent.id)
        assert stored is not None and stored.state == "prepared"
        updated = await session.get(MediaRequest, request.id)
        assert updated is not None and updated.status == RequestStatus.pending


class FailingPrepare(FakeQbittorrent):
    async def prepare_add(self, magnet_or_url: str) -> PreparedAdd:
        raise QbittorrentSourceError("expired source")


async def test_known_active_hash_is_idempotent_before_prepare(engine: AsyncEngine) -> None:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        request = MediaRequest(
            tmdb_id=40,
            media_type=MediaType.movie,
            title="M",
            status=RequestStatus.downloading,
        )
        session.add(request)
        await session.flush()
        torrent_hash = "6" * 40
        row = Download(
            torrent_hash=torrent_hash,
            status="downloading",
            media_request_id=request.id,
            tmdb_id=40,
            media_type=MediaType.movie,
        )
        session.add(row)
        await session.commit()

        record = await grab_service.grab(
            FailingPrepare(),
            session,
            scored=scored(torrent_hash),
            request_id=request.id,
            tmdb_id=40,
        )

        assert record.id == row.id


class WinnerAndFailedRemove(FakeQbittorrent):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], request_id: int) -> None:
        super().__init__()
        self._session_factory = session_factory
        self._request_id = request_id

    async def add_prepared(self, prepared: PreparedAdd, save_path: str, category: str) -> AddResult:
        async with self._session_factory() as session:
            session.add(
                Download(
                    torrent_hash="winner-hash",
                    status="downloading",
                    media_request_id=self._request_id,
                    tmdb_id=50,
                    media_type=MediaType.movie,
                )
            )
            await session.commit()
        return AddResult(torrent_hash=prepared.torrent_hash, created=True)

    async def remove(self, info_hash: str, *, delete_files: bool) -> None:
        raise RuntimeError("remove outage")


async def test_failed_orphan_cleanup_retains_intent_during_status_outage(
    engine: AsyncEngine,
) -> None:
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    torrent_hash = "5" * 40
    async with session_factory() as session:
        request = MediaRequest(
            tmdb_id=50,
            media_type=MediaType.movie,
            title="M",
            status=RequestStatus.searching,
        )
        session.add(request)
        await session.commit()
        request_id = request.id

    qbt = WinnerAndFailedRemove(session_factory, request_id)
    async with session_factory() as session:
        with pytest.raises(grab_service.AlreadyDownloadingError):
            await grab_service.grab(
                qbt,
                session,
                scored=scored(torrent_hash),
                request_id=request_id,
                tmdb_id=50,
            )

    async with session_factory() as session:
        intent = await session.scalar(
            select(DownloadAddIntent).where(DownloadAddIntent.torrent_hash == torrent_hash)
        )
        assert intent is not None and intent.state == "cancel_requested"

        deferred = await recover_all(qbt, session)

        assert deferred.removed == 1
        assert await session.get(DownloadAddIntent, intent.id) is None


class SameHashWinner(FakeQbittorrent):
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        owner_request_id: int,
        torrent_hash: str,
    ) -> None:
        super().__init__()
        self._session_factory = session_factory
        self._owner_request_id = owner_request_id
        self._torrent_hash = torrent_hash

    async def add_prepared(self, prepared: PreparedAdd, save_path: str, category: str) -> AddResult:
        async with self._session_factory() as session:
            session.add(
                Download(
                    torrent_hash=self._torrent_hash,
                    status="downloading",
                    media_request_id=self._owner_request_id,
                    tmdb_id=60,
                    media_type=MediaType.movie,
                )
            )
            await session.commit()
        return AddResult(torrent_hash=self._torrent_hash, created=True)


async def test_same_hash_foreign_winner_retires_loser_reservation(engine: AsyncEngine) -> None:
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    torrent_hash = "6" * 40
    async with session_factory() as session:
        owner = MediaRequest(
            tmdb_id=60,
            media_type=MediaType.movie,
            title="Owner",
            status=RequestStatus.downloading,
        )
        contender = MediaRequest(
            tmdb_id=61,
            media_type=MediaType.movie,
            title="Contender",
            status=RequestStatus.searching,
        )
        session.add_all((owner, contender))
        await session.commit()
        owner_id, contender_id = owner.id, contender.id

    async with session_factory() as session:
        with pytest.raises(grab_service.TorrentAlreadyTrackedError):
            await grab_service.grab(
                SameHashWinner(session_factory, owner_id, torrent_hash),
                session,
                scored=scored(torrent_hash),
                request_id=contender_id,
                tmdb_id=61,
            )

    async with session_factory() as session:
        assert await session.scalar(select(DownloadAddIntent)) is None


async def test_same_hash_same_request_winner_retires_loser_reservation(
    engine: AsyncEngine,
) -> None:
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    torrent_hash = "7" * 40
    async with session_factory() as session:
        request = MediaRequest(
            tmdb_id=60,
            media_type=MediaType.movie,
            title="M",
            status=RequestStatus.searching,
        )
        session.add(request)
        await session.commit()
        request_id = request.id

    async with session_factory() as session:
        record = await grab_service.grab(
            SameHashWinner(session_factory, request_id, torrent_hash),
            session,
            scored=scored(torrent_hash),
            request_id=request_id,
            tmdb_id=60,
        )

        assert record.torrent_hash == torrent_hash
        assert await session.scalar(select(DownloadAddIntent)) is None


async def _exercise_late_same_hash_winner(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    *,
    foreign: bool,
) -> None:
    torrent_hash = "8" * 40
    async with AsyncSession(engine, expire_on_commit=False) as session:
        owner = MediaRequest(
            tmdb_id=70,
            media_type=MediaType.movie,
            title="Owner",
            status=RequestStatus.downloading,
        )
        contender = MediaRequest(
            tmdb_id=71,
            media_type=MediaType.movie,
            title="Contender",
            status=RequestStatus.searching,
        )
        session.add_all((owner, contender))
        await session.flush()
        request = contender if foreign else owner
        session.add(
            Download(
                torrent_hash=torrent_hash,
                status="downloading",
                media_request_id=owner.id,
                tmdb_id=owner.tmdb_id,
                media_type=MediaType.movie,
            )
        )
        await session.commit()

        original_get_by_hash = SqlDownloadRepository.get_by_hash
        calls = 0

        async def hide_winner_until_create_collision(
            repository: SqlDownloadRepository,
            value: str,
            *,
            populate_existing: bool = False,
        ) -> object:
            nonlocal calls
            calls += 1
            if calls <= 2:
                return None
            return await original_get_by_hash(
                repository, value, populate_existing=populate_existing
            )

        monkeypatch.setattr(
            SqlDownloadRepository, "get_by_hash", hide_winner_until_create_collision
        )
        qbt = FakeQbittorrent()
        if foreign:
            with pytest.raises(grab_service.TorrentAlreadyTrackedError):
                await grab_service.grab(
                    qbt,
                    session,
                    scored=scored(torrent_hash),
                    request_id=request.id,
                    tmdb_id=request.tmdb_id,
                )
        else:
            record = await grab_service.grab(
                qbt,
                session,
                scored=scored(torrent_hash),
                request_id=request.id,
                tmdb_id=request.tmdb_id,
            )
            assert record.torrent_hash == torrent_hash

        assert await session.scalar(select(DownloadAddIntent)) is None


async def test_late_foreign_same_hash_collision_retires_reservation(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _exercise_late_same_hash_winner(engine, monkeypatch, foreign=True)


async def test_late_same_request_same_hash_collision_retires_reservation(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _exercise_late_same_hash_winner(engine, monkeypatch, foreign=False)


async def test_late_collision_winner_vanishes_retires_reservation(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    torrent_hash = "9" * 40
    async with AsyncSession(engine, expire_on_commit=False) as session:
        owner = MediaRequest(
            tmdb_id=80,
            media_type=MediaType.movie,
            title="Owner",
            status=RequestStatus.downloading,
        )
        contender = MediaRequest(
            tmdb_id=81,
            media_type=MediaType.movie,
            title="Contender",
            status=RequestStatus.searching,
        )
        session.add_all((owner, contender))
        await session.flush()
        session.add(
            Download(
                torrent_hash=torrent_hash,
                status="downloading",
                media_request_id=owner.id,
                tmdb_id=owner.tmdb_id,
                media_type=MediaType.movie,
            )
        )
        await session.commit()

        calls = 0

        async def hide_then_delete_winner(
            repository: SqlDownloadRepository,
            value: str,
            *,
            populate_existing: bool = False,
        ) -> object:
            nonlocal calls
            calls += 1
            if calls <= 2:
                return None
            await session.execute(delete(Download).where(Download.torrent_hash == torrent_hash))
            await session.commit()
            return None

        monkeypatch.setattr(SqlDownloadRepository, "get_by_hash", hide_then_delete_winner)
        with pytest.raises(IntegrityError):
            await grab_service.grab(
                FakeQbittorrent(),
                session,
                scored=scored(torrent_hash),
                request_id=contender.id,
                tmdb_id=contender.tmdb_id,
            )

        intent = await session.scalar(select(DownloadAddIntent))
        assert intent is not None and intent.state == "cancel_requested"
