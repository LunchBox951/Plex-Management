"""Durable add-intent recovery lifecycle."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.models import (
    Download,
    DownloadHistory,
    DownloadScope,
    MediaRequest,
    MediaType,
    RequestStatus,
)
from plex_manager.ports.download_client import AddResult, DownloadStatus, PreparedAdd
from plex_manager.ports.repositories import CreateDownloadAddIntent, DownloadAddIntentScopeCreate
from plex_manager.repositories.download_add_intents import SqlDownloadAddIntentRepository
from plex_manager.services.download_add_intent_service import (
    intent_category,
    recover_all,
    submit_and_finalize,
)


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
