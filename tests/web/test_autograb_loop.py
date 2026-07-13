"""The background auto-grab pass (ADR-0013) turns a pending request into a grab,
respects the master toggle, and records a Prowlarr outage on its health signal.

``_autograb_once`` resolves its adapters directly (not via FastAPI DI), so they are
monkeypatched on the app module -- mirroring ``tests/web/test_reconcile_loop.py``.
"""

from __future__ import annotations

import logging
from datetime import date

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.adapters.prowlarr import IndexerError
from plex_manager.models import Download, MediaRequest, MediaType, RequestStatus, SeasonRequest
from plex_manager.ports.download_client import DownloadClientPort
from plex_manager.ports.indexer import IndexerPort
from plex_manager.ports.library import LibraryPort
from plex_manager.ports.metadata import EpisodeInfo, MetadataPort, TvMetadata
from plex_manager.services.health_service import AutograbStatus
from plex_manager.web import app as app_module
from tests.web.fakes import (
    FakeLibrary,
    FakeProwlarr,
    FakeQbittorrent,
    FakeTmdb,
    candidate,
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
    async def _prowlarr(
        _state: object, _session: AsyncSession, _client: httpx.AsyncClient
    ) -> IndexerPort:
        return prowlarr

    async def _qbt(
        _state: object, _session: AsyncSession, _client: httpx.AsyncClient
    ) -> DownloadClientPort:
        return qbt

    async def _enabled(_session: AsyncSession) -> bool:
        return enabled

    monkeypatch.setattr(app_module, "resolve_prowlarr", _prowlarr)
    monkeypatch.setattr(app_module, "resolve_qbittorrent", _qbt)
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


async def test_autograb_records_grab_error_on_health(
    sessionmaker_: SessionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A GrabError (qBittorrent accepted the torrent but no info-hash could be
    # derived -> a live, untracked torrent) is an OPERATIONAL failure. Unlike a
    # raised search it does NOT propagate/abort: run_grab_cycle surfaces it on the
    # result and _autograb_once records it on the health signal (TYPE only) WITHOUT
    # marking the cycle clean -- so the operator sees a failing loop, never a request
    # silently parked while an orphan torrent lingers.
    request_id = await _seed_pending_movie(sessionmaker_)
    prowlarr = FakeProwlarr([candidate("Some.Movie.2020.1080p.WEB-DL.x264-GROUP", magnet=False)])
    _patch_adapters(monkeypatch, prowlarr=prowlarr, qbt=FakeQbittorrent(), enabled=True)

    app = _build_app(sessionmaker_)
    try:
        await app_module._autograb_once(app)  # pyright: ignore[reportPrivateUsage]
    finally:
        await app.state.http_client.aclose()

    # The scope was left UNCHANGED (never falsely parked).
    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        assert request is not None
        assert request.status == RequestStatus.pending
        assert request.next_search_at is None
    # The operational failure is recorded (TYPE only) and the cycle is NOT clean.
    status = app.state.autograb_status
    assert status.last_error_type == "GrabError"
    assert status.consecutive_failures == 1
    assert status.last_ok_at is None
    # The scope entered a grab-pipeline cooldown, surfaced on the health record so the
    # operator SEES the pipeline failing (ADR-0013 round-3 #2), not just a stuck request.
    assert status.cooled_down_scopes == 1


async def _seed_pending_tv_season(sessionmaker_: SessionMaker, tmdb_id: int) -> tuple[int, int]:
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=tmdb_id,
            media_type=MediaType.tv,
            title="Some Show",
            status=RequestStatus.pending,
        )
        session.add(request)
        await session.flush()
        season = SeasonRequest(media_request_id=request.id, season_number=1, status="pending")
        session.add(season)
        await session.commit()
        return request.id, season.id


async def test_autograb_wires_tmdb_into_the_episode_fallback(
    sessionmaker_: SessionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_autograb_once`` resolves TMDB best-effort and threads it into
    ``run_grab_cycle`` (ADR-0020, issue #178): with TMDB configured and no season
    pack available, a whole-season TV scope reaches the Pass-2 episode-level
    fallback and grabs the missing episode."""
    tmdb_id = 611
    request_id, season_id = await _seed_pending_tv_season(sessionmaker_, tmdb_id)
    # No season pack in the results -- Pass 1 rejects it NOT_SEASON_PACK, leaving
    # Pass 2 to run.
    prowlarr = FakeProwlarr(
        [candidate("Some.Show.S01E01.1080p.WEB-DL.x264-GROUP", info_hash="ab" * 20)]
    )
    qbt = FakeQbittorrent()
    _patch_adapters(monkeypatch, prowlarr=prowlarr, qbt=qbt, enabled=True)

    tmdb: MetadataPort = FakeTmdb(
        season_episodes={(tmdb_id, 1): [EpisodeInfo(episode_number=1, air_date=date(2020, 1, 1))]}
    )

    async def _get_tmdb(
        _state: object, _session: AsyncSession, _client: httpx.AsyncClient
    ) -> MetadataPort:
        return tmdb

    monkeypatch.setattr(app_module, "resolve_tmdb", _get_tmdb)

    app = _build_app(sessionmaker_)
    try:
        await app_module._autograb_once(app)  # pyright: ignore[reportPrivateUsage]
    finally:
        await app.state.http_client.aclose()

    async with sessionmaker_() as session:
        season = await session.get(SeasonRequest, season_id)
        assert season is not None
        assert season.status == RequestStatus.downloading
        request = await session.get(MediaRequest, request_id)
        assert request is not None
        assert request.status == RequestStatus.downloading
    status = app.state.autograb_status
    assert status.last_ok_at is not None
    assert status.last_error_type is None


async def test_autograb_unconfigured_tmdb_disables_fallback_cleanly(
    sessionmaker_: SessionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``tmdb_api_key`` setting -> ``get_tmdb`` raises ``ServiceNotConfiguredError``
    -> ``_autograb_once`` catches it and passes ``metadata=None``: Pass 1 alone,
    an honest park on nothing acceptable, no crash."""
    tmdb_id = 612
    request_id, season_id = await _seed_pending_tv_season(sessionmaker_, tmdb_id)
    prowlarr = FakeProwlarr(
        [candidate("Some.Show.S01E01.1080p.WEB-DL.x264-GROUP", info_hash="cd" * 20)]
    )
    _patch_adapters(monkeypatch, prowlarr=prowlarr, qbt=FakeQbittorrent(), enabled=True)
    # get_tmdb is NOT monkeypatched: the real deps.py function runs against a DB
    # with no tmdb_api_key setting and raises ServiceNotConfiguredError.

    app = _build_app(sessionmaker_)
    try:
        await app_module._autograb_once(app)  # pyright: ignore[reportPrivateUsage]
    finally:
        await app.state.http_client.aclose()

    async with sessionmaker_() as session:
        season = await session.get(SeasonRequest, season_id)
        assert season is not None
        assert season.status == RequestStatus.no_acceptable_release
        request = await session.get(MediaRequest, request_id)
        assert request is not None
        assert request.status == RequestStatus.no_acceptable_release
    status = app.state.autograb_status
    assert status.last_ok_at is not None
    assert status.last_error_type is None


async def _seed_waiting_tv_season(
    sessionmaker_: SessionMaker, tmdb_id: int, *, season_number: int = 2
) -> tuple[int, int]:
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=tmdb_id,
            media_type=MediaType.tv,
            title="Some Show",
            status=RequestStatus.waiting_for_air_date,
        )
        session.add(request)
        await session.flush()
        season = SeasonRequest(
            media_request_id=request.id,
            season_number=season_number,
            status=RequestStatus.waiting_for_air_date,
        )
        session.add(season)
        await session.commit()
        return request.id, season.id


async def test_autograb_once_wakes_waiting_season_end_to_end(
    sessionmaker_: SessionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loop resolves TMDB (and optionally Plex) and threads both into
    ``run_grab_cycle``'s air-date wake pre-pass (issue #210): a waiting season
    TMDB now reports aired is woken and searched/grabbed in the SAME tick, and
    the transition fires a realtime invalidation."""
    tmdb_id = 613
    request_id, season_id = await _seed_waiting_tv_season(sessionmaker_, tmdb_id)
    prowlarr = FakeProwlarr(
        [candidate("Some.Show.S02.1080p.WEB-DL.x264-GROUP", info_hash="aa" * 20)]
    )
    qbt = FakeQbittorrent()
    _patch_adapters(monkeypatch, prowlarr=prowlarr, qbt=qbt, enabled=True)

    tmdb: MetadataPort = FakeTmdb(
        shows={tmdb_id: TvMetadata(tmdb_id=tmdb_id, title="Some Show", season_count=2)}
    )

    async def _get_tmdb(
        _state: object, _session: AsyncSession, _client: httpx.AsyncClient
    ) -> MetadataPort:
        return tmdb

    monkeypatch.setattr(app_module, "resolve_tmdb", _get_tmdb)

    published: list[tuple[tuple[str, ...], str]] = []

    def _publish(_app: FastAPI, topics: tuple[str, ...], *, reason: str) -> None:
        published.append((topics, reason))

    monkeypatch.setattr(app_module, "publish_realtime", _publish)

    app = _build_app(sessionmaker_)
    try:
        await app_module._autograb_once(app)  # pyright: ignore[reportPrivateUsage]
    finally:
        await app.state.http_client.aclose()

    async with sessionmaker_() as session:
        season = await session.get(SeasonRequest, season_id)
        assert season is not None
        assert season.status != RequestStatus.waiting_for_air_date
        request = await session.get(MediaRequest, request_id)
        assert request is not None
        assert request.status != RequestStatus.waiting_for_air_date
    assert published != []
    status = app.state.autograb_status
    assert status.last_ok_at is not None
    assert status.last_error_type is None


async def test_autograb_once_publishes_on_air_date_woken_even_without_grab(
    sessionmaker_: SessionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A season woken straight to ``available`` (already in Plex) grabs nothing
    this cycle -- the ``or result.air_date_woken`` realtime-publish term must
    still fire so the frontend's stale 'waiting' view is invalidated."""
    tmdb_id = 614
    request_id, season_id = await _seed_waiting_tv_season(sessionmaker_, tmdb_id)
    prowlarr = FakeProwlarr([])
    qbt = FakeQbittorrent()
    _patch_adapters(monkeypatch, prowlarr=prowlarr, qbt=qbt, enabled=True)

    tmdb: MetadataPort = FakeTmdb(
        shows={tmdb_id: TvMetadata(tmdb_id=tmdb_id, title="Some Show", season_count=2)}
    )

    async def _get_tmdb(
        _state: object, _session: AsyncSession, _client: httpx.AsyncClient
    ) -> MetadataPort:
        return tmdb

    library: LibraryPort = FakeLibrary(available_tv_seasons={tmdb_id: frozenset({2})})

    async def _get_library(
        _session: AsyncSession, _client: httpx.AsyncClient
    ) -> LibraryPort | None:
        return library

    monkeypatch.setattr(app_module, "resolve_tmdb", _get_tmdb)
    monkeypatch.setattr(app_module, "get_library_optional", _get_library)

    published: list[tuple[tuple[str, ...], str]] = []

    def _publish(_app: FastAPI, topics: tuple[str, ...], *, reason: str) -> None:
        published.append((topics, reason))

    monkeypatch.setattr(app_module, "publish_realtime", _publish)

    app = _build_app(sessionmaker_)
    try:
        await app_module._autograb_once(app)  # pyright: ignore[reportPrivateUsage]
    finally:
        await app.state.http_client.aclose()

    assert prowlarr.searched == []
    async with sessionmaker_() as session:
        season = await session.get(SeasonRequest, season_id)
        assert season is not None
        assert season.status == RequestStatus.available
        request = await session.get(MediaRequest, request_id)
        assert request is not None
        assert request.status != RequestStatus.waiting_for_air_date
    assert published != []
    status = app.state.autograb_status
    assert status.last_ok_at is not None
    assert status.last_error_type is None
