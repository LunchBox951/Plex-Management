"""Queue — grab (idempotent), reconcile transitions, and operator mark-failed."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import httpx
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.models import Download, DownloadHistory, DownloadHistoryEvent
from plex_manager.ports.download_client import DownloadStatus
from plex_manager.ports.metadata import MovieMetadata
from tests.web.fakes import (
    FakeProwlarr,
    FakeQbittorrent,
    FakeTmdb,
    candidate,
    override_adapters,
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


async def test_queue_requires_api_key(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(app, qbt=FakeQbittorrent())
    response = await client.get("/api/v1/queue")
    assert response.status_code == 401
