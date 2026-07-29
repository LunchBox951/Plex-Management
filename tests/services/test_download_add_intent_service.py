"""Durable add-intent recovery lifecycle."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import cast

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.adapters.qbittorrent.adapter import QbittorrentSourceError
from plex_manager.domain.quality import WEBDL1080P, QualitySource
from plex_manager.domain.release import ParsedRelease, ScoredRelease
from plex_manager.domain.state_machine import DownloadState
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
from plex_manager.ports.download_client import (
    AddResult,
    DownloadClientPort,
    DownloadStatus,
    PreparedAdd,
)
from plex_manager.ports.repositories import CreateDownloadAddIntent, DownloadAddIntentScopeCreate
from plex_manager.repositories.download_add_intents import SqlDownloadAddIntentRepository
from plex_manager.repositories.downloads import SqlDownloadRepository
from plex_manager.services import download_add_intent_service, request_service
from plex_manager.services.correction_service import cancel_request_with_outcome
from plex_manager.services.download_add_intent_service import (
    intent_category,
    publish_intent,
    recover_all,
    recover_for_request,
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


class _DelayedAddClient(_Client):
    def __init__(self, statuses: dict[str, DownloadStatus]) -> None:
        super().__init__(statuses)
        self.add_entered = asyncio.Event()
        self.allow_add = asyncio.Event()
        self.fail_removes = False

    async def add_prepared(self, prepared: PreparedAdd, save_path: str, category: str) -> AddResult:
        self.add_entered.set()
        await self.allow_add.wait()
        return await super().add_prepared(prepared, save_path, category)

    async def remove(self, info_hash: str, *, delete_files: bool) -> None:
        if self.fail_removes:
            raise ConnectionError("remove unavailable")
        await super().remove(info_hash, delete_files=delete_files)


class _DuplicateAddClient(_Client):
    def __init__(self, statuses: dict[str, DownloadStatus], *, created: bool) -> None:
        super().__init__(statuses)
        self.created = created

    async def add_prepared(self, prepared: PreparedAdd, save_path: str, category: str) -> AddResult:
        self.adds.append(category)
        if self.created:
            self.statuses[prepared.torrent_hash] = DownloadStatus(
                info_hash=prepared.torrent_hash,
                name="torrent",
                raw_state="downloading",
                category=category,
            )
        return AddResult(torrent_hash=prepared.torrent_hash, created=self.created)


class _DuplicateForeignRecoveryClient(_DuplicateAddClient):
    def __init__(self) -> None:
        super().__init__({}, created=False)
        self.status_calls = 0

    async def get_status(self, info_hash: str) -> DownloadStatus | None:
        self.status_calls += 1
        if self.status_calls == 1:
            return None
        return DownloadStatus(
            info_hash=info_hash, name="foreign", raw_state="downloading", category="other-app"
        )


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

    finalization = await submit_and_finalize(client, session, intent=intent)

    assert finalization.record is not None
    assert finalization.record.torrent_hash == "hash"
    assert await SqlDownloadAddIntentRepository(session).get(intent.id) is None
    assert len((await session.scalars(select(DownloadHistory))).all()) == 1
    assert client.categories == [("hash", "plex-manager")]
    request_after = await session.get(MediaRequest, request.id)
    assert request_after is not None
    assert request_after.status == RequestStatus.downloading


async def test_foreign_present_hash_parks_without_client_mutation_or_later_removal(
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
    client = _Client(
        {
            "hash": DownloadStatus(
                info_hash="hash", name="foreign", raw_state="downloading", category="other-app"
            )
        }
    )

    recovered = await recover_all(client, session)

    assert recovered == type(recovered)(needs_attention=1)
    stored = await SqlDownloadAddIntentRepository(session).get(intent.id)
    assert stored is not None
    assert stored.state == "needs_attention"
    assert stored.last_error == "client_hash_ownership_unproven"
    assert client.categories == []
    assert client.removes == []
    assert await session.scalar(select(Download).where(Download.torrent_hash == "hash")) is None

    outcome = await cancel_request_with_outcome(
        session, cast(DownloadClientPort, client), request_id=request.id
    )

    assert outcome.record.status == RequestStatus.cancelled.value
    assert client.removes == []
    assert client.statuses["hash"].category == "other-app"


async def test_explicit_adoption_finalizes_foreign_category(session: AsyncSession) -> None:
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
            owns_client_torrent=True,
            scopes=(
                DownloadAddIntentScopeCreate(tmdb_id=1, media_type="movie", scope_key="movie"),
            ),
        )
    )
    client = _Client(
        {
            "hash": DownloadStatus(
                info_hash="hash", name="adopted", raw_state="downloading", category="other-app"
            )
        }
    )

    recovered = await recover_all(client, session)

    assert recovered.finalized == 1
    assert await SqlDownloadAddIntentRepository(session).get(intent.id) is None
    assert client.categories == [("hash", "plex-manager")]


@pytest.mark.parametrize(
    ("created", "category", "finalizes"),
    [
        (False, "other-app", False),
        (False, "plex-manager-intent-{intent_id}", True),
        (True, "other-app", True),
    ],
)
async def test_submit_uses_add_result_ownership_proof(
    session: AsyncSession, created: bool, category: str, finalizes: bool
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
            observed_request_status=RequestStatus.pending.value,
            scopes=(
                DownloadAddIntentScopeCreate(tmdb_id=1, media_type="movie", scope_key="movie"),
            ),
        )
    )
    client = _DuplicateAddClient(
        {
            "hash": DownloadStatus(
                info_hash="hash",
                name="existing",
                raw_state="downloading",
                category=category.format(intent_id=intent.id),
            )
        },
        created=created,
    )

    finalization = await submit_and_finalize(client, session, intent=intent)

    assert (finalization.record is not None) is finalizes
    stored = await SqlDownloadAddIntentRepository(session).get(intent.id)
    if finalizes:
        assert stored is None
        assert client.categories == [("hash", "plex-manager")]
    else:
        assert stored is not None
        assert stored.state == "needs_attention"
        assert stored.last_error == "client_hash_ownership_unproven"
        assert client.categories == []


async def test_cancel_during_submit_removes_late_created_torrent(
    sessionmaker_: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker_() as setup:
        request = MediaRequest(
            tmdb_id=1, media_type=MediaType.movie, title="Movie", status=RequestStatus.pending
        )
        setup.add(request)
        await setup.flush()
        intent = await SqlDownloadAddIntentRepository(setup).create(
            CreateDownloadAddIntent(
                torrent_hash="hash",
                source="magnet:source",
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
        request_id = request.id
        intent_id = intent.id
        await setup.commit()

    client = _DelayedAddClient({})
    async with sessionmaker_() as worker, sessionmaker_() as canceller:
        recovery = asyncio.create_task(recover_all(client, worker))
        await client.add_entered.wait()

        outcome = await cancel_request_with_outcome(
            canceller, cast(DownloadClientPort, client), request_id=request_id
        )

        assert outcome.cleanup_deferred is False
        assert await SqlDownloadAddIntentRepository(canceller).get(intent_id, fresh=True) is None
        client.allow_add.set()
        await recovery

    assert client.removes == ["hash"]
    assert client.statuses == {}
    async with sessionmaker_() as check:
        assert await SqlDownloadAddIntentRepository(check).get(intent_id, fresh=True) is None
        assert await check.scalar(select(Download).where(Download.torrent_hash == "hash")) is None


async def test_late_remove_failure_retains_cancel_requested_cleanup_intent(
    sessionmaker_: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker_() as setup:
        request = MediaRequest(
            tmdb_id=1, media_type=MediaType.movie, title="Movie", status=RequestStatus.pending
        )
        setup.add(request)
        await setup.flush()
        intent = await SqlDownloadAddIntentRepository(setup).create(
            CreateDownloadAddIntent(
                torrent_hash="hash",
                source="magnet:source",
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
        request_id = request.id
        await setup.commit()

    client = _DelayedAddClient({})
    client.fail_removes = True
    async with sessionmaker_() as worker, sessionmaker_() as canceller:
        recovery = asyncio.create_task(recover_all(client, worker))
        await client.add_entered.wait()
        outcome = await cancel_request_with_outcome(
            canceller, cast(DownloadClientPort, client), request_id=request_id
        )
        assert outcome.cleanup_deferred is False
        assert await SqlDownloadAddIntentRepository(canceller).get(intent.id, fresh=True) is None
        client.allow_add.set()
        with pytest.raises(ConnectionError, match="remove unavailable"):
            await recovery

    async with sessionmaker_() as check:
        cleanup = await SqlDownloadAddIntentRepository(check).get_by_hash("hash")
        assert cleanup is not None
        assert cleanup.state == "cancel_requested"
        assert cleanup.owns_client_torrent is True

    client.fail_removes = False
    async with sessionmaker_() as sweep:
        recovered = await recover_all(client, sweep)

    assert recovered.removed == 1
    assert client.statuses == {}
    async with sessionmaker_() as check:
        assert await SqlDownloadAddIntentRepository(check).get_by_hash("hash") is None


async def test_late_cleanup_collision_preserves_new_hash_reservation(
    sessionmaker_: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker_() as session:
        old_request = MediaRequest(
            tmdb_id=1, media_type=MediaType.movie, title="Old", status=RequestStatus.cancelled
        )
        new_request = MediaRequest(
            tmdb_id=2, media_type=MediaType.movie, title="New", status=RequestStatus.pending
        )
        session.add_all((old_request, new_request))
        await session.flush()
        old = await SqlDownloadAddIntentRepository(session).create(
            CreateDownloadAddIntent(
                torrent_hash="hash",
                media_request_id=old_request.id,
                tmdb_id=1,
                media_type="movie",
                save_path="",
            )
        )
        await SqlDownloadAddIntentRepository(session).delete(old.id)
        replacement = await SqlDownloadAddIntentRepository(session).create(
            CreateDownloadAddIntent(
                torrent_hash="hash",
                source="magnet:new",
                media_request_id=new_request.id,
                tmdb_id=2,
                media_type="movie",
                save_path="",
            )
        )
        await session.commit()

        cleanup = await download_add_intent_service.create_late_cleanup(session, old)

        replacement_after = await SqlDownloadAddIntentRepository(session).get(
            replacement.id, fresh=True
        )
        assert replacement_after is not None
        assert replacement_after.state == "prepared"
        assert replacement_after.owns_client_torrent is False
        assert cleanup.id != replacement.id
        assert cleanup.state == "cancel_requested"
        assert cleanup.cleanup_torrent_hash == "hash"
        assert cleanup.cleanup_category == intent_category(old.id)


async def test_synthetic_late_cleanup_retries_only_old_category_removal(
    sessionmaker_: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker_() as session:
        old_request = MediaRequest(
            tmdb_id=1, media_type=MediaType.movie, title="Old", status=RequestStatus.cancelled
        )
        new_request = MediaRequest(
            tmdb_id=2, media_type=MediaType.movie, title="New", status=RequestStatus.pending
        )
        session.add_all((old_request, new_request))
        await session.flush()
        old = await SqlDownloadAddIntentRepository(session).create(
            CreateDownloadAddIntent(
                torrent_hash="hash",
                media_request_id=old_request.id,
                tmdb_id=1,
                media_type="movie",
                save_path="",
            )
        )
        await SqlDownloadAddIntentRepository(session).delete(old.id)
        replacement = await SqlDownloadAddIntentRepository(session).create(
            CreateDownloadAddIntent(
                torrent_hash="hash",
                source="magnet:new",
                media_request_id=new_request.id,
                tmdb_id=2,
                media_type="movie",
                save_path="",
            )
        )
        await session.commit()
        cleanup = await download_add_intent_service.create_late_cleanup(session, old)

    client = _DelayedAddClient(
        {
            "hash": DownloadStatus(
                info_hash="hash",
                name="old",
                raw_state="downloading",
                category=intent_category(old.id),
            )
        }
    )
    client.fail_removes = True
    async with sessionmaker_() as sweep:
        with pytest.raises(ConnectionError, match="remove unavailable"):
            await recover_for_request(client, sweep, request_id=old_request.id)

    async with sessionmaker_() as check:
        replacement_after = await SqlDownloadAddIntentRepository(check).get(
            replacement.id, fresh=True
        )
        cleanup_after = await SqlDownloadAddIntentRepository(check).get(cleanup.id, fresh=True)
        assert replacement_after is not None and replacement_after.state == "prepared"
        assert cleanup_after is not None and cleanup_after.state == "cancel_requested"

    client.fail_removes = False
    async with sessionmaker_() as sweep:
        recovered = await recover_for_request(client, sweep, request_id=old_request.id)
    assert recovered.removed == 1
    assert client.removes == ["hash"]
    async with sessionmaker_() as check:
        assert (
            await SqlDownloadAddIntentRepository(check).get(replacement.id, fresh=True) is not None
        )
        assert await SqlDownloadAddIntentRepository(check).get(cleanup.id, fresh=True) is None


async def test_synthetic_late_cleanup_retires_when_active_download_converges(
    sessionmaker_: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker_() as session:
        old_request = MediaRequest(
            tmdb_id=1, media_type=MediaType.movie, title="Old", status=RequestStatus.cancelled
        )
        new_request = MediaRequest(
            tmdb_id=2, media_type=MediaType.movie, title="New", status=RequestStatus.downloading
        )
        session.add_all((old_request, new_request))
        await session.flush()
        old = await SqlDownloadAddIntentRepository(session).create(
            CreateDownloadAddIntent(
                torrent_hash="hash",
                media_request_id=old_request.id,
                tmdb_id=1,
                media_type="movie",
                save_path="",
            )
        )
        await SqlDownloadAddIntentRepository(session).delete(old.id)
        replacement = await SqlDownloadAddIntentRepository(session).create(
            CreateDownloadAddIntent(
                torrent_hash="hash",
                media_request_id=new_request.id,
                tmdb_id=2,
                media_type="movie",
                save_path="",
            )
        )
        session.add(
            Download(
                torrent_hash="hash",
                status=DownloadState.Downloading.value,
                media_request_id=new_request.id,
                tmdb_id=2,
                media_type=MediaType.movie,
            )
        )
        await session.commit()
        cleanup = await download_add_intent_service.create_late_cleanup(session, old)

    client = _Client(
        {
            "hash": DownloadStatus(
                info_hash="hash",
                name="newly tracked",
                raw_state="downloading",
                category=intent_category(old.id),
            )
        }
    )
    async with sessionmaker_() as sweep:
        recovered = await recover_for_request(client, sweep, request_id=old_request.id)

    assert recovered.removed == 1
    assert client.removes == []
    async with sessionmaker_() as check:
        assert await SqlDownloadAddIntentRepository(check).get(cleanup.id, fresh=True) is None
        assert (
            await SqlDownloadAddIntentRepository(check).get(replacement.id, fresh=True) is not None
        )


async def test_submit_late_add_id_reuse_creates_synthetic_cleanup(
    sessionmaker_: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker_() as setup:
        old_request = MediaRequest(
            tmdb_id=1, media_type=MediaType.movie, title="Old", status=RequestStatus.cancelled
        )
        new_request = MediaRequest(
            tmdb_id=2, media_type=MediaType.movie, title="New", status=RequestStatus.pending
        )
        setup.add_all((old_request, new_request))
        await setup.flush()
        old = await SqlDownloadAddIntentRepository(setup).create(
            CreateDownloadAddIntent(
                torrent_hash="hash",
                source="magnet:old",
                media_request_id=old_request.id,
                tmdb_id=1,
                media_type="movie",
                save_path="",
            )
        )
        await setup.commit()

    client = _DelayedAddClient({})
    async with sessionmaker_() as worker, sessionmaker_() as replacement_session:
        submission = asyncio.create_task(submit_and_finalize(client, worker, intent=old))
        await client.add_entered.wait()
        replacement_repo = SqlDownloadAddIntentRepository(replacement_session)
        await replacement_repo.delete(old.id)
        replacement = await replacement_repo.create(
            CreateDownloadAddIntent(
                torrent_hash="hash",
                source="magnet:new",
                media_request_id=new_request.id,
                tmdb_id=2,
                media_type="movie",
                save_path="",
            )
        )
        await replacement_session.commit()
        assert replacement.id == old.id
        client.allow_add.set()
        await submission

    assert client.removes == ["hash"]
    async with sessionmaker_() as check:
        replacement_after = await SqlDownloadAddIntentRepository(check).get(
            replacement.id, fresh=True
        )
        cleanup = await SqlDownloadAddIntentRepository(check).get_by_hash(f"cleanup:{old.id}:hash")
        assert replacement_after is not None and replacement_after.state == "prepared"
        assert cleanup is None
        assert await check.scalar(select(Download).where(Download.torrent_hash == "hash")) is None


async def test_submit_lost_attention_cas_does_not_report_a_mutation(
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
            source="magnet:source",
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
    await session.commit()
    client = _DuplicateForeignRecoveryClient()
    original_mark_state = SqlDownloadAddIntentRepository.mark_state

    async def lose_to_cancel(
        self: SqlDownloadAddIntentRepository,
        intent_id: int,
        state: str,
        *,
        last_error: str | None = None,
        expected_state: str | None = None,
    ) -> bool:
        if state == "needs_attention":
            assert await original_mark_state(
                self, intent_id, "cancel_requested", expected_state="prepared"
            )
            return False
        return await original_mark_state(
            self,
            intent_id,
            state,
            last_error=last_error,
            expected_state=expected_state,
        )

    monkeypatch.setattr(SqlDownloadAddIntentRepository, "mark_state", lose_to_cancel)

    result = await recover_all(client, session)

    stored = await SqlDownloadAddIntentRepository(session).get(intent.id, fresh=True)
    assert stored is not None
    assert stored.state == "cancel_requested"
    assert result.needs_attention == 0
    assert not result.changed


async def test_recovery_resurrects_terminal_same_hash_download_when_client_owned(
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

    assert result.finalized == 1
    terminal_after = await session.get(Download, terminal_id)
    assert terminal_after is not None
    assert terminal_after.status == "downloading"
    request_after = await session.get(MediaRequest, request_id)
    assert request_after is not None
    assert request_after.status == RequestStatus.downloading
    assert await SqlDownloadAddIntentRepository(session).get(intent.id) is None


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


async def test_cancelled_intent_status_outage_after_remove_stays_recoverable(
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


async def test_same_hash_convergence_locks_active_download_before_attachment(
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


async def test_tv_same_hash_convergence_attaches_new_target_scope(
    session: AsyncSession,
) -> None:
    request = MediaRequest(
        tmdb_id=1, media_type=MediaType.tv, title="Show", status=RequestStatus.pending
    )
    session.add(request)
    await session.flush()
    season_one = SeasonRequest(
        media_request_id=request.id, season_number=1, status=RequestStatus.downloading
    )
    season_two = SeasonRequest(
        media_request_id=request.id, season_number=2, status=RequestStatus.pending
    )
    session.add_all((season_one, season_two))
    await session.flush()
    existing = Download(
        torrent_hash="hash",
        status="downloading",
        media_request_id=request.id,
        tmdb_id=1,
        media_type=MediaType.tv,
        season=1,
    )
    session.add(existing)
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
                    scope_key="season:2",
                    season_number=2,
                    is_target=True,
                ),
            ),
        )
    )
    await session.commit()

    recovered = await recover_all(
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

    assert recovered.finalized == 1
    assert await SqlDownloadAddIntentRepository(session).get(intent.id) is None
    updated_season = await session.get(SeasonRequest, season_two.id)
    assert updated_season is not None and updated_season.status == RequestStatus.downloading
    scopes = (await session.scalars(select(DownloadScope))).all()
    assert [(scope.download_id, scope.season_number) for scope in scopes] == [(existing.id, 2)]


async def test_tv_convergence_locks_before_attaching_scopes(
    sessionmaker_: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    async with sessionmaker_() as setup:
        request = MediaRequest(
            tmdb_id=1, media_type=MediaType.tv, title="Show", status=RequestStatus.downloading
        )
        season = SeasonRequest(media_request_id=1, season_number=2, status=RequestStatus.pending)
        setup.add(request)
        await setup.flush()
        season.media_request_id = request.id
        setup.add(season)
        download = Download(
            torrent_hash="hash",
            status="downloading",
            media_request_id=request.id,
            tmdb_id=1,
            media_type=MediaType.tv,
            season=1,
        )
        setup.add(download)
        await setup.flush()
        intent = await SqlDownloadAddIntentRepository(setup).create(
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
                        scope_key="season:2",
                        season_number=2,
                        is_target=True,
                    ),
                ),
            )
        )
        download_id = download.id
        season_id = season.id
        await setup.commit()

    original_lock = SqlDownloadRepository.lock_if_active
    raced = False

    async def complete_before_lock(repository: SqlDownloadRepository, download_id_: int) -> bool:
        nonlocal raced
        if not raced:
            raced = True
            async with sessionmaker_() as other:
                completed = await other.get(Download, download_id)
                assert completed is not None
                completed.status = DownloadState.Imported.value
                await other.commit()
        return await original_lock(repository, download_id_)

    monkeypatch.setattr(SqlDownloadRepository, "lock_if_active", complete_before_lock)
    async with sessionmaker_() as worker:
        recovered = await recover_all(
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
            worker,
        )

    assert recovered.finalized == 1
    async with sessionmaker_() as check:
        downloaded = await check.get(Download, download_id)
        updated_season = await check.get(SeasonRequest, season_id)
        scopes = (await check.scalars(select(DownloadScope))).all()
        assert downloaded is not None and downloaded.status == DownloadState.Downloading.value
        assert updated_season is not None and updated_season.status == RequestStatus.downloading
        assert [(scope.download_id, scope.season_number) for scope in scopes] == [(download_id, 2)]
        assert await SqlDownloadAddIntentRepository(check).get(intent.id) is None


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


async def test_stale_owned_premise_enters_cleanup_and_recovers_later_intents(
    session: AsyncSession,
) -> None:
    first_request = MediaRequest(
        tmdb_id=1, media_type=MediaType.movie, title="First", status=RequestStatus.pending
    )
    second_request = MediaRequest(
        tmdb_id=2, media_type=MediaType.movie, title="Second", status=RequestStatus.pending
    )
    session.add_all((first_request, second_request))
    await session.flush()
    intents = SqlDownloadAddIntentRepository(session)
    stale = await intents.create(
        CreateDownloadAddIntent(
            torrent_hash="stale",
            media_request_id=first_request.id,
            tmdb_id=1,
            media_type="movie",
            save_path="",
            observed_request_status=RequestStatus.pending.value,
            scopes=(
                DownloadAddIntentScopeCreate(tmdb_id=1, media_type="movie", scope_key="movie"),
            ),
        )
    )
    later = await intents.create(
        CreateDownloadAddIntent(
            torrent_hash="later",
            media_request_id=second_request.id,
            tmdb_id=2,
            media_type="movie",
            save_path="",
            observed_request_status=RequestStatus.pending.value,
            scopes=(
                DownloadAddIntentScopeCreate(tmdb_id=2, media_type="movie", scope_key="movie"),
            ),
        )
    )
    await session.commit()
    assert (
        await request_service.mark_no_acceptable_release(
            session, first_request.id, require_no_active_download_or_intent=True
        )
        is False
    )
    await request_service.mark_no_acceptable_release(session, first_request.id)
    await session.commit()

    result = await recover_all(
        _Client(
            {
                "stale": DownloadStatus(
                    info_hash="stale",
                    name="stale",
                    raw_state="downloading",
                    category=intent_category(stale.id),
                ),
                "later": DownloadStatus(
                    info_hash="later",
                    name="later",
                    raw_state="downloading",
                    category=intent_category(later.id),
                ),
            }
        ),
        session,
    )

    assert result == type(result)(finalized=1)
    stale_after = await intents.get(stale.id, fresh=True)
    assert stale_after is not None
    assert stale_after.state == "cancel_requested"
    assert await intents.get(later.id, fresh=True) is None


async def test_cancel_recovers_only_cancelled_request_intents(session: AsyncSession) -> None:
    cancelled = MediaRequest(
        tmdb_id=1, media_type=MediaType.movie, title="Cancelled", status=RequestStatus.pending
    )
    unrelated = MediaRequest(
        tmdb_id=2, media_type=MediaType.movie, title="Unrelated", status=RequestStatus.pending
    )
    session.add_all((cancelled, unrelated))
    await session.flush()
    intents = SqlDownloadAddIntentRepository(session)
    target = await intents.create(
        CreateDownloadAddIntent(
            torrent_hash="target",
            media_request_id=cancelled.id,
            tmdb_id=1,
            media_type="movie",
            save_path="",
            scopes=(
                DownloadAddIntentScopeCreate(tmdb_id=1, media_type="movie", scope_key="movie"),
            ),
        )
    )
    other = await intents.create(
        CreateDownloadAddIntent(
            torrent_hash="other",
            source="magnet:other",
            media_request_id=unrelated.id,
            tmdb_id=2,
            media_type="movie",
            save_path="",
            scopes=(
                DownloadAddIntentScopeCreate(tmdb_id=2, media_type="movie", scope_key="movie"),
            ),
        )
    )
    await session.commit()

    outcome = await cancel_request_with_outcome(
        session, cast(DownloadClientPort, _FailingPrepareClient({})), request_id=cancelled.id
    )

    assert outcome.record.status == RequestStatus.cancelled.value
    assert not outcome.cleanup_deferred
    assert await intents.get(target.id, fresh=True) is None
    unrelated_after = await intents.get(other.id, fresh=True)
    assert unrelated_after is not None
    assert unrelated_after.state == "prepared"


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
