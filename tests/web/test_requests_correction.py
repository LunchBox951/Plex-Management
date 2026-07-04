"""Correction endpoints (ADR-0014): POST /requests/{id}/report-issue and /cancel.

Focuses on routing, dependency wiring, and HTTP error mapping -- the deep flow
is covered by ``tests/services/test_correction_service.py``. Uses a real on-disk
``movies_root`` setting + file so the endpoint's ``get_eviction_filesystem`` purge
runs against a genuine root-guarded filesystem.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.models import (
    Blocklist,
    Download,
    DownloadHistory,
    DownloadHistoryEvent,
    MediaRequest,
    MediaType,
    RequestStatus,
    Setting,
)
from tests.web.fakes import FakeLibrary, FakeProwlarr, FakeQbittorrent, candidate, override_adapters

SeedFn = Callable[..., Awaitable[None]]
SessionMaker = async_sessionmaker[AsyncSession]

_API_KEY = "correction-key"
_HEADERS = {"X-Api-Key": _API_KEY}
_TMDB = 603
_CULPRIT = "3" * 40
_ALT = "a" * 40


async def _set_setting(sm: SessionMaker, key: str, value: str) -> None:
    async with sm() as session:
        session.add(Setting(key=key, value=value))
        await session.commit()


async def _seed_available_movie(
    sm: SessionMaker, *, library_path: str, is_anime: bool = False
) -> int:
    async with sm() as session:
        request = MediaRequest(
            tmdb_id=_TMDB,
            media_type=MediaType.movie,
            title="Some Movie",
            year=2020,
            status=RequestStatus.available,
            library_path=library_path,
            is_anime=is_anime,
        )
        session.add(request)
        await session.flush()
        session.add(
            Download(
                torrent_hash=_CULPRIT,
                status="imported",
                media_request_id=request.id,
                tmdb_id=_TMDB,
                year=2020,
            )
        )
        session.add(
            DownloadHistory(
                tmdb_id=_TMDB,
                torrent_hash=_CULPRIT,
                event_type=DownloadHistoryEvent.grabbed,
                source_title="Some.Movie.2020.1080p.BluRay.x264-GROUP",
                indexer="FakeIndexer",
            )
        )
        await session.commit()
        return request.id


async def test_report_issue_endpoint_blocklists_purges_and_regrabs(
    app: FastAPI,
    client: httpx.AsyncClient,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    tmp_path: Path,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    root = tmp_path / "movies"
    root.mkdir()
    movie_file = root / "Some Movie (2020).mkv"
    movie_file.write_bytes(b"x" * 4096)
    await _set_setting(sessionmaker_, "movies_root", str(root))
    request_id = await _seed_available_movie(sessionmaker_, library_path=str(movie_file))

    qbt = FakeQbittorrent()
    override_adapters(
        app,
        library=FakeLibrary(),
        qbt=qbt,
        prowlarr=FakeProwlarr(
            [
                candidate("Some.Movie.2020.1080p.BluRay.x264-GROUP", info_hash=_CULPRIT),
                candidate("Some.Movie.2020.1080p.WEB-DL.x264-OTHER", info_hash=_ALT),
            ]
        ),
    )

    response = await client.post(
        f"/api/v1/requests/{request_id}/report-issue",
        json={"reason": "bad_quality"},
        headers=_HEADERS,
    )
    assert response.status_code == 200
    assert response.json()["status"] == "downloading"
    assert not movie_file.exists()
    assert (_CULPRIT, True) in qbt.removed

    async with sessionmaker_() as session:
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
        downloads = (await session.execute(select(Download))).scalars().all()
    assert len(blocklist) == 1
    assert {d.torrent_hash for d in downloads if d.status != "imported"} == {_ALT}


async def test_report_issue_endpoint_purges_anime_content_under_the_anime_root(
    app: FastAPI,
    client: httpx.AsyncClient,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    tmp_path: Path,
) -> None:
    """ADR-0015: an anime request's ``library_path`` lives under
    ``anime_movie_root``, not ``movies_root``. The endpoint must build both the
    mount-check root AND the delete-guard's filesystem from the ANIME root, or
    the purge is silently refused (the bad file stays on disk) even though
    blocklist + re-search report success."""
    await seed(initialized=True, app_api_key=_API_KEY)
    movies_root = tmp_path / "movies"
    movies_root.mkdir()
    anime_root = tmp_path / "anime-movies"
    anime_root.mkdir()
    movie_file = anime_root / "Some Movie (2020).mkv"
    movie_file.write_bytes(b"x" * 4096)
    await _set_setting(sessionmaker_, "movies_root", str(movies_root))
    await _set_setting(sessionmaker_, "anime_movie_root", str(anime_root))
    request_id = await _seed_available_movie(
        sessionmaker_, library_path=str(movie_file), is_anime=True
    )

    qbt = FakeQbittorrent()
    override_adapters(
        app,
        library=FakeLibrary(),
        qbt=qbt,
        prowlarr=FakeProwlarr(
            [
                candidate("Some.Movie.2020.1080p.BluRay.x264-GROUP", info_hash=_CULPRIT),
                candidate("Some.Movie.2020.1080p.WEB-DL.x264-OTHER", info_hash=_ALT),
            ]
        ),
    )

    response = await client.post(
        f"/api/v1/requests/{request_id}/report-issue",
        json={"reason": "bad_quality"},
        headers=_HEADERS,
    )
    assert response.status_code == 200
    assert response.json()["status"] == "downloading"
    # The anime file was actually purged -- not silently refused by a guard that
    # only knew about movies_root.
    assert not movie_file.exists()
    assert (_CULPRIT, True) in qbt.removed


async def test_report_issue_endpoint_404_for_unknown_request(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(app, library=FakeLibrary(), qbt=FakeQbittorrent(), prowlarr=FakeProwlarr([]))
    response = await client.post(
        "/api/v1/requests/999/report-issue", json={"reason": "bad_quality"}, headers=_HEADERS
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "request_not_found"


async def test_report_issue_endpoint_409_for_not_reportable_state(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=_TMDB,
            media_type=MediaType.movie,
            title="Some Movie",
            status=RequestStatus.searching,
        )
        session.add(request)
        await session.commit()
        request_id = request.id
    override_adapters(app, library=FakeLibrary(), qbt=FakeQbittorrent(), prowlarr=FakeProwlarr([]))

    response = await client.post(
        f"/api/v1/requests/{request_id}/report-issue",
        json={"reason": "bad_quality"},
        headers=_HEADERS,
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "not_reportable"


async def test_report_issue_endpoint_422_for_bad_reason(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=_TMDB,
            media_type=MediaType.movie,
            title="Some Movie",
            status=RequestStatus.available,
        )
        session.add(request)
        await session.commit()
        request_id = request.id
    override_adapters(app, library=FakeLibrary(), qbt=FakeQbittorrent(), prowlarr=FakeProwlarr([]))

    # 'failed' is auto-only, not an operator-choosable reason -> pydantic 422.
    response = await client.post(
        f"/api/v1/requests/{request_id}/report-issue",
        json={"reason": "failed"},
        headers=_HEADERS,
    )
    assert response.status_code == 422


async def test_cancel_endpoint_settles_cancelled(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=_TMDB,
            media_type=MediaType.movie,
            title="Some Movie",
            status=RequestStatus.downloading,
        )
        session.add(request)
        await session.flush()
        session.add(
            Download(
                torrent_hash=_CULPRIT,
                status="downloading",
                media_request_id=request.id,
                tmdb_id=_TMDB,
            )
        )
        await session.commit()
        request_id = request.id

    qbt = FakeQbittorrent()
    override_adapters(app, qbt=qbt)
    response = await client.post(f"/api/v1/requests/{request_id}/cancel", headers=_HEADERS)
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert (_CULPRIT, True) in qbt.removed


async def test_cancel_endpoint_409_for_imported_request(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=_TMDB,
            media_type=MediaType.movie,
            title="Some Movie",
            status=RequestStatus.available,
        )
        session.add(request)
        await session.commit()
        request_id = request.id

    override_adapters(app, qbt=FakeQbittorrent())
    response = await client.post(f"/api/v1/requests/{request_id}/cancel", headers=_HEADERS)
    assert response.status_code == 409
    assert response.json()["detail"] == "not_cancellable"


async def test_cancel_endpoint_409_while_import_is_finalizing(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    # A download mid-import (`importing`) under a request still reading `downloading`:
    # the endpoint maps the refusal to a retryable 409 import_in_progress, never a 500,
    # and the row is left untouched.
    await seed(initialized=True, app_api_key=_API_KEY)
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=_TMDB,
            media_type=MediaType.movie,
            title="Some Movie",
            status=RequestStatus.downloading,
        )
        session.add(request)
        await session.flush()
        session.add(
            Download(
                torrent_hash=_CULPRIT,
                status="importing",
                media_request_id=request.id,
                tmdb_id=_TMDB,
            )
        )
        await session.commit()
        request_id = request.id

    qbt = FakeQbittorrent()
    override_adapters(app, qbt=qbt)
    response = await client.post(f"/api/v1/requests/{request_id}/cancel", headers=_HEADERS)
    assert response.status_code == 409
    assert response.json()["detail"] == "import_in_progress"
    assert qbt.removed == []


async def test_cancel_endpoint_settles_without_qbittorrent_when_no_active_rows(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    # Finding #1: a cancel for a not-yet-imported request with NO active download rows
    # is a pure DB settle -- it never touches qBittorrent -- so it must succeed even with
    # the client UNCONFIGURED. qbt is intentionally NOT overridden (get_qbittorrent_optional
    # resolves to None), and the endpoint must NOT 409 service_not_configured.
    await seed(initialized=True, app_api_key=_API_KEY)
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=_TMDB,
            media_type=MediaType.movie,
            title="Some Movie",
            status=RequestStatus.searching,
        )
        session.add(request)
        await session.commit()
        request_id = request.id

    response = await client.post(f"/api/v1/requests/{request_id}/cancel", headers=_HEADERS)
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"


async def test_cancel_endpoint_409_service_not_configured_with_active_torrent(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    # Finding #1's honest counterpart: a cancel that owns an ACTIVE torrent needs the
    # client to remove it. With qBittorrent unconfigured (qbt NOT overridden -> None), the
    # endpoint refuses up front with 409 service_not_configured -- never a silent skip --
    # and settles/removes nothing.
    await seed(initialized=True, app_api_key=_API_KEY)
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=_TMDB,
            media_type=MediaType.movie,
            title="Some Movie",
            status=RequestStatus.downloading,
        )
        session.add(request)
        await session.flush()
        session.add(
            Download(
                torrent_hash=_CULPRIT,
                status="downloading",
                media_request_id=request.id,
                tmdb_id=_TMDB,
            )
        )
        await session.commit()
        request_id = request.id

    response = await client.post(f"/api/v1/requests/{request_id}/cancel", headers=_HEADERS)
    assert response.status_code == 409
    assert response.json()["detail"] == "service_not_configured"
    assert response.json()["service"] == "qbittorrent"
    # Nothing settled: the request is still downloading.
    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, request_id)
    assert row is not None and row.status == RequestStatus.downloading


async def test_report_issue_endpoint_409_when_an_active_sibling_exists(
    app: FastAPI,
    client: httpx.AsyncClient,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    tmp_path: Path,
) -> None:
    # An older settled `available` request + a newer active one for the same media:
    # report-issue on the settled row is refused up front (409 active_duplicate), with
    # nothing purged/blocklisted.
    await seed(initialized=True, app_api_key=_API_KEY)
    root = tmp_path / "movies"
    root.mkdir()
    movie_file = root / "Some Movie (2020).mkv"
    movie_file.write_bytes(b"x" * 4096)
    await _set_setting(sessionmaker_, "movies_root", str(root))
    settled_id = await _seed_available_movie(sessionmaker_, library_path=str(movie_file))
    async with sessionmaker_() as session:
        session.add(
            MediaRequest(
                tmdb_id=_TMDB,
                media_type=MediaType.movie,
                title="Some Movie",
                year=2020,
                status=RequestStatus.searching,
            )
        )
        await session.commit()

    qbt = FakeQbittorrent()
    override_adapters(
        app,
        library=FakeLibrary(),
        qbt=qbt,
        prowlarr=FakeProwlarr([candidate("Some.Movie.2020.1080p.WEB-DL.x264", info_hash=_ALT)]),
    )
    response = await client.post(
        f"/api/v1/requests/{settled_id}/report-issue",
        json={"reason": "bad_quality"},
        headers=_HEADERS,
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "active_duplicate"
    assert movie_file.exists()
    assert qbt.removed == []
