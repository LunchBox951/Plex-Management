"""Queue — grab (idempotent), reconcile transitions, and operator mark-failed."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.domain.release import CandidateRelease
from plex_manager.models import Download, DownloadHistory, DownloadHistoryEvent, MediaRequest
from plex_manager.ports.download_client import DownloadStatus
from plex_manager.ports.metadata import MovieMetadata
from plex_manager.ports.repositories import DownloadRecord
from plex_manager.repositories.downloads import SqlDownloadRepository
from tests.web.fakes import (
    FakeProwlarr,
    FakeQbittorrent,
    FakeTmdb,
    candidate,
    override_adapters,
    prerelease_only_candidates,
)

SeedFn = Callable[..., Awaitable[None]]
SessionMaker = async_sessionmaker[AsyncSession]

_API_KEY = "queue-key"
_HEADERS = {"X-Api-Key": _API_KEY}

_GOOD = "Some.Movie.2020.1080p.WEB-DL.x264-GROUP"
_GOOD_HASH = "3" * 40


async def _insert_download(
    sm: SessionMaker,
    *,
    torrent_hash: str,
    status: str,
    first_seen_at: datetime | None = None,
) -> int:
    async with sm() as session:
        row = Download(
            torrent_hash=torrent_hash,
            status=status,
            first_seen_at=first_seen_at,
            tmdb_id=603,
        )
        session.add(row)
        await session.commit()
        return row.id


async def _create_request(app: FastAPI, client: httpx.AsyncClient) -> int:
    override_adapters(
        app,
        tmdb=FakeTmdb(movies={603: MovieMetadata(tmdb_id=603, title="Some Movie", year=2020)}),
    )
    created = await client.post(
        "/api/v1/requests", json={"tmdb_id": 603, "media_type": "movie"}, headers=_HEADERS
    )
    assert created.status_code == 201
    return int(created.json()["id"])


async def test_grab_creates_download_and_history_and_is_idempotent(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    request_id = await _create_request(app, client)

    qbt = FakeQbittorrent()
    override_adapters(
        app,
        prowlarr=FakeProwlarr([candidate(_GOOD, info_hash=_GOOD_HASH, seeders=42)]),
        qbt=qbt,
    )

    first = await client.post(
        "/api/v1/queue/grab", json={"request_id": request_id}, headers=_HEADERS
    )
    assert first.status_code == 201
    item = first.json()
    assert item["status"] == "downloading"
    assert item["torrent_hash"] == _GOOD_HASH

    # The grabbed history event is written exactly once.
    async with sessionmaker_() as session:
        events = (
            (
                await session.execute(
                    select(DownloadHistory).where(
                        DownloadHistory.event_type == DownloadHistoryEvent.grabbed
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(events) == 1

    # Re-grabbing the same release is a no-op: no second client add, same row.
    second = await client.post(
        "/api/v1/queue/grab", json={"request_id": request_id}, headers=_HEADERS
    )
    assert second.status_code == 201
    assert second.json()["id"] == item["id"]
    assert len(qbt.added) == 1


async def test_reconcile_applies_completed_and_keeps_client_missing_within_grace(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    completed_id = await _insert_download(
        sessionmaker_, torrent_hash="a" * 40, status="downloading"
    )
    missing_id = await _insert_download(
        sessionmaker_,
        torrent_hash="b" * 40,
        status="client_missing",
        first_seen_at=datetime.now(UTC),  # within the 10-minute grace
    )

    qbt = FakeQbittorrent(
        statuses=[
            DownloadStatus(info_hash="a" * 40, name="completed.torrent", raw_state="stoppedUP"),
        ]
    )
    override_adapters(app, qbt=qbt)

    response = await client.get("/api/v1/queue", headers=_HEADERS)
    assert response.status_code == 200
    by_id = {item["id"]: item for item in response.json()["queue"]}

    # Completed torrent -> import_pending; absent-but-in-grace stays client_missing.
    assert by_id[completed_id]["status"] == "import_pending"
    assert by_id[missing_id]["status"] == "client_missing"


async def test_mark_failed_blocklists(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    download_id = await _insert_download(sessionmaker_, torrent_hash="c" * 40, status="downloading")

    response = await client.post(
        f"/api/v1/queue/{download_id}/mark-failed",
        params={"blocklist": "true"},
        headers=_HEADERS,
    )
    assert response.status_code == 200
    assert response.json()["status"] == "failed"

    blocklist = await client.get("/api/v1/blocklist", headers=_HEADERS)
    entries = blocklist.json()["entries"]
    assert len(entries) == 1
    assert entries[0]["torrent_hash"] == "c" * 40


async def test_grab_refuses_cam_only_release_and_adds_nothing(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    """The hard cutoff at the WIRED grab path: a CAM/TS-only indexer result yields
    409 no_acceptable_release and NOTHING is handed to the download client."""
    await seed(initialized=True, app_api_key=_API_KEY)
    request_id = await _create_request(app, client)

    qbt = FakeQbittorrent()
    override_adapters(app, prowlarr=FakeProwlarr(prerelease_only_candidates()), qbt=qbt)

    response = await client.post(
        "/api/v1/queue/grab", json={"request_id": request_id}, headers=_HEADERS
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "no_acceptable_release"
    # The CAM/TS never reached qBittorrent — nothing was leaked to the client.
    assert qbt.added == []


async def test_grab_no_acceptable_release_marks_request(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """A grab that finds nothing acceptable returns 409 AND persists the dead-end on
    the owning request (no_acceptable_release) — honesty over silence: the request
    no longer lingers as 'pending'/'searching' with nothing in flight."""
    await seed(initialized=True, app_api_key=_API_KEY)
    request_id = await _create_request(app, client)
    override_adapters(
        app, prowlarr=FakeProwlarr(prerelease_only_candidates()), qbt=FakeQbittorrent()
    )

    response = await client.post(
        "/api/v1/queue/grab", json={"request_id": request_id}, headers=_HEADERS
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "no_acceptable_release"

    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
    assert request is not None
    assert request.status.value == "no_acceptable_release"


async def test_grab_recovers_from_concurrent_insert_conflict(
    app: FastAPI,
    client: httpx.AsyncClient,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two concurrent grabs for the same release both pass the get_by_hash guard
    then both INSERT; the loser hits UNIQUE(torrent_hash). Instead of an opaque 500,
    it recovers by re-fetching and reusing the winner row — no duplicate inserted."""
    await seed(initialized=True, app_api_key=_API_KEY)
    request_id = await _create_request(app, client)
    override_adapters(
        app,
        prowlarr=FakeProwlarr([candidate(_GOOD, info_hash=_GOOD_HASH, seeders=42)]),
        qbt=FakeQbittorrent(),
    )

    real_create = SqlDownloadRepository.create
    calls = {"n": 0}

    async def conflicting_create(
        self: SqlDownloadRepository,
        *,
        torrent_hash: str,
        status: str,
        media_request_id: int | None = None,
        magnet_link: str | None = None,
        tmdb_id: int | None = None,
        year: int | None = None,
        season: int | None = None,
    ) -> DownloadRecord:
        if calls["n"] == 0:
            calls["n"] = 1
            # Simulate the winning concurrent transaction landing the row first
            # (committed, so it survives the loser's rollback), then this insert
            # losing the UNIQUE race.
            async with sessionmaker_() as winner:
                winner.add(
                    Download(
                        torrent_hash=torrent_hash,
                        status=status,
                        media_request_id=media_request_id,
                        tmdb_id=tmdb_id,
                    )
                )
                await winner.commit()
            raise IntegrityError(
                "INSERT INTO downloads",
                {},
                Exception("UNIQUE constraint failed: downloads.torrent_hash"),
            )
        return await real_create(
            self,
            torrent_hash=torrent_hash,
            status=status,
            media_request_id=media_request_id,
            magnet_link=magnet_link,
            tmdb_id=tmdb_id,
            year=year,
            season=season,
        )

    monkeypatch.setattr(SqlDownloadRepository, "create", conflicting_create)

    response = await client.post(
        "/api/v1/queue/grab", json={"request_id": request_id}, headers=_HEADERS
    )
    assert response.status_code == 201
    assert response.json()["torrent_hash"] == _GOOD_HASH

    async with sessionmaker_() as session:
        rows = (
            (await session.execute(select(Download).where(Download.torrent_hash == _GOOD_HASH)))
            .scalars()
            .all()
        )
    assert len(rows) == 1  # the winner only — the loser inserted no duplicate


async def test_grab_operator_chosen_release_grabs_that_one(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    """A grab targeting a specific (non-top) accepted release by info_hash grabs
    THAT release, not the top-ranked default."""
    await seed(initialized=True, app_api_key=_API_KEY)
    request_id = await _create_request(app, client)

    # Two acceptable releases; the 720p ranks below the 1080p, so it is never the
    # default pick — choosing it by hash proves operator selection works.
    top = candidate("Some.Movie.2020.1080p.WEB-DL.x264-GROUP", info_hash="3" * 40, seeders=99)
    chosen = candidate("Some.Movie.2020.720p.WEB-DL.x264-GROUP", info_hash="7" * 40, seeders=5)
    qbt = FakeQbittorrent()
    override_adapters(app, prowlarr=FakeProwlarr([top, chosen]), qbt=qbt)

    response = await client.post(
        "/api/v1/queue/grab",
        json={"request_id": request_id, "info_hash": "7" * 40},
        headers=_HEADERS,
    )
    assert response.status_code == 201
    assert response.json()["torrent_hash"] == "7" * 40


async def test_grab_unknown_chosen_hash_returns_404(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    request_id = await _create_request(app, client)
    override_adapters(
        app,
        prowlarr=FakeProwlarr([candidate(_GOOD, info_hash=_GOOD_HASH)]),
        qbt=FakeQbittorrent(),
    )

    response = await client.post(
        "/api/v1/queue/grab",
        json={"request_id": request_id, "info_hash": "9" * 40},
        headers=_HEADERS,
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "release_not_found"


async def test_grab_release_without_source_returns_409_no_grab_source(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    """A chosen release exposing neither magnet nor download url is an honest 409,
    never a silent skip (NoGrabSourceError surface)."""
    await seed(initialized=True, app_api_key=_API_KEY)
    request_id = await _create_request(app, client)

    # An accepted (good-quality) candidate with NO grab source at all.
    sourceless = CandidateRelease(
        guid="Some.Movie.2020.1080p.WEB-DL.x264-GROUP",
        title="Some.Movie.2020.1080p.WEB-DL.x264-GROUP",
        size_bytes=1_000_000_000,
        magnet_url=None,
        download_url=None,
        info_hash=_GOOD_HASH,
        seeders=10,
        leechers=1,
        indexer_id=1,
        indexer_name="FakeIndexer",
        publish_date=datetime(2020, 1, 1, tzinfo=UTC),
    )
    qbt = FakeQbittorrent()
    override_adapters(app, prowlarr=FakeProwlarr([sourceless]), qbt=qbt)

    response = await client.post(
        "/api/v1/queue/grab", json={"request_id": request_id}, headers=_HEADERS
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "no_grab_source"
    assert qbt.added == []


async def test_regrab_after_mark_failed_without_blocklist_reuses_row(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """grab -> mark-failed(blocklist=false) -> grab-same-release must NOT crash on
    the UNIQUE torrent_hash. The terminal (Failed) row is reused, driven back to
    Downloading, rather than colliding on a fresh insert."""
    await seed(initialized=True, app_api_key=_API_KEY)
    request_id = await _create_request(app, client)

    qbt = FakeQbittorrent()
    override_adapters(
        app,
        prowlarr=FakeProwlarr([candidate(_GOOD, info_hash=_GOOD_HASH, seeders=42)]),
        qbt=qbt,
    )

    first = await client.post(
        "/api/v1/queue/grab", json={"request_id": request_id}, headers=_HEADERS
    )
    assert first.status_code == 201
    download_id = first.json()["id"]

    # Fail WITHOUT blocklisting: the release stays acceptable to the decision engine.
    failed = await client.post(
        f"/api/v1/queue/{download_id}/mark-failed",
        params={"blocklist": "false"},
        headers=_HEADERS,
    )
    assert failed.status_code == 200
    assert failed.json()["status"] == "failed"

    # Re-grab the same release: previously a 500 (UNIQUE violation); now heals.
    regrab = await client.post(
        "/api/v1/queue/grab", json={"request_id": request_id}, headers=_HEADERS
    )
    assert regrab.status_code == 201
    item = regrab.json()
    assert item["id"] == download_id  # same row reused
    assert item["status"] == "downloading"
    assert item["failed_reason"] is None  # stale failure reason cleared

    # Exactly one downloads row for the hash (no duplicate inserted).
    async with sessionmaker_() as session:
        rows = (
            (await session.execute(select(Download).where(Download.torrent_hash == _GOOD_HASH)))
            .scalars()
            .all()
        )
    assert len(rows) == 1


async def test_mark_failed_without_blocklist_rearms_request_via_api(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """Through the wired endpoint: mark-failed?blocklist=false re-arms the request
    to 'searching' (no zombie 'downloading' request without an active download)."""
    await seed(initialized=True, app_api_key=_API_KEY)
    request_id = await _create_request(app, client)
    override_adapters(
        app,
        prowlarr=FakeProwlarr([candidate(_GOOD, info_hash=_GOOD_HASH, seeders=42)]),
        qbt=FakeQbittorrent(),
    )
    grabbed = await client.post(
        "/api/v1/queue/grab", json={"request_id": request_id}, headers=_HEADERS
    )
    download_id = grabbed.json()["id"]

    await client.post(
        f"/api/v1/queue/{download_id}/mark-failed",
        params={"blocklist": "false"},
        headers=_HEADERS,
    )

    detail = await client.get(f"/api/v1/requests/{request_id}", headers=_HEADERS)
    assert detail.json()["status"] == "searching"


async def test_queue_requires_api_key(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(app, qbt=FakeQbittorrent())
    response = await client.get("/api/v1/queue")
    assert response.status_code == 401
