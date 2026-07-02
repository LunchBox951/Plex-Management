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
from plex_manager.domain.state_machine import DownloadState
from plex_manager.models import (
    Download,
    DownloadHistory,
    DownloadHistoryEvent,
    MediaRequest,
    MediaType,
    RequestStatus,
    SeasonRequest,
)
from plex_manager.ports.download_client import DownloadStatus
from plex_manager.ports.metadata import MovieMetadata, TvMetadata
from plex_manager.ports.repositories import DownloadRecord
from plex_manager.repositories.downloads import SqlDownloadRepository
from tests.web.fakes import (
    FakeLibrary,
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


async def test_get_queue_is_passive_and_does_not_reconcile(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """G5: GET /queue is read-only. The background reconcile loop is the single owner
    of cross-system truth; a queue poll must NOT reconcile — a concurrent write could
    clobber the importer's CAS-claimed ``importing`` status. So GET /queue lists the
    queue WITHOUT mutating any row: a ``downloading`` row whose client snapshot reports
    complete is NOT advanced to import_pending by the read, and an ``importing`` row
    stays ``importing``. (Reconcile transitions are proven on the background path in
    tests/services/test_queue_service.py.)"""
    await seed(initialized=True, app_api_key=_API_KEY)
    downloading_id = await _insert_download(
        sessionmaker_, torrent_hash="a" * 40, status="downloading"
    )
    importing_id = await _insert_download(sessionmaker_, torrent_hash="b" * 40, status="importing")

    # A snapshot that, IF the read reconciled, would advance the downloading row to
    # import_pending (stoppedUP). Passive GET never consults it — the endpoint no
    # longer depends on qBittorrent, so this override is inert by design.
    override_adapters(
        app,
        qbt=FakeQbittorrent(
            statuses=[
                DownloadStatus(info_hash="a" * 40, name="done.torrent", raw_state="stoppedUP"),
                DownloadStatus(info_hash="b" * 40, name="imp.torrent", raw_state="stoppedUP"),
            ]
        ),
    )

    response = await client.get("/api/v1/queue", headers=_HEADERS)
    assert response.status_code == 200
    by_id = {item["id"]: item for item in response.json()["queue"]}

    # Both rows are returned, with their persisted status UNCHANGED by the read.
    assert by_id[downloading_id]["status"] == "downloading"
    assert by_id[importing_id]["status"] == "importing"

    # And the DB rows were not mutated — no reconcile clobber from a poll.
    async with sessionmaker_() as session:
        downloading = await session.get(Download, downloading_id)
        importing = await session.get(Download, importing_id)
    assert downloading is not None and downloading.status == "downloading"
    assert importing is not None and importing.status == "importing"


async def test_mark_failed_blocklists(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    download_id = await _insert_download(sessionmaker_, torrent_hash="c" * 40, status="downloading")
    qbt = FakeQbittorrent()
    override_adapters(app, qbt=qbt)

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

    # ADR-0014 seeding-leak fix: mark-failed removed the torrent WITH its data.
    assert qbt.removed == [("c" * 40, True)]


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


async def test_grab_no_acceptable_release_marks_the_season_not_the_show(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """A TV grab that finds nothing acceptable records the dead-end on the SEASON
    (a visible, retryable SeasonRequest.no_acceptable_release), while the parent
    MediaRequest.status stays a computed rollup — never a direct write. The season
    service is flush-only, so the endpoint owns the commit; without it the season
    write would be silently rolled back."""
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(
        app,
        tmdb=FakeTmdb(
            shows={901: TvMetadata(tmdb_id=901, title="Dead End Show", year=2021, season_count=3)}
        ),
    )
    created = await client.post(
        "/api/v1/requests",
        json={"tmdb_id": 901, "media_type": "tv", "seasons": [2]},
        headers=_HEADERS,
    )
    assert created.status_code == 201
    request_id = int(created.json()["id"])

    # Only prerelease/CAM-grade candidates come back, so nothing is acceptable.
    override_adapters(
        app, prowlarr=FakeProwlarr(prerelease_only_candidates()), qbt=FakeQbittorrent()
    )
    response = await client.post(
        "/api/v1/queue/grab",
        json={"request_id": request_id, "season": 2},
        headers=_HEADERS,
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "no_acceptable_release"

    async with sessionmaker_() as session:
        season = (
            await session.execute(
                select(SeasonRequest).where(
                    SeasonRequest.media_request_id == request_id,
                    SeasonRequest.season_number == 2,
                )
            )
        ).scalar_one()
        # The SEASON carries the honest dead-end (committed, not rolled back).
        assert season.status.value == "no_acceptable_release"
        # And the parent rolls up to match (single requested season).
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
        episodes: list[int] | None = None,
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


async def test_grab_rejects_second_active_release_for_same_request(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """A request that already has an ACTIVE download must not spawn a second one for
    a DIFFERENT release: the parallel grab is refused 409 already_downloading and no
    second active row is created (later failure of one must not re-arm the request
    while the other still runs)."""
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

    # A DIFFERENT acceptable release for the same request, while the first is still
    # active (downloading). Grabbing it by hash must be refused.
    other = candidate("Some.Movie.2020.720p.WEB-DL.x264-GROUP", info_hash="7" * 40, seeders=5)
    qbt2 = FakeQbittorrent()
    override_adapters(app, prowlarr=FakeProwlarr([other]), qbt=qbt2)
    second = await client.post(
        "/api/v1/queue/grab",
        json={"request_id": request_id, "info_hash": "7" * 40},
        headers=_HEADERS,
    )
    assert second.status_code == 409
    assert second.json()["detail"] == "already_downloading"
    # The second release never reached the client, and no parallel row exists.
    assert qbt2.added == []
    async with sessionmaker_() as session:
        rows = (
            (await session.execute(select(Download).where(Download.media_request_id == request_id)))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].torrent_hash == _GOOD_HASH


async def test_grab_no_acceptable_release_keeps_active_download_status(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """A re-search (grab) for a request that ALREADY has an active download, where the
    fresh preview finds nothing acceptable, must NOT flip the request to the dead-end
    no_acceptable_release: a download is still in flight, so the request stays
    'downloading' (honesty over silence cuts both ways — don't assert a dead-end that
    isn't true)."""
    await seed(initialized=True, app_api_key=_API_KEY)
    request_id = await _create_request(app, client)

    # First grab lands an active download and drives the request to 'downloading'.
    override_adapters(
        app,
        prowlarr=FakeProwlarr([candidate(_GOOD, info_hash=_GOOD_HASH, seeders=42)]),
        qbt=FakeQbittorrent(),
    )
    first = await client.post(
        "/api/v1/queue/grab", json={"request_id": request_id}, headers=_HEADERS
    )
    assert first.status_code == 201

    # Re-search now returns nothing acceptable while the download is still active.
    override_adapters(
        app, prowlarr=FakeProwlarr(prerelease_only_candidates()), qbt=FakeQbittorrent()
    )
    second = await client.post(
        "/api/v1/queue/grab", json={"request_id": request_id}, headers=_HEADERS
    )
    assert second.status_code == 409
    assert second.json()["detail"] == "no_acceptable_release"

    # The request was left untouched — still downloading, not a false dead-end.
    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
    assert request is not None
    assert request.status.value == "downloading"


async def test_grab_terminal_request_returns_409_and_adds_nothing(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """Grabbing a stale TERMINAL request id is an honest 409 request_not_active and
    nothing is handed to the client (no untracked torrent left behind)."""
    await seed(initialized=True, app_api_key=_API_KEY)
    request_id = await _create_request(app, client)

    # Drive the request terminal (a newer active request would own the media slot).
    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        assert request is not None
        request.status = RequestStatus.completed
        await session.commit()

    qbt = FakeQbittorrent()
    override_adapters(
        app,
        prowlarr=FakeProwlarr([candidate(_GOOD, info_hash=_GOOD_HASH, seeders=42)]),
        qbt=qbt,
    )
    response = await client.post(
        "/api/v1/queue/grab", json={"request_id": request_id}, headers=_HEADERS
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "request_not_active"
    assert qbt.added == []
    async with sessionmaker_() as session:
        rows = (await session.execute(select(Download))).scalars().all()
    assert rows == []


async def test_grab_evicted_request_returns_409_and_adds_nothing(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """C2 regression: an ``evicted`` request id (ADR-0012's disk-pressure sweep
    already deleted the file) is refused exactly like any other terminal
    status -- an honest 409 request_not_active, nothing handed to the client."""
    await seed(initialized=True, app_api_key=_API_KEY)
    request_id = await _create_request(app, client)

    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        assert request is not None
        request.status = RequestStatus.evicted
        await session.commit()

    qbt = FakeQbittorrent()
    override_adapters(
        app,
        prowlarr=FakeProwlarr([candidate(_GOOD, info_hash=_GOOD_HASH, seeders=42)]),
        qbt=qbt,
    )
    response = await client.post(
        "/api/v1/queue/grab", json={"request_id": request_id}, headers=_HEADERS
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "request_not_active"
    assert qbt.added == []
    async with sessionmaker_() as session:
        rows = (await session.execute(select(Download))).scalars().all()
    assert rows == []


async def test_grab_terminal_request_with_empty_preview_stays_terminal(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """A stale TERMINAL request grabbed when the fresh preview finds nothing
    acceptable must NOT be flipped to no_acceptable_release: that non-terminal,
    dedup-blocking status would resurrect the finished request as a ghost. The
    empty-preview path is rejected up front (409 request_not_active) and the
    request's terminal status is left intact — never un-terminate a finished
    request."""
    await seed(initialized=True, app_api_key=_API_KEY)
    request_id = await _create_request(app, client)

    # Drive the request terminal (media already obtained); a newer active request
    # would own the media slot.
    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        assert request is not None
        request.status = RequestStatus.available
        await session.commit()

    # Only CAM/TS available -> empty preview, the path that previously clobbered
    # the terminal status via mark_no_acceptable_release.
    qbt = FakeQbittorrent()
    override_adapters(app, prowlarr=FakeProwlarr(prerelease_only_candidates()), qbt=qbt)
    response = await client.post(
        "/api/v1/queue/grab", json={"request_id": request_id}, headers=_HEADERS
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "request_not_active"
    assert qbt.added == []

    # The terminal status is untouched — the finished request was not resurrected.
    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
    assert request is not None
    assert request.status.value == "available"


async def test_grab_loser_orphaned_torrent_is_removed_from_client(
    app: FastAPI,
    client: httpx.AsyncClient,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two DIFFERENT releases for the same request race past the pre-add guard and
    both reach qBittorrent. The loser's INSERT hits uq_downloads_active_request; it
    must remove the torrent it just added (best-effort) before returning 409, so no
    untracked torrent is left seeding."""
    await seed(initialized=True, app_api_key=_API_KEY)
    request_id = await _create_request(app, client)

    qbt = FakeQbittorrent()
    override_adapters(
        app,
        prowlarr=FakeProwlarr([candidate(_GOOD, info_hash=_GOOD_HASH, seeders=42)]),
        qbt=qbt,
    )

    real_create = SqlDownloadRepository.create
    calls = {"n": 0}
    winner_hash = "9" * 40

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
        episodes: list[int] | None = None,
    ) -> DownloadRecord:
        if calls["n"] == 0:
            calls["n"] = 1
            # A concurrent grab of a DIFFERENT release wins the request's single
            # active slot (committed), then this insert loses the partial-unique
            # uq_downloads_active_request race.
            async with sessionmaker_() as winner:
                winner.add(
                    Download(
                        torrent_hash=winner_hash,
                        status="downloading",
                        media_request_id=media_request_id,
                        tmdb_id=tmdb_id,
                    )
                )
                await winner.commit()
            raise IntegrityError(
                "INSERT INTO downloads",
                {},
                Exception("UNIQUE constraint failed: uq_downloads_active_request"),
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
    assert response.status_code == 409
    assert response.json()["detail"] == "already_downloading"

    # The loser added its torrent to the client, then removed it (with its files).
    assert (_GOOD_HASH, True) in qbt.removed
    # Only the winner's row survives — the loser tracked nothing.
    async with sessionmaker_() as session:
        rows = (await session.execute(select(Download))).scalars().all()
    assert {row.torrent_hash for row in rows} == {winner_hash}


async def test_queue_requires_api_key(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(app, qbt=FakeQbittorrent())
    response = await client.get("/api/v1/queue")
    assert response.status_code == 401


async def test_import_endpoint_no_upfront_409_for_unset_root_nonexistent_download(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    # Neither root is configured, and download id 1 doesn't exist: with the roots
    # now OPTIONAL dependencies (no upfront 409 the way the required Plex/qBittorrent
    # deps still 409), the endpoint reaches import_download, which honestly reports
    # 404 download_not_found rather than an unrelated service_not_configured.
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(app, qbt=FakeQbittorrent(), library=FakeLibrary())
    response = await client.post("/api/v1/queue/1/import", headers=_HEADERS)
    assert response.status_code == 404
    assert response.json()["detail"] == "download_not_found"


async def _insert_movie_download(sm: SessionMaker) -> tuple[int, int]:
    """Insert a movie request + an ImportPending download; (download_id, request_id)."""
    async with sm() as session:
        request = MediaRequest(
            tmdb_id=603,
            media_type=MediaType.movie,
            title="Some Movie",
            year=2020,
            status=RequestStatus.downloading,
        )
        session.add(request)
        await session.flush()
        download = Download(
            torrent_hash="a" * 40,
            status=DownloadState.ImportPending.value,
            media_request_id=request.id,
            tmdb_id=603,
            year=2020,
        )
        session.add(download)
        await session.commit()
        return download.id, request.id


async def _insert_tv_download(sm: SessionMaker, *, season: int = 1) -> tuple[int, int]:
    """Insert a tv request + one tracked season + an ImportPending download."""
    async with sm() as session:
        request = MediaRequest(
            tmdb_id=900,
            media_type=MediaType.tv,
            title="Some Show",
            year=2020,
            status=RequestStatus.downloading,
        )
        session.add(request)
        await session.flush()
        session.add(
            SeasonRequest(media_request_id=request.id, season_number=season, status="downloading")
        )
        download = Download(
            torrent_hash="b" * 40,
            status=DownloadState.ImportPending.value,
            media_request_id=request.id,
            tmdb_id=900,
            year=2020,
            season=season,
        )
        session.add(download)
        await session.commit()
        return download.id, request.id


async def test_import_endpoint_movies_root_unset_is_an_honest_block_not_a_409(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    # movies_root is unset (never configured via PUT /settings); Plex + qBittorrent
    # ARE configured. The endpoint still runs (no upfront 409) and reports the
    # honest, retryable ImportBlocked as a normal 200 -- the correction-without-a
    # -terminal button, not a dead end.
    await seed(initialized=True, app_api_key=_API_KEY)
    download_id, _request_id = await _insert_movie_download(sessionmaker_)
    override_adapters(app, qbt=FakeQbittorrent(), library=FakeLibrary())

    response = await client.post(f"/api/v1/queue/{download_id}/import", headers=_HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "import_blocked"
    assert body["failed_reason"] == "movies library root is not configured"


async def test_import_endpoint_tv_root_unset_is_an_honest_block_not_a_409(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    # Symmetric to the movies-side case: tv_root is unset, movies_root may or may
    # not be -- either way a tv download gets its OWN honest block, never a crash
    # and never gated on the OTHER root.
    await seed(initialized=True, app_api_key=_API_KEY)
    download_id, _request_id = await _insert_tv_download(sessionmaker_)
    override_adapters(app, qbt=FakeQbittorrent(), library=FakeLibrary())

    response = await client.post(f"/api/v1/queue/{download_id}/import", headers=_HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "import_blocked"
    assert body["failed_reason"] == "tv library root is not configured"
    assert body["season"] == 1


async def test_grab_threads_season_and_episodes_into_search_and_queue_item(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    """A TV grab scoped to specific episode(s) threads BOTH season and episodes:
    into the indexer search (a single named episode narrows the search itself)
    and onto the persisted ``QueueItem``, so the queue can render "S02E05"."""
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(
        app,
        tmdb=FakeTmdb(
            shows={900: TvMetadata(tmdb_id=900, title="Some Show", year=2020, season_count=2)}
        ),
    )
    created = await client.post(
        "/api/v1/requests",
        json={"tmdb_id": 900, "media_type": "tv", "seasons": [2]},
        headers=_HEADERS,
    )
    assert created.status_code == 201
    request_id = created.json()["id"]

    prowlarr = FakeProwlarr(
        [candidate("Some.Show.S02E05.1080p.WEB-DL.x264-GROUP", info_hash="5" * 40)]
    )
    qbt = FakeQbittorrent()
    override_adapters(app, prowlarr=prowlarr, qbt=qbt)

    response = await client.post(
        "/api/v1/queue/grab",
        json={"request_id": request_id, "season": 2, "episodes": [5]},
        headers=_HEADERS,
    )
    assert response.status_code == 201
    body = response.json()
    assert body["season"] == 2
    assert body["episodes"] == [5]

    # The indexer search itself was narrowed to the single named episode.
    assert prowlarr.searched[-1].season == 2
    assert prowlarr.searched[-1].episode == "5"


async def test_grab_tv_without_season_rejected_422_and_adds_nothing(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """F1: a tv request grabbed with NO season is rejected up front (422), BEFORE
    run_preview even runs an unscoped search -- no Download row, no SeasonRequest
    spuriously created, the parent's computed rollup status is untouched, and
    NOTHING is handed to qBittorrent (an unscoped tv grab would otherwise update
    the parent MediaRequest directly instead of a SeasonRequest, corrupting the
    computed-rollup invariant, and the importer would later block it as
    season-less)."""
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(
        app,
        tmdb=FakeTmdb(
            shows={900: TvMetadata(tmdb_id=900, title="Some Show", year=2020, season_count=2)}
        ),
    )
    created = await client.post(
        "/api/v1/requests",
        json={"tmdb_id": 900, "media_type": "tv", "seasons": [2]},
        headers=_HEADERS,
    )
    assert created.status_code == 201
    request_id = created.json()["id"]

    async with sessionmaker_() as session:
        pre_request = await session.get(MediaRequest, request_id)
        assert pre_request is not None
        pre_status = pre_request.status
        pre_season_count = len(
            (
                await session.execute(
                    select(SeasonRequest).where(SeasonRequest.media_request_id == request_id)
                )
            )
            .scalars()
            .all()
        )

    qbt = FakeQbittorrent()
    override_adapters(
        app,
        prowlarr=FakeProwlarr(
            [candidate("Some.Show.S02E05.1080p.WEB-DL.x264-GROUP", info_hash="5" * 40)]
        ),
        qbt=qbt,
    )

    response = await client.post(
        "/api/v1/queue/grab", json={"request_id": request_id}, headers=_HEADERS
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "tv_grab_requires_season"

    # Nothing was ever handed to the download client.
    assert qbt.added == []

    async with sessionmaker_() as session:
        downloads = (await session.execute(select(Download))).scalars().all()
        assert downloads == []  # no Download row created at all

        post_request = await session.get(MediaRequest, request_id)
        assert post_request is not None
        assert post_request.status == pre_status  # unchanged computed rollup

        seasons = (
            (
                await session.execute(
                    select(SeasonRequest).where(SeasonRequest.media_request_id == request_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(seasons) == pre_season_count  # no season rows spuriously created


async def test_grab_movie_with_season_rejected_422_and_adds_nothing(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """F6: a movie grab carrying a ``season`` is rejected up front (422) rather
    than silently treated as tv -- a movie must never spawn a SeasonRequest row
    or have its one-active-download guard scoped to a fake season."""
    await seed(initialized=True, app_api_key=_API_KEY)
    request_id = await _create_request(app, client)

    qbt = FakeQbittorrent()
    override_adapters(
        app,
        prowlarr=FakeProwlarr([candidate(_GOOD, info_hash=_GOOD_HASH, seeders=42)]),
        qbt=qbt,
    )

    response = await client.post(
        "/api/v1/queue/grab",
        json={"request_id": request_id, "season": 1},
        headers=_HEADERS,
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "movie_grab_rejects_season"
    assert qbt.added == []

    async with sessionmaker_() as session:
        downloads = (await session.execute(select(Download))).scalars().all()
        assert downloads == []
        seasons = (await session.execute(select(SeasonRequest))).scalars().all()
        assert seasons == []
