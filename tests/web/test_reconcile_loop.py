"""The background reconcile pass must survive a qBittorrent outage (F5).

``_reconcile_once`` runs the qBittorrent-dependent reconcile + import drain and
THEN the Plex-only availability promotion. A qBittorrent outage —
``get_all_statuses`` raising ``QbittorrentError`` (or its ``QbittorrentAuthError``
subclass) — must NOT abort the cycle before the availability pass, or an
already-imported title stays stuck in "Finalizing" (``completed``) until the
download client recovers. These deps are called directly (not via FastAPI's
dependency injection), so they are monkeypatched on the app module.
"""

from __future__ import annotations

import logging

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.adapters.qbittorrent import QbittorrentAuthError, QbittorrentError
from plex_manager.domain.state_machine import DownloadState
from plex_manager.models import Download, MediaRequest, MediaType, RequestStatus
from plex_manager.ports.download_client import DownloadClientPort, DownloadStatus
from plex_manager.ports.library import LibraryPort
from plex_manager.web import app as app_module
from plex_manager.web.deps import ServiceNotConfiguredError
from tests.web.fakes import FakeLibrary, FakeQbittorrent

SessionMaker = async_sessionmaker[AsyncSession]

_TMDB_ID = 603


class _OutageQbittorrent(FakeQbittorrent):
    """A qBittorrent whose status poll fails like a real outage / auth rejection."""

    def __init__(self, exc: QbittorrentError) -> None:
        super().__init__()
        self._exc = exc

    async def get_all_statuses(self, category: str | None = None) -> list[DownloadStatus]:
        raise self._exc


async def _seed_finalizing(sessionmaker_: SessionMaker) -> int:
    """Insert an imported movie whose request is 'completed' ("Finalizing")."""
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=_TMDB_ID,
            media_type=MediaType.movie,
            title="The Matrix",
            year=1999,
            status=RequestStatus.completed,
        )
        session.add(request)
        await session.flush()
        session.add(
            Download(
                torrent_hash="deadbeef01",
                status=DownloadState.Imported.value,
                media_request_id=request.id,
                tmdb_id=_TMDB_ID,
                year=1999,
            )
        )
        await session.commit()
        return request.id


@pytest.mark.parametrize(
    "outage",
    [
        QbittorrentError("qBittorrent request failed"),
        QbittorrentAuthError("qBittorrent rejected the login (HTTP 403)"),
    ],
    ids=["outage", "auth_failure"],
)
async def test_reconcile_runs_availability_when_qbittorrent_is_down(
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    outage: QbittorrentError,
) -> None:
    request_id = await _seed_finalizing(sessionmaker_)
    library = FakeLibrary(available={_TMDB_ID})

    app = FastAPI()
    app.state.sessionmaker = sessionmaker_
    app.state.http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, text="ok"))
    )

    async def _qbt(_session: AsyncSession, _client: httpx.AsyncClient) -> DownloadClientPort:
        return _OutageQbittorrent(outage)

    async def _library(_session: AsyncSession, _client: httpx.AsyncClient) -> LibraryPort | None:
        return library

    async def _movies_root(_session: AsyncSession) -> str | None:
        return None  # the import drain is irrelevant; the status poll fails first

    monkeypatch.setattr(app_module, "get_qbittorrent", _qbt)
    monkeypatch.setattr(app_module, "get_library_optional", _library)
    monkeypatch.setattr(app_module, "get_movies_root_optional", _movies_root)

    try:
        with caplog.at_level(logging.WARNING, logger="plex_manager.web.app"):
            await app_module._reconcile_once(app)  # pyright: ignore[reportPrivateUsage]
    finally:
        await app.state.http_client.aclose()

    # The outage did NOT abort the cycle: the Plex-only availability pass still
    # promoted the imported title (no request left stuck in "Finalizing").
    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        assert request is not None
        assert request.status == RequestStatus.available

    # The outage was surfaced, not swallowed (honesty over silence): the log names
    # the exception TYPE only — never a url, username, password or session id.
    assert type(outage).__name__ in caplog.text


async def test_reconcile_imports_when_only_movies_root_unset(
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard for the old ``if library and movies_root:`` gate, which
    skipped the WHOLE import cycle (including any tv rows) whenever movies_root
    was unset. The cycle must now run unconditionally once Plex is configured,
    so a movie download reaching the drain with its OWN root unset gets an
    honest, retryable ImportBlocked -- never left forgotten in ImportPending."""
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=_TMDB_ID,
            media_type=MediaType.movie,
            title="The Matrix",
            year=1999,
            status=RequestStatus.downloading,
        )
        session.add(request)
        await session.flush()
        download = Download(
            torrent_hash="deadbeef02",
            status=DownloadState.ImportPending.value,
            media_request_id=request.id,
            tmdb_id=_TMDB_ID,
            year=1999,
        )
        session.add(download)
        await session.commit()
        download_id, request_id = download.id, request.id

    library = FakeLibrary()
    # A matching client snapshot -- so the reconcile pass that runs BEFORE the
    # import drain sees the torrent as present (still seeding) and leaves it
    # ImportPending, rather than surfacing the unrelated ClientMissing this test
    # isn't about.
    qbt = FakeQbittorrent(
        statuses=[
            DownloadStatus(
                info_hash="deadbeef02", name="The.Matrix.1999.mkv", raw_state="stalledUP"
            )
        ]
    )

    app = FastAPI()
    app.state.sessionmaker = sessionmaker_
    app.state.http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, text="ok"))
    )

    async def _qbt(_session: AsyncSession, _client: httpx.AsyncClient) -> DownloadClientPort:
        return qbt

    async def _library(_session: AsyncSession, _client: httpx.AsyncClient) -> LibraryPort | None:
        return library

    monkeypatch.setattr(app_module, "get_qbittorrent", _qbt)
    monkeypatch.setattr(app_module, "get_library_optional", _library)
    # movies_root / tv_root are left to the REAL (optional) dependency, reading
    # from a settings store where neither was ever configured -- both resolve to
    # None naturally, no monkeypatch needed to prove the "unset" case.

    try:
        await app_module._reconcile_once(app)  # pyright: ignore[reportPrivateUsage]
    finally:
        await app.state.http_client.aclose()

    async with sessionmaker_() as session:
        download = await session.get(Download, download_id)
        request = await session.get(MediaRequest, request_id)
    assert download is not None
    assert download.status == DownloadState.ImportBlocked.value
    assert download.failed_reason == "movies library root is not configured"
    assert request is not None
    assert request.status == RequestStatus.import_blocked


async def test_reconcile_once_heals_db_only_strand_when_qbt_unconfigured(
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With qBittorrent UNCONFIGURED the cycle cannot reconcile at all, but a
    remove=no operator residual (mark_failed with remove_torrent=False -- the
    exact flow such installs rely on) needs NO client I/O, so ``_reconcile_once``
    must still heal it via the DB-only branch: download Failed, request re-armed,
    the operator's no-blocklist choice honored."""
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=_TMDB_ID,
            media_type=MediaType.movie,
            title="The Matrix",
            year=1999,
            status=RequestStatus.downloading,
        )
        session.add(request)
        await session.flush()
        download = Download(
            torrent_hash="deadbeef03",
            status=DownloadState.FailedPending.value,
            media_request_id=request.id,
            tmdb_id=_TMDB_ID,
            year=1999,
            failed_reason="operator mark-failed in progress (blocklist=no, remove=no, nonce=902)",
        )
        session.add(download)
        await session.commit()
        download_id, request_id = download.id, request.id

    app = FastAPI()
    app.state.sessionmaker = sessionmaker_
    app.state.http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, text="ok"))
    )

    async def _no_qbt(_session: AsyncSession, _client: httpx.AsyncClient) -> DownloadClientPort:
        raise ServiceNotConfiguredError("qbittorrent")

    async def _no_library(_session: AsyncSession, _client: httpx.AsyncClient) -> LibraryPort | None:
        return None

    monkeypatch.setattr(app_module, "get_qbittorrent", _no_qbt)
    monkeypatch.setattr(app_module, "get_library_optional", _no_library)

    try:
        await app_module._reconcile_once(app)  # pyright: ignore[reportPrivateUsage]
    finally:
        await app.state.http_client.aclose()

    async with sessionmaker_() as session:
        download = await session.get(Download, download_id)
        request = await session.get(MediaRequest, request_id)
    assert download is not None
    assert download.status == DownloadState.Failed.value  # healed without a client
    assert download.failed_reason == "marked failed by operator"
    assert request is not None
    assert request.status == RequestStatus.searching  # re-armed


async def test_reconcile_outage_tick_still_heals_db_only_strand(
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 6: a qBittorrent OUTAGE (configured client, status poll raising
    QbittorrentError) must not strand a remove=no operator residual for the
    outage's whole duration -- its heal needs no client I/O, so the outage branch
    runs the same narrow DB-only heal the unconfigured branch does, on the same
    tick."""
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=_TMDB_ID,
            media_type=MediaType.movie,
            title="The Matrix",
            year=1999,
            status=RequestStatus.downloading,
        )
        session.add(request)
        await session.flush()
        download = Download(
            torrent_hash="deadbeef04",
            status=DownloadState.FailedPending.value,
            media_request_id=request.id,
            tmdb_id=_TMDB_ID,
            year=1999,
            failed_reason="operator mark-failed in progress (blocklist=no, remove=no, nonce=902)",
        )
        session.add(download)
        await session.commit()
        download_id, request_id = download.id, request.id

    app = FastAPI()
    app.state.sessionmaker = sessionmaker_
    app.state.http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, text="ok"))
    )

    async def _qbt(_session: AsyncSession, _client: httpx.AsyncClient) -> DownloadClientPort:
        return _OutageQbittorrent(QbittorrentError("qBittorrent request failed"))

    async def _no_library(_session: AsyncSession, _client: httpx.AsyncClient) -> LibraryPort | None:
        return None

    monkeypatch.setattr(app_module, "get_qbittorrent", _qbt)
    monkeypatch.setattr(app_module, "get_library_optional", _no_library)

    try:
        await app_module._reconcile_once(app)  # pyright: ignore[reportPrivateUsage]
    finally:
        await app.state.http_client.aclose()

    async with sessionmaker_() as session:
        download = await session.get(Download, download_id)
        request = await session.get(MediaRequest, request_id)
    assert download is not None
    assert download.status == DownloadState.Failed.value  # healed despite the outage
    assert download.failed_reason == "marked failed by operator"
    assert request is not None
    assert request.status == RequestStatus.searching  # re-armed on the outage tick


async def test_reconcile_idle_cycle_does_not_publish_realtime_event(
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An entirely idle background tick must not invalidate frontend caches."""
    app = FastAPI()
    app.state.sessionmaker = sessionmaker_
    app.state.http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, text="ok"))
    )
    qbt = FakeQbittorrent()
    library = FakeLibrary()
    phases: list[str] = []
    published: list[tuple[tuple[str, ...], str]] = []

    async def _qbt(_session: AsyncSession, _client: httpx.AsyncClient) -> DownloadClientPort:
        return qbt

    async def _library(_session: AsyncSession, _client: httpx.AsyncClient) -> LibraryPort | None:
        return library

    async def _root(_session: AsyncSession) -> str | None:
        return None

    async def _reconcile(
        _qbt: DownloadClientPort,
        _session: AsyncSession,
        *,
        changes: app_module.queue_service.ReconcileChanges | None = None,
    ) -> list[object]:
        assert changes is not None
        phases.append("reconcile")
        return []

    async def _import(**_kwargs: object) -> int:
        phases.append("import")
        return 0

    async def _availability(**_kwargs: object) -> int:
        phases.append("availability")
        return 0

    def _publish(_app: FastAPI, topics: tuple[str, ...], *, reason: str) -> None:
        published.append((topics, reason))

    monkeypatch.setattr(app_module, "get_qbittorrent", _qbt)
    monkeypatch.setattr(app_module, "get_library_optional", _library)
    monkeypatch.setattr(app_module, "get_movies_root_optional", _root)
    monkeypatch.setattr(app_module, "get_tv_root_optional", _root)
    monkeypatch.setattr(app_module, "get_anime_movie_root_optional", _root)
    monkeypatch.setattr(app_module, "get_anime_tv_root_optional", _root)
    monkeypatch.setattr(app_module.queue_service, "reconcile_and_list", _reconcile)
    monkeypatch.setattr(app_module.import_service, "run_import_cycle", _import)
    monkeypatch.setattr(app_module.import_service, "run_availability_cycle", _availability)
    monkeypatch.setattr(app_module, "publish_realtime", _publish)

    try:
        await app_module._reconcile_once(app)  # pyright: ignore[reportPrivateUsage]
    finally:
        await app.state.http_client.aclose()

    assert phases == ["reconcile", "import", "availability"]
    assert published == []


async def test_reconcile_coalesces_all_reported_changes_into_one_realtime_event(
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reconcile/import/availability changes share one ordered invalidation."""
    app = FastAPI()
    app.state.sessionmaker = sessionmaker_
    app.state.http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, text="ok"))
    )
    qbt = FakeQbittorrent()
    library = FakeLibrary()
    published: list[tuple[tuple[str, ...], str]] = []

    async def _qbt(_session: AsyncSession, _client: httpx.AsyncClient) -> DownloadClientPort:
        return qbt

    async def _library(_session: AsyncSession, _client: httpx.AsyncClient) -> LibraryPort | None:
        return library

    async def _root(_session: AsyncSession) -> str | None:
        return None

    async def _reconcile(
        _qbt: DownloadClientPort,
        _session: AsyncSession,
        *,
        changes: app_module.queue_service.ReconcileChanges | None = None,
    ) -> list[object]:
        assert changes is not None
        changes.queue = True
        changes.requests = True
        changes.blocklist = True
        return []

    async def _import(**_kwargs: object) -> int:
        return 2

    async def _availability(**_kwargs: object) -> int:
        return 1

    def _publish(_app: FastAPI, topics: tuple[str, ...], *, reason: str) -> None:
        published.append((topics, reason))

    monkeypatch.setattr(app_module, "get_qbittorrent", _qbt)
    monkeypatch.setattr(app_module, "get_library_optional", _library)
    monkeypatch.setattr(app_module, "get_movies_root_optional", _root)
    monkeypatch.setattr(app_module, "get_tv_root_optional", _root)
    monkeypatch.setattr(app_module, "get_anime_movie_root_optional", _root)
    monkeypatch.setattr(app_module, "get_anime_tv_root_optional", _root)
    monkeypatch.setattr(app_module.queue_service, "reconcile_and_list", _reconcile)
    monkeypatch.setattr(app_module.import_service, "run_import_cycle", _import)
    monkeypatch.setattr(app_module.import_service, "run_availability_cycle", _availability)
    monkeypatch.setattr(app_module, "publish_realtime", _publish)

    try:
        await app_module._reconcile_once(app)  # pyright: ignore[reportPrivateUsage]
    finally:
        await app.state.http_client.aclose()

    assert published == [(("queue", "requests", "blocklist", "discover"), "reconcile_cycle")]
