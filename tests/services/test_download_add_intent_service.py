"""Durable add-intent recovery lifecycle."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.models import DownloadHistory, MediaRequest, MediaType, RequestStatus
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


async def test_absent_prepared_intent_is_readded_only_after_matching_prepare(
    session: AsyncSession,
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
