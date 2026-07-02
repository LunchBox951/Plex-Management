"""The background auto-grab pass (ADR-0013) turns a pending request into a grab,
respects the master toggle, and records a Prowlarr outage on its health signal.

``_autograb_once`` resolves its adapters directly (not via FastAPI DI), so they are
monkeypatched on the app module -- mirroring ``tests/web/test_reconcile_loop.py``.
"""

from __future__ import annotations

import logging

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.adapters.prowlarr import IndexerError
from plex_manager.models import Download, MediaRequest, MediaType, RequestStatus
from plex_manager.ports.download_client import DownloadClientPort
from plex_manager.ports.indexer import IndexerPort
from plex_manager.services.health_service import AutograbStatus
from plex_manager.web import app as app_module
from tests.web.fakes import (
    FakeProwlarr,
    FakeQbittorrent,
    good_and_cam_candidates,
)

SessionMaker = async_sessionmaker[AsyncSession]

_TMDB_ID = 603


class _RaisingProwlarr(FakeProwlarr):
    """A Prowlarr whose search fails like a real outage / rate-limit."""

    async def search(self, request: object) -> list:  # type: ignore[override]
        raise IndexerError("prowlarr unreachable")


async def _seed_pending_movie(sessionmaker_: SessionMaker) -> int:
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=_TMDB_ID,
            media_type=MediaType.movie,
            title="Some Movie",
            year=2020,
            status=RequestStatus.pending,
        )
        session.add(request)
        await session.commit()
        return request.id


def _build_app(sessionmaker_: SessionMaker) -> FastAPI:
    app = FastAPI()
    app.state.sessionmaker = sessionmaker_
    app.state.http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, text="ok"))
    )
    app.state.autograb_status = AutograbStatus()
    return app


def _patch_adapters(
    monkeypatch: pytest.MonkeyPatch,
    *,
    prowlarr: IndexerPort,
    qbt: DownloadClientPort,
    enabled: bool,
) -> None:
    async def _prowlarr(_session: AsyncSession, _client: httpx.AsyncClient) -> IndexerPort:
        return prowlarr

    async def _qbt(_session: AsyncSession, _client: httpx.AsyncClient) -> DownloadClientPort:
        return qbt

    async def _enabled(_session: AsyncSession) -> bool:
        return enabled

    monkeypatch.setattr(app_module, "get_prowlarr", _prowlarr)
    monkeypatch.setattr(app_module, "get_qbittorrent", _qbt)
    monkeypatch.setattr(app_module, "get_auto_grab_enabled", _enabled)


async def test_autograb_grabs_a_pending_request(
    sessionmaker_: SessionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    request_id = await _seed_pending_movie(sessionmaker_)
    qbt = FakeQbittorrent()
    _patch_adapters(
        monkeypatch, prowlarr=FakeProwlarr(good_and_cam_candidates()), qbt=qbt, enabled=True
    )

    app = _build_app(sessionmaker_)
    try:
        await app_module._autograb_once(app)  # pyright: ignore[reportPrivateUsage]
    finally:
        await app.state.http_client.aclose()

    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        assert request is not None
        assert request.status == RequestStatus.downloading
    # A clean cycle stamps the health signal ok, no error.
    status = app.state.autograb_status
    assert status.last_ok_at is not None
    assert status.last_error_type is None


async def test_autograb_disabled_toggle_is_a_clean_no_op(
    sessionmaker_: SessionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    request_id = await _seed_pending_movie(sessionmaker_)
    prowlarr = FakeProwlarr(good_and_cam_candidates())
    _patch_adapters(monkeypatch, prowlarr=prowlarr, qbt=FakeQbittorrent(), enabled=False)

    app = _build_app(sessionmaker_)
    try:
        await app_module._autograb_once(app)  # pyright: ignore[reportPrivateUsage]
    finally:
        await app.state.http_client.aclose()

    # The master switch is OFF: nothing searched, nothing grabbed, request untouched.
    assert prowlarr.searched == []
    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        assert request is not None
        assert request.status == RequestStatus.pending
    # Disabled is a healthy no-op, never an error.
    status = app.state.autograb_status
    assert status.last_ok_at is not None
    assert status.last_error_type is None


async def test_autograb_records_prowlarr_outage_on_health(
    sessionmaker_: SessionMaker, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    request_id = await _seed_pending_movie(sessionmaker_)
    _patch_adapters(monkeypatch, prowlarr=_RaisingProwlarr(), qbt=FakeQbittorrent(), enabled=True)

    app = _build_app(sessionmaker_)
    try:
        # _autograb_once propagates; the LOOP is what records + logs. Drive one
        # loop iteration's worth of error handling exactly as _autograb_loop does.
        try:
            with caplog.at_level(logging.WARNING, logger="plex_manager.web.app"):
                await app_module._autograb_once(app)  # pyright: ignore[reportPrivateUsage]
        except Exception as exc:  # mirrors _autograb_loop's guard
            app.state.autograb_status.mark_error(exc)
    finally:
        await app.state.http_client.aclose()

    # The outage did NOT falsely park the request (honesty over silence).
    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        assert request is not None
        assert request.status == RequestStatus.pending
        assert request.next_search_at is None
        # No stray download was created.
        assert (await session.get(Download, 1)) is None
    # The failure is recorded on the health signal by TYPE only (no secret leak).
    status = app.state.autograb_status
    assert status.last_error_type == "IndexerError"
    assert status.consecutive_failures == 1
