"""Durable add-intent recovery lifecycle."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.adapters.qbittorrent.adapter import QbittorrentSourceError
from plex_manager.domain.quality import WEBDL1080P, QualitySource
from plex_manager.domain.release import ParsedRelease, ScoredRelease
from plex_manager.models import (
    Download,
    DownloadCoverageClaim,
    DownloadHistory,
    DownloadScope,
    MediaRequest,
    MediaType,
    RequestStatus,
    SeasonRequest,
)
from plex_manager.ports.download_client import AddResult, DownloadStatus, PreparedAdd
from plex_manager.ports.repositories import CreateDownloadAddIntent, DownloadAddIntentScopeCreate
from plex_manager.repositories.download_add_intents import SqlDownloadAddIntentRepository
from plex_manager.services.download_add_intent_service import (
    intent_category,
    publish_intent,
    recover_all,
    submit_and_finalize,
)
from tests.web.fakes import candidate


@pytest.fixture
async def session(sessionmaker_: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with sessionmaker_() as value:
        yield value


class _Client:
    def __init__(self, statuses: dict[str, DownloadStatus]) -> None:
        self.statuses = statuses
        self.adds: list[str] = []
        self.removes: list[str] = []
        self.categories: list[tuple[str, str]] = []

    async def prepare_add(self, magnet_or_url: str) -> PreparedAdd:
        return PreparedAdd(torrent_hash="hash", submission_url="https://example.invalid/torrent")

    async def add_prepared(self, prepared: PreparedAdd, save_path: str, category: str) -> AddResult:
        self.adds.append(category)
        self.statuses[prepared.torrent_hash] = DownloadStatus(
            info_hash=prepared.torrent_hash,
            name="torrent",
            raw_state="downloading",
            category=category,
        )
        return AddResult(torrent_hash=prepared.torrent_hash, created=True)

    async def get_status(self, info_hash: str) -> DownloadStatus | None:
        return self.statuses.get(info_hash)

    async def set_category(self, info_hash: str, category: str) -> None:
        self.categories.append((info_hash, category))

    async def remove(self, info_hash: str, *, delete_files: bool) -> None:
        self.removes.append(info_hash)
        self.statuses.pop(info_hash, None)


class _SourceErrorPrepareClient(_Client):
    async def prepare_add(self, magnet_or_url: str) -> PreparedAdd:
        raise QbittorrentSourceError("bad source")


class _FailingRemoveClient(_Client):
    async def remove(self, info_hash: str, *, delete_files: bool) -> None:
        raise ConnectionError("client unavailable")


class _RemoveThenDatabaseFailureClient(_Client):
    async def remove(self, info_hash: str, *, delete_files: bool) -> None:
        await super().remove(info_hash, delete_files=delete_files)


class _FailingPrepareClient(_Client):
    async def prepare_add(self, magnet_or_url: str) -> PreparedAdd:
        raise ConnectionError("client unavailable")


async def test_present_intent_finalizes_once_and_normalizes_category(session: AsyncSession) -> None:
    request = MediaRequest(
        tmdb_id=1, media_type=MediaType.movie, title="Movie", status=RequestStatus.pending
    )
    session.add(request)
    await session.flush()
    intent = await SqlDownloadAddIntentRepository(session).create(
        CreateDownloadAddIntent(
            torrent_hash="hash",
            source="magnet:source",
            media_request_id=request.id,
            tmdb_id=1,
            media_type="movie",
            release_title="Release",
            save_path="",
            observed_request_status="pending",
            scopes=(
                DownloadAddIntentScopeCreate(
                    tmdb_id=1, media_type="movie", scope_key="movie", is_target=True
                ),
            ),
        )
    )
    client = _Client(
        {
            "hash": DownloadStatus(
                info_hash="hash",
                name="torrent",
                raw_state="downloading",
                category=intent_category(intent.id),
            )
        }
    )

    record = await submit_and_finalize(client, session, intent=intent)

    assert record.torrent_hash == "hash"
    assert await SqlDownloadAddIntentRepository(session).get(intent.id) is None
    assert len((await session.scalars(select(DownloadHistory))).all()) == 1
    assert client.categories == [("hash", "plex-manager")]
    request_after = await session.get(MediaRequest, request.id)
    assert request_after is not None
    assert request_after.status == RequestStatus.downloading


async def test_recovery_does_not_delete_intent_for_terminal_same_hash_download(
    session: AsyncSession,
) -> None:
    request = MediaRequest(
        tmdb_id=1, media_type=MediaType.movie, title="Movie", status=RequestStatus.pending
    )
    session.add(request)
    await session.flush()
    request_id = request.id
    intent = await SqlDownloadAddIntentRepository(session).create(
        CreateDownloadAddIntent(
            torrent_hash="hash",
            media_request_id=request_id,
            tmdb_id=1,
            media_type="movie",
            save_path="",
            observed_request_status=RequestStatus.pending.value,
            scopes=(
                DownloadAddIntentScopeCreate(tmdb_id=1, media_type="movie", scope_key="movie"),
            ),
        )
    )
    terminal = Download(
        torrent_hash="hash",
        status="failed",
        media_request_id=request_id,
        tmdb_id=1,
        media_type=MediaType.movie,
    )
    session.add(terminal)
    await session.flush()
    terminal_id = terminal.id
    await session.commit()

    result = await recover_all(
        _Client(
            {
                "hash": DownloadStatus(
                    info_hash="hash",
                    name="torrent",
                    raw_state="downloading",
                    category=intent_category(intent.id),
                )
            }
        ),
        session,
    )

    assert result.needs_attention == 1
    terminal_after = await session.get(Download, terminal_id)
    assert terminal_after is not None
    assert terminal_after.status == "failed"
    request_after = await session.get(MediaRequest, request_id)
    assert request_after is not None
    assert request_after.status == RequestStatus.pending
    remaining = await SqlDownloadAddIntentRepository(session).get(intent.id)
    assert remaining is not None
    assert remaining.state == "needs_attention"


async def test_recovery_never_reowns_same_hash_foreign_download(session: AsyncSession) -> None:
    foreign_request = MediaRequest(
        tmdb_id=2, media_type=MediaType.tv, title="Foreign", status=RequestStatus.pending
    )
    target_request = MediaRequest(
        tmdb_id=1, media_type=MediaType.tv, title="Target", status=RequestStatus.pending
    )
    session.add_all((foreign_request, target_request))
    await session.flush()
    target_request_id = target_request.id
    intent = await SqlDownloadAddIntentRepository(session).create(
        CreateDownloadAddIntent(
            torrent_hash="hash",
            media_request_id=target_request_id,
            tmdb_id=1,
            media_type="tv",
            save_path="",
            observed_request_status=RequestStatus.pending.value,
            scopes=(
                DownloadAddIntentScopeCreate(
                    tmdb_id=1,
                    media_type="tv",
                    scope_key="season:1",
                    season_number=1,
                    is_target=True,
                ),
            ),
        )
    )
    foreign_download = Download(
        torrent_hash="hash",
        status="downloading",
        media_request_id=foreign_request.id,
        tmdb_id=1,
        media_type=MediaType.tv,
    )
    session.add(foreign_download)
    await session.flush()
    foreign_download_id = foreign_download.id
    foreign_request_id = foreign_request.id
    session.add(
        DownloadScope(
            download_id=foreign_download.id,
            media_request_id=foreign_request.id,
            season_number=1,
            scope_key="season:1",
        )
    )
    await session.commit()

    result = await recover_all(
        _Client(
            {
                "hash": DownloadStatus(
                    info_hash="hash",
                    name="torrent",
                    raw_state="downloading",
                    category=intent_category(intent.id),
                )
            }
        ),
        session,
    )

    assert result.needs_attention == 1
    scopes = (await session.scalars(select(DownloadScope))).all()
    assert [(scope.download_id, scope.media_request_id) for scope in scopes] == [
        (foreign_download_id, foreign_request_id)
    ]
    target_after = await session.get(MediaRequest, target_request_id)
    assert target_after is not None
    assert target_after.status == RequestStatus.pending
    remaining = await SqlDownloadAddIntentRepository(session).get(intent.id)
    assert remaining is not None
    assert remaining.state == "needs_attention"


async def test_request_deleted_intent_remains_attention_required(session: AsyncSession) -> None:
    intent = await SqlDownloadAddIntentRepository(session).create(
        CreateDownloadAddIntent(
            torrent_hash="orphan",
            media_request_id=None,
            tmdb_id=1,
            media_type="movie",
            save_path="",
            scopes=(
                DownloadAddIntentScopeCreate(tmdb_id=1, media_type="movie", scope_key="movie"),
            ),
        )
    )

    result = await recover_all(
        _Client(
            {
                "orphan": DownloadStatus(
                    info_hash="orphan",
                    name="torrent",
                    raw_state="downloading",
                    category=intent_category(intent.id),
                )
            }
        ),
        session,
    )

    assert result.needs_attention == 1
    remaining = await SqlDownloadAddIntentRepository(session).get(intent.id)
    assert remaining is not None
    assert remaining.state == "needs_attention"


async def test_absent_prepared_intent_is_readded_only_after_matching_prepare(
    session: AsyncSession,
) -> None:
    request = MediaRequest(
        tmdb_id=1, media_type=MediaType.movie, title="Movie", status=RequestStatus.pending
    )
    session.add(request)
    await session.flush()
    intent = await SqlDownloadAddIntentRepository(session).create(
        CreateDownloadAddIntent(
            torrent_hash="hash",
            source="magnet:source",
            media_request_id=request.id,
            tmdb_id=1,
            media_type="movie",
            save_path="",
            scopes=(
                DownloadAddIntentScopeCreate(tmdb_id=1, media_type="movie", scope_key="movie"),
            ),
        )
    )
    client = _Client({})

    result = await recover_all(client, session)

    assert result.finalized == 1
    assert client.adds == [intent_category(intent.id)]
    assert await SqlDownloadAddIntentRepository(session).get(intent.id) is None


async def test_hash_mismatch_needs_attention_without_client_mutation(session: AsyncSession) -> None:
    intent = await SqlDownloadAddIntentRepository(session).create(
        CreateDownloadAddIntent(
            torrent_hash="other",
            source="magnet:source",
            tmdb_id=1,
            media_type="movie",
            save_path="",
            scopes=(
                DownloadAddIntentScopeCreate(tmdb_id=1, media_type="movie", scope_key="movie"),
            ),
        )
    )
    client = _Client({})

    result = await recover_all(client, session)

    assert result.needs_attention == 1
    assert client.adds == []
    stored = await SqlDownloadAddIntentRepository(session).get(intent.id)
    assert stored is not None
    assert stored.state == "needs_attention"


async def test_transient_cancel_remove_failure_preserves_retryable_state(
    session: AsyncSession,
) -> None:
    intent = await SqlDownloadAddIntentRepository(session).create(
        CreateDownloadAddIntent(
            torrent_hash="hash",
            tmdb_id=1,
            media_type="movie",
            save_path="",
            scopes=(
                DownloadAddIntentScopeCreate(tmdb_id=1, media_type="movie", scope_key="movie"),
            ),
        )
    )
    await SqlDownloadAddIntentRepository(session).mark_state(intent.id, "cancel_requested")
    await session.commit()
    client = _FailingRemoveClient(
        {
            "hash": DownloadStatus(
                info_hash="hash",
                name="torrent",
                raw_state="downloading",
                category=intent_category(intent.id),
            )
        }
    )

    with pytest.raises(ConnectionError, match="client unavailable"):
        await recover_all(client, session)

    remaining = await SqlDownloadAddIntentRepository(session).get(intent.id)
    assert remaining is not None
    assert remaining.state == "cancel_requested"


async def test_cancelled_intent_database_failure_after_remove_stays_non_readdable(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    intent = await SqlDownloadAddIntentRepository(session).create(
        CreateDownloadAddIntent(
            torrent_hash="hash",
            source="magnet:source",
            tmdb_id=1,
            media_type="movie",
            save_path="",
            scopes=(
                DownloadAddIntentScopeCreate(tmdb_id=1, media_type="movie", scope_key="movie"),
            ),
        )
    )
    await SqlDownloadAddIntentRepository(session).mark_state(intent.id, "cancel_requested")
    await session.commit()
    client = _RemoveThenDatabaseFailureClient(
        {
            "hash": DownloadStatus(
                info_hash="hash",
                name="torrent",
                raw_state="downloading",
                category=intent_category(intent.id),
            )
        }
    )
    original_delete = SqlDownloadAddIntentRepository.delete

    async def fail_once(self: SqlDownloadAddIntentRepository, intent_id: int) -> bool:
        monkeypatch.setattr(SqlDownloadAddIntentRepository, "delete", original_delete)
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(SqlDownloadAddIntentRepository, "delete", fail_once)

    with pytest.raises(RuntimeError, match="database unavailable"):
        await recover_all(client, session)

    remaining = await SqlDownloadAddIntentRepository(session).get(intent.id)
    assert remaining is not None
    assert remaining.state == "cancel_requested"
    assert client.statuses == {}

    result = await recover_all(client, session)
    assert result.removed == 1
    assert client.adds == []
    assert await SqlDownloadAddIntentRepository(session).get(intent.id) is None


async def test_client_wide_prepare_failure_is_reraised(session: AsyncSession) -> None:
    request = MediaRequest(
        tmdb_id=1, media_type=MediaType.movie, title="Movie", status=RequestStatus.pending
    )
    session.add(request)
    await session.flush()
    intent = await SqlDownloadAddIntentRepository(session).create(
        CreateDownloadAddIntent(
            torrent_hash="hash",
            source="magnet:source",
            media_request_id=request.id,
            tmdb_id=1,
            media_type="movie",
            save_path="",
            scopes=(
                DownloadAddIntentScopeCreate(tmdb_id=1, media_type="movie", scope_key="movie"),
            ),
        )
    )
    await session.commit()

    with pytest.raises(ConnectionError, match="client unavailable"):
        await recover_all(_FailingPrepareClient({}), session)

    remaining = await SqlDownloadAddIntentRepository(session).get(intent.id)
    assert remaining is not None
    assert remaining.state == "prepared"


async def test_same_hash_convergence_locks_active_download_before_attaching_scopes(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = MediaRequest(
        tmdb_id=1, media_type=MediaType.movie, title="Movie", status=RequestStatus.pending
    )
    session.add(request)
    await session.flush()
    intent = await SqlDownloadAddIntentRepository(session).create(
        CreateDownloadAddIntent(
            torrent_hash="hash",
            media_request_id=request.id,
            tmdb_id=1,
            media_type="movie",
            save_path="",
            observed_request_status=RequestStatus.pending.value,
            scopes=(
                DownloadAddIntentScopeCreate(tmdb_id=1, media_type="movie", scope_key="movie"),
            ),
        )
    )
    session.add(
        Download(
            torrent_hash="hash",
            status="downloading",
            media_request_id=request.id,
            tmdb_id=1,
            media_type=MediaType.movie,
        )
    )
    await session.commit()
    from plex_manager.repositories.downloads import SqlDownloadRepository

    original_lock = SqlDownloadRepository.lock_if_active
    locked: list[int] = []

    async def record_lock(self: SqlDownloadRepository, download_id: int) -> bool:
        locked.append(download_id)
        return await original_lock(self, download_id)

    monkeypatch.setattr(SqlDownloadRepository, "lock_if_active", record_lock)
    await recover_all(
        _Client(
            {
                "hash": DownloadStatus(
                    info_hash="hash",
                    name="torrent",
                    raw_state="downloading",
                    category=intent_category(intent.id),
                )
            }
        ),
        session,
    )

    assert locked
    assert await SqlDownloadAddIntentRepository(session).get(intent.id) is None


async def test_tv_pack_recovery_keeps_initiating_season_as_primary_target(
    session: AsyncSession,
) -> None:
    request = MediaRequest(
        tmdb_id=1, media_type=MediaType.tv, title="Show", status=RequestStatus.pending
    )
    session.add(request)
    await session.flush()
    season_one = SeasonRequest(
        media_request_id=request.id, season_number=1, status=RequestStatus.pending
    )
    season_two = SeasonRequest(
        media_request_id=request.id, season_number=2, status=RequestStatus.failed
    )
    session.add_all((season_one, season_two))
    await session.flush()
    scored = ScoredRelease(
        candidate=candidate("Show.S01-S02", info_hash="hash"),
        parsed=ParsedRelease(
            raw_title="Show.S01-S02", clean_title="Show", source=QualitySource.WEBDL
        ),
        quality=WEBDL1080P,
        profile_index=1,
        score=1,
        target_seasons=(1, 2),
        covered_seasons=(1, 2),
    )
    intent = await publish_intent(
        session,
        scored=scored,
        prepared=PreparedAdd(torrent_hash="hash", submission_url="magnet:hash"),
        request_id=request.id,
        tmdb_id=1,
        year=None,
        season=2,
        episodes=None,
        save_path="",
        observed_request_status=None,
        observed_season_status=RequestStatus.failed.value,
        scope_episodes_by_season=None,
    )
    await session.commit()

    await recover_all(
        _Client(
            {
                "hash": DownloadStatus(
                    info_hash="hash",
                    name="pack",
                    raw_state="downloading",
                    category=intent_category(intent.id),
                )
            }
        ),
        session,
    )

    seasons = (
        await session.scalars(select(SeasonRequest).order_by(SeasonRequest.season_number))
    ).all()
    assert [(row.season_number, row.status.value) for row in seasons] == [
        (1, RequestStatus.downloading.value),
        (2, RequestStatus.downloading.value),
    ]


async def test_tv_pack_finalization_moves_every_target_and_claims_full_footprint(
    session: AsyncSession,
) -> None:
    request = MediaRequest(
        tmdb_id=1, media_type=MediaType.tv, title="Show", status=RequestStatus.pending
    )
    session.add(request)
    await session.flush()
    intent = await SqlDownloadAddIntentRepository(session).create(
        CreateDownloadAddIntent(
            torrent_hash="hash",
            media_request_id=request.id,
            tmdb_id=1,
            media_type="tv",
            save_path="",
            observed_season_status=RequestStatus.pending.value,
            scopes=(
                DownloadAddIntentScopeCreate(
                    tmdb_id=1,
                    media_type="tv",
                    scope_key="season:1",
                    season_number=1,
                    episodes=(4,),
                    is_target=True,
                ),
                DownloadAddIntentScopeCreate(
                    tmdb_id=1,
                    media_type="tv",
                    scope_key="season:2",
                    season_number=2,
                    episodes=(5,),
                    is_target=True,
                ),
                DownloadAddIntentScopeCreate(
                    tmdb_id=1,
                    media_type="tv",
                    scope_key="season:3",
                    season_number=3,
                ),
            ),
        )
    )
    await session.commit()
    client = _Client(
        {
            "hash": DownloadStatus(
                info_hash="hash",
                name="pack",
                raw_state="downloading",
                category=intent_category(intent.id),
            )
        }
    )

    await recover_all(client, session)

    from plex_manager.models import SeasonRequest

    seasons = (
        await session.scalars(select(SeasonRequest).order_by(SeasonRequest.season_number))
    ).all()
    claims = (
        await session.scalars(
            select(DownloadCoverageClaim).order_by(DownloadCoverageClaim.season_number)
        )
    ).all()
    assert [(row.season_number, row.status.value) for row in seasons] == [
        (1, RequestStatus.downloading.value),
        (2, RequestStatus.downloading.value),
    ]
    assert [claim.season_number for claim in claims] == [1, 2, 3]


async def test_publish_intent_uses_scored_pack_targets_and_coverage(session: AsyncSession) -> None:
    request = MediaRequest(
        tmdb_id=1, media_type=MediaType.tv, title="Show", status=RequestStatus.pending
    )
    session.add(request)
    await session.flush()
    scored = ScoredRelease(
        candidate=candidate("Show.S01-S03", info_hash="hash"),
        parsed=ParsedRelease(
            raw_title="Show.S01-S03", clean_title="Show", source=QualitySource.WEBDL
        ),
        quality=WEBDL1080P,
        profile_index=1,
        score=1,
        target_seasons=(1, 2),
        covered_seasons=(1, 2, 3),
    )

    intent = await publish_intent(
        session,
        scored=scored,
        prepared=PreparedAdd(torrent_hash="hash", submission_url="magnet:hash"),
        request_id=request.id,
        tmdb_id=1,
        year=None,
        season=1,
        episodes=[4],
        save_path="",
        observed_request_status=None,
        observed_season_status=RequestStatus.pending.value,
        scope_episodes_by_season={1: [4], 2: [5], 3: [6]},
    )

    assert [(scope.season_number, scope.episodes, scope.is_target) for scope in intent.scopes] == [
        (1, (4,), True),
        (2, (5,), True),
        (3, None, False),
    ]


async def test_source_error_parks_intent_and_recovers_later_intents(session: AsyncSession) -> None:
    request = MediaRequest(
        tmdb_id=1, media_type=MediaType.movie, title="Movie", status=RequestStatus.pending
    )
    session.add(request)
    await session.flush()
    first = await SqlDownloadAddIntentRepository(session).create(
        CreateDownloadAddIntent(
            torrent_hash="bad",
            source="magnet:bad",
            media_request_id=request.id,
            tmdb_id=1,
            media_type="movie",
            save_path="",
            scopes=(
                DownloadAddIntentScopeCreate(tmdb_id=1, media_type="movie", scope_key="movie"),
            ),
        )
    )
    second = await SqlDownloadAddIntentRepository(session).create(
        CreateDownloadAddIntent(
            torrent_hash="hash",
            source="magnet:good",
            media_request_id=request.id,
            tmdb_id=2,
            media_type="movie",
            save_path="",
            scopes=(
                DownloadAddIntentScopeCreate(tmdb_id=2, media_type="movie", scope_key="movie"),
            ),
        )
    )

    class _MixedClient(_SourceErrorPrepareClient):
        async def prepare_add(self, magnet_or_url: str) -> PreparedAdd:
            if magnet_or_url == "magnet:bad":
                raise QbittorrentSourceError("bad source")
            return await _Client.prepare_add(self, magnet_or_url)

    result = await recover_all(_MixedClient({}), session)

    assert result.needs_attention == 1
    assert result.finalized == 1
    first_after = await SqlDownloadAddIntentRepository(session).get(first.id)
    assert first_after is not None
    assert first_after.state == "needs_attention"
    assert await SqlDownloadAddIntentRepository(session).get(second.id) is None


async def test_cancelled_intent_removes_only_owned_category(session: AsyncSession) -> None:
    intent = await SqlDownloadAddIntentRepository(session).create(
        CreateDownloadAddIntent(
            torrent_hash="hash",
            tmdb_id=1,
            media_type="movie",
            save_path="",
            scopes=(
                DownloadAddIntentScopeCreate(tmdb_id=1, media_type="movie", scope_key="movie"),
            ),
        )
    )
    await SqlDownloadAddIntentRepository(session).mark_state(intent.id, "cancel_requested")
    client = _Client(
        {
            "hash": DownloadStatus(
                info_hash="hash",
                name="torrent",
                raw_state="downloading",
                category=intent_category(intent.id),
            )
        }
    )

    result = await recover_all(client, session)

    assert result.removed == 1
    assert client.removes == ["hash"]
    assert await SqlDownloadAddIntentRepository(session).get(intent.id) is None
