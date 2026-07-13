"""Correction endpoints (ADR-0014): POST /requests/{id}/report-issue and /cancel.

Focuses on routing, dependency wiring, and HTTP error mapping -- the deep flow
is covered by ``tests/services/test_correction_service.py``. Uses a real on-disk
``movies_root`` setting + file so the endpoint's ``get_eviction_filesystem`` purge
runs against a genuine root-guarded filesystem.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.models import (
    AuthSession,
    Blocklist,
    Download,
    DownloadHistory,
    DownloadHistoryEvent,
    MediaRequest,
    MediaType,
    RequestStatus,
    Setting,
    User,
)
from plex_manager.web.deps import get_downloads_host_root, hash_session_token
from tests.web.fakes import FakeLibrary, FakeProwlarr, FakeQbittorrent, candidate, override_adapters

SeedFn = Callable[..., Awaitable[None]]
SessionMaker = async_sessionmaker[AsyncSession]

_API_KEY = "correction-key"
_HEADERS = {"X-Api-Key": _API_KEY}
_TMDB = 603
_CULPRIT = "3" * 40
_ALT = "a" * 40


async def _creator_session(app: FastAPI, *, tag: str) -> tuple[int, dict[str, str], dict[str, str]]:
    token = f"{tag}-session-token"
    csrf = f"{tag}-csrf-token"
    async with app.state.sessionmaker() as session:
        user = User(username=f"{tag}-user", permissions=0)
        session.add(user)
        await session.flush()
        user_id = user.id
        session.add(
            AuthSession(
                user_id=user_id,
                token_hash=hash_session_token(token),
                expires_at=datetime.now(UTC) + timedelta(days=1),
                last_seen_at=datetime.now(UTC),
            )
        )
        await session.commit()
    return user_id, {"plexmgr.session": token, "plexmgr.csrf": csrf}, {"X-CSRF-Token": csrf}


async def _set_setting(sm: SessionMaker, key: str, value: str) -> None:
    async with sm() as session:
        session.add(Setting(key=key, value=value))
        await session.commit()


async def _seed_available_movie(
    sm: SessionMaker,
    *,
    library_path: str,
    is_anime: bool = False,
    user_id: int | None = None,
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
            user_id=user_id,
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
    user_id, cookies, headers = await _creator_session(app, tag="report-creator")
    root = tmp_path / "movies"
    root.mkdir()
    movie_file = root / "Some Movie (2020).mkv"
    movie_file.write_bytes(b"x" * 4096)
    await _set_setting(sessionmaker_, "movies_root", str(root))
    request_id = await _seed_available_movie(
        sessionmaker_, library_path=str(movie_file), user_id=user_id
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
    app.dependency_overrides[get_downloads_host_root] = lambda: "/home/lunchbox/Downloads"

    response = await client.post(
        f"/api/v1/requests/{request_id}/report-issue",
        json={"reason": "bad_quality"},
        cookies=cookies,
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["status"] == "downloading"
    assert response.json()["can_mutate"] is True
    assert not movie_file.exists()
    assert (_CULPRIT, True) in qbt.removed

    async with sessionmaker_() as session:
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
        downloads = (await session.execute(select(Download))).scalars().all()
    assert len(blocklist) == 1
    assert {d.torrent_hash for d in downloads if d.status != "imported"} == {_ALT}
    # Issues #133/#157: the inline re-grab directs the replacement torrent at
    # the derived HOST-namespace downloads root, not qBittorrent's own default.
    assert len(qbt.added) == 1
    _source, save_path, _category = qbt.added[0]
    assert save_path == "/home/lunchbox/Downloads"


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


async def test_report_issue_endpoint_purges_legacy_anime_under_movies_root(
    app: FastAPI,
    client: httpx.AsyncClient,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    tmp_path: Path,
) -> None:
    """FINDING 1 (a): an anime title imported BEFORE an anime root was configured has
    its ``library_path`` under ``movies_root``. With an anime root NOW configured but
    still EMPTY, the old is_anime-based mount check verified that empty anime root and
    spuriously 409'd. The endpoint now hands the service the full root set and the
    failsafe derives the check-root from the breadcrumb -> movies_root (mounted) -> the
    purge + re-grab proceed against the real file, no spurious media_root_unavailable."""
    await seed(initialized=True, app_api_key=_API_KEY)
    movies_root = tmp_path / "movies"
    movies_root.mkdir()
    anime_root = tmp_path / "anime-movies"
    anime_root.mkdir()  # configured but EMPTY (a freshly-added anime root)
    movie_file = movies_root / "Some Anime Movie (2020).mkv"
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
            [candidate("Some.Movie.2020.1080p.WEB-DL.x264-OTHER", info_hash=_ALT)]
        ),
    )

    response = await client.post(
        f"/api/v1/requests/{request_id}/report-issue",
        json={"reason": "bad_quality"},
        headers=_HEADERS,
    )
    # No spurious 409 against the empty anime root: the breadcrumb's REAL root
    # (movies_root) is mounted, so the correction runs.
    assert response.status_code == 200
    assert response.json()["status"] == "downloading"
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
    user_id, cookies, headers = await _creator_session(app, tag="cancel-creator")
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=_TMDB,
            media_type=MediaType.movie,
            title="Some Movie",
            status=RequestStatus.downloading,
            user_id=user_id,
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
    response = await client.post(
        f"/api/v1/requests/{request_id}/cancel", cookies=cookies, headers=headers
    )
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert response.json()["can_mutate"] is True
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


async def test_report_issue_endpoint_409_media_root_unavailable_is_actionable(
    app: FastAPI,
    client: httpx.AsyncClient,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    tmp_path: Path,
) -> None:
    # A configured-but-EMPTY movies_root reads as "not mounted" (_root_is_mounted) --
    # the Radarr-style unmounted-drive failsafe. The 409 must be an actionable
    # envelope (message + hint + diagnostics.root), never a bare code.
    await seed(initialized=True, app_api_key=_API_KEY)
    root = tmp_path / "movies"
    root.mkdir()  # configured, but EMPTY -- reads as "not mounted"
    movie_file = root / "Some Movie (2020).mkv"
    await _set_setting(sessionmaker_, "movies_root", str(root))
    request_id = await _seed_available_movie(sessionmaker_, library_path=str(movie_file))

    override_adapters(app, library=FakeLibrary(), qbt=FakeQbittorrent(), prowlarr=FakeProwlarr([]))
    response = await client.post(
        f"/api/v1/requests/{request_id}/report-issue",
        json={"reason": "bad_quality"},
        headers=_HEADERS,
    )

    assert response.status_code == 409
    body = response.json()
    assert body["detail"] == "media_root_unavailable"
    assert body["message"]  # non-empty, operator-facing
    assert body["hint"]  # non-empty, actionable next step
    assert body["diagnostics"]["root"] == str(root)


async def test_creator_media_root_error_does_not_expose_absolute_path(
    app: FastAPI,
    client: httpx.AsyncClient,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    tmp_path: Path,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    user_id, cookies, headers = await _creator_session(app, tag="root-error-creator")
    root = tmp_path / "private-movies"
    root.mkdir()
    movie_file = root / "Some Movie (2020).mkv"
    await _set_setting(sessionmaker_, "movies_root", str(root))
    request_id = await _seed_available_movie(
        sessionmaker_, library_path=str(movie_file), user_id=user_id
    )
    override_adapters(app, library=FakeLibrary(), qbt=FakeQbittorrent(), prowlarr=FakeProwlarr([]))

    response = await client.post(
        f"/api/v1/requests/{request_id}/report-issue",
        json={"reason": "bad_quality"},
        cookies=cookies,
        headers=headers,
    )
    assert response.status_code == 409
    body = response.json()
    assert body["detail"] == "media_root_unavailable"
    assert body.get("diagnostics") is None
    assert str(root) not in response.text


async def test_report_issue_endpoint_presence_only_no_culprit_reacquires(
    app: FastAPI,
    client: httpx.AsyncClient,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    tmp_path: Path,
) -> None:
    # Issue #131, endpoint-level twin of the service test: a purely
    # presence-derived row (no library_path breadcrumb, no culprit download) must
    # NOT 409 `media_root_unavailable` on an unmounted `movies_root` -- there is
    # nothing of ours to protect. Contrast with the test above, whose row has a
    # breadcrumb (via ``_seed_available_movie``) and correctly still 409s.
    await seed(initialized=True, app_api_key=_API_KEY)
    root = tmp_path / "movies"  # never created -> unmounted
    await _set_setting(sessionmaker_, "movies_root", str(root))
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=_TMDB,
            media_type=MediaType.movie,
            title="Some Movie",
            year=2020,
            status=RequestStatus.available,
            library_path=None,
        )
        session.add(request)
        await session.commit()
        request_id = request.id

    override_adapters(
        app,
        library=FakeLibrary(),
        qbt=FakeQbittorrent(),
        prowlarr=FakeProwlarr(
            [candidate("Some.Movie.2020.1080p.WEB-DL.x264-OTHER", info_hash=_ALT)]
        ),
    )
    response = await client.post(
        f"/api/v1/requests/{request_id}/report-issue",
        json={"reason": "bad_quality"},
        headers=_HEADERS,
    )

    assert response.status_code == 200
    assert response.json()["status"] == "downloading"


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
