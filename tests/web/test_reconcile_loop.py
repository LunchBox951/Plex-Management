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
