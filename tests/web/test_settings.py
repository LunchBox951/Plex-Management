"""Settings — GET redacts secrets; PUT round-trips and stores secrets encrypted."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.config import get_settings
from plex_manager.domain.eviction import EvictionCandidate, select_evictions
from plex_manager.models import AuthSession, LogEvent, Setting, User
from plex_manager.repositories.log_events import SqlLogEventRepository
from plex_manager.services import log_capture_service, path_visibility
from plex_manager.web import deps
from plex_manager.web.deps import (
    AUTO_GRAB_ENABLED_DEFAULT,
    AUTO_GRAB_INTERVAL_SECONDS_DEFAULT,
    AUTO_GRAB_INTERVAL_SECONDS_MAX,
    AUTO_GRAB_INTERVAL_SECONDS_MIN,
    AUTO_GRAB_MAX_SEARCHES_PER_CYCLE_DEFAULT,
    AUTO_GRAB_MAX_SEARCHES_PER_CYCLE_MAX,
    AUTO_GRAB_MAX_SEARCHES_PER_CYCLE_MIN,
    DISK_PRESSURE_TARGET_PERCENT_DEFAULT,
    DISK_PRESSURE_THRESHOLD_PERCENT_DEFAULT,
    EVICTION_ENABLED_DEFAULT,
    EVICTION_GRACE_DAYS_DEFAULT,
    EVICTION_GRACE_DAYS_MAX,
    EVICTION_INTERVAL_MAX_MINUTES,
    EVICTION_INTERVAL_MINUTES_DEFAULT,
    KNOWN_SETTING_KEYS,
    LOG_MAX_ROWS_DEFAULT,
    LOG_MAX_ROWS_MAX,
    LOG_RETENTION_DAYS_DEFAULT,
    LOG_RETENTION_DAYS_MAX,
    PLEX_MACHINE_ID_SETTING,
    SECRET_SETTING_KEYS,
    WATCHLIST_SYNC_INTERVAL_MINUTES_DEFAULT,
    AuthContext,
    AuthMethod,
    SettingsStore,
    get_anime_movie_root_optional,
    get_anime_tv_root_optional,
    get_auto_grab_enabled,
    get_auto_grab_interval_seconds,
    get_auto_grab_max_searches_per_cycle,
    get_disk_pressure_target_percent,
    get_disk_pressure_threshold_percent,
    get_eviction_enabled,
    get_eviction_grace_days,
    get_eviction_interval_minutes,
    get_eviction_proactive_enabled,
    get_log_max_rows,
    get_log_retention_days,
    get_movies_root_optional,
    get_tv_root_optional,
    get_watchlist_sync_interval_minutes,
    hash_session_token,
    load_system_settings,
    require_api_key,
)
from plex_manager.web.events import get_event_hub
from plex_manager.web.routers import auth as auth_module
from plex_manager.web.routers.settings import (
    _BOOL_SETTING_DEFAULTS,  # pyright: ignore[reportPrivateUsage]
)
from plex_manager.web.schemas import SettingsResponse, SettingsUpdate
from tests.support import assert_task_raises

# The float/int typed-key groups MIRROR the explicit per-key resolver branches in
# ``_sanitize_typed_settings`` (each key has its own resolver + default, so the
# router has no generic tuple to import). The parity guard below asserts these
# groups cover every non-str ``SettingsResponse`` field -- a new typed field
# fails the guard until BOTH the sanitizer branch and this mirror are extended.
_FLOAT_TYPED_SETTING_KEYS: tuple[str, ...] = (
    "disk_pressure_threshold_percent",
    "disk_pressure_target_percent",
    "eviction_interval_minutes",
    "watchlist_sync_interval_minutes",
    "auto_grab_interval_seconds",
)
_INT_TYPED_SETTING_KEYS: tuple[str, ...] = (
    "eviction_grace_days",
    "log_retention_days",
    "log_max_rows",
    "auto_grab_max_searches_per_cycle",
)
_BOOL_TYPED_SETTING_KEYS: tuple[str, ...] = tuple(_BOOL_SETTING_DEFAULTS)
_COLLECTION_TYPED_SETTING_KEYS: tuple[str, ...] = ("automatic_update_weekdays",)

SeedFn = Callable[..., Awaitable[None]]
SessionMaker = async_sessionmaker[AsyncSession]


@pytest.fixture(autouse=True)
def reset_throttle() -> None:
    """Clear the in-process sign-in throttle so tests never leak attempt counts.

    This suite exercises ``POST /api/v1/auth/api-key``, which shares the
    module-level sign-in throttle: attempts left by another suite within the
    real 60s window would otherwise 429 the exchange here (order-dependence).
    """
    auth_module.reset_sign_in_throttle()


_API_KEY = "settings-key"
# Throwaway Plex credentials for the identity-cache/repoint tests. Held in NAMES
# (not inline keyword literals) so ruff's S106 secret-in-call heuristic stays
# quiet — fixture values, never real secrets. ``_SEED_PLEX_TOKEN`` is the stored
# SERVICE token; ``_SEED_OAUTH_TOKEN`` is a session admin's ACCOUNT OAuth token
# (what the repoint ownership check presents to plex.tv).
_SEED_PLEX_TOKEN = "seed-plex-token"  # noqa: S105 — test fixture value, not a credential
_SEED_OAUTH_TOKEN = "seed-admin-oauth-token"  # noqa: S105 — test fixture value, not a credential


class _OrderingLock(asyncio.Lock):
    """Event-observable lock used to prove the real shared-lock ordering."""

    def __init__(self) -> None:
        super().__init__()
        self.acquire_count = 0
        self.second_acquire_started = asyncio.Event()
        self.releases: asyncio.Queue[None] = asyncio.Queue()

    async def acquire(self) -> Literal[True]:
        self.acquire_count += 1
        if self.acquire_count == 2:
            self.second_acquire_started.set()
        return await super().acquire()

    def release(self) -> None:
        super().release()
        self.releases.put_nowait(None)


class _StopDrainLoop(Exception):
    """End one exercised drain tick without a real-time sleep."""


async def _wait_for_event(event: asyncio.Event) -> None:
    await asyncio.wait_for(event.wait(), timeout=5.0)


def test_every_known_setting_key_has_a_response_and_update_field() -> None:
    """Regression guard for the operability beta's original defect: every
    ``KNOWN_SETTING_KEYS`` entry (what ``SettingsStore.redacted()`` always
    returns a value for) must be a real field on BOTH ``SettingsResponse`` and
    ``SettingsUpdate`` -- otherwise it is readable/writable only via a direct
    DB edit, which violates the "100% web-operable" north star. The 7
    eviction/log-retention settings were once present in ``KNOWN_SETTING_KEYS``
    but absent from both schemas."""
    for key in KNOWN_SETTING_KEYS:
        assert key in SettingsResponse.model_fields, f"{key} missing from SettingsResponse"
        assert key in SettingsUpdate.model_fields, f"{key} missing from SettingsUpdate"


def test_settings_update_rejects_target_above_threshold() -> None:
    # R2-2: a disk_pressure_target above the trigger threshold makes every root in the
    # [threshold, target] band read "under pressure" yet select nothing -> a silent
    # dead band. When both are sent together it must be a visible 422, not accepted.
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SettingsUpdate(disk_pressure_threshold_percent=80.0, disk_pressure_target_percent=90.0)
    # equal and below the threshold are both fine.
    SettingsUpdate(disk_pressure_threshold_percent=80.0, disk_pressure_target_percent=80.0)
    SettingsUpdate(disk_pressure_threshold_percent=80.0, disk_pressure_target_percent=70.0)


def test_settings_update_validates_automatic_update_policy() -> None:
    valid = SettingsUpdate(
        automatic_update_timezone="America/Toronto",
        automatic_update_weekdays=["friday", "monday"],
        automatic_update_window_start="23:00",
        automatic_update_window_end="02:00",
    )
    assert valid.automatic_update_weekdays == ["monday", "friday"]

    for body in (
        {"automatic_update_timezone": "not/a-zone"},
        {"automatic_update_weekdays": []},
        {"automatic_update_weekdays": ["monday", "monday"]},
        {"automatic_update_window_start": "3am"},
        {
            "automatic_update_window_start": "03:00",
            "automatic_update_window_end": "03:00",
        },
    ):
        with pytest.raises(ValidationError):
            SettingsUpdate.model_validate(body)


@pytest.mark.parametrize("field", ["plex_token", "prowlarr_api_key"])
def test_settings_update_rejects_header_unsafe_credential(field: str) -> None:
    # A header-sink credential (plex_token -> X-Plex-Token, prowlarr_api_key ->
    # X-Api-Key) that cannot ride its HTTP header is rejected at write time, before
    # it can be stored and then leaked via httpx's str(exc) / crash the grab loop
    # when an adapter sends it. CR/LF/NUL (a str(exc) leak) and non-ASCII (an
    # uncaught UnicodeEncodeError/500) are the two closed failure modes.
    from pydantic import ValidationError

    for bad in ("key\r\ninjected", "key\x00nul", "kéy-nonascii"):
        with pytest.raises(ValidationError):
            SettingsUpdate.model_validate({field: bad})
    # Header-safe inputs pass untouched so partial updates and the FE mask
    # round-trip are unaffected: a plain ASCII key, the "***" redaction mask, and
    # an absent field are all accepted.
    SettingsUpdate.model_validate({field: "plain-ascii-key"})
    SettingsUpdate.model_validate({field: "***"})
    SettingsUpdate.model_validate({})


@pytest.mark.parametrize("field", ["plex_token", "prowlarr_api_key"])
async def test_put_settings_422_never_echoes_the_submitted_credential(
    client: httpx.AsyncClient, seed: SeedFn, field: str
) -> None:
    # north star #3: a header-unsafe credential submitted to PUT /settings is
    # rejected (422), but the 422 body must NEVER echo the submitted value.
    # FastAPI's DEFAULT handler returns the raw ``input``; the secret-redacting
    # RequestValidationError handler scrubs it. Assert on the RAW response text so a
    # leak anywhere in the body (input/ctx/msg) is caught.
    await seed(initialized=True, app_api_key=_API_KEY)
    sentinel = "leak-SENTINEL-\r\nZZZINJECT"

    response = await client.put(
        "/api/v1/settings", json={field: sentinel}, headers={"X-Api-Key": _API_KEY}
    )

    assert response.status_code == 422
    assert "SENTINEL" not in response.text
    assert "ZZZINJECT" not in response.text
    # The {"detail": [...]} envelope shape is preserved for the typed client.
    detail = response.json()["detail"]
    assert isinstance(detail, list) and detail
    assert any(err.get("loc", [])[-1:] == [field] for err in detail)


async def test_get_starts_empty(client: httpx.AsyncClient, seed: SeedFn) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    response = await client.get("/api/v1/settings", headers={"X-Api-Key": _API_KEY})
    assert response.status_code == 200
    body = response.json()
    assert body["plex_url"] is None
    assert body["tmdb_api_key"] is None


async def test_get_starts_with_tv_root_unset(client: httpx.AsyncClient, seed: SeedFn) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    response = await client.get("/api/v1/settings", headers={"X-Api-Key": _API_KEY})
    assert response.json()["tv_root"] is None


async def test_put_tv_root_round_trips_independently_of_movies_root(
    client: httpx.AsyncClient, seed: SeedFn, tmp_path: Path
) -> None:
    # tv_root is a plain (non-secret) path, just like movies_root, and settable
    # without touching movies_root -- the two roots are independently optional.
    await seed(initialized=True, app_api_key=_API_KEY)
    root = tmp_path / "tv"
    root.mkdir()
    put = await client.put(
        "/api/v1/settings", json={"tv_root": str(root)}, headers={"X-Api-Key": _API_KEY}
    )
    assert put.status_code == 200
    assert put.json()["tv_root"] == str(root)
    assert put.json()["movies_root"] is None

    got = (await client.get("/api/v1/settings", headers={"X-Api-Key": _API_KEY})).json()
    assert got["tv_root"] == str(root)


async def test_put_root_not_visible_is_422(
    client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    put = await client.put(
        "/api/v1/settings",
        json={"movies_root": "/nope"},
        headers={"X-Api-Key": _API_KEY},
    )
    assert put.status_code == 422
    assert put.json()["detail"] == "library_root_unreachable"

    got = await client.get("/api/v1/settings", headers={"X-Api-Key": _API_KEY})
    assert got.json()["movies_root"] is None  # nothing was written
    async with sessionmaker_() as session:
        assert await SettingsStore(session).get("movies_root") is None


async def test_put_remaps_host_root_to_container_path(
    client: httpx.AsyncClient,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    mount = tmp_path / "media"
    (mount / "Movies").mkdir(parents=True)
    # Library roots are remapped under the LIBRARY mounts only (never /downloads).
    # tmp dirs are never mount points, so relax the live-mount gate (the test seam).
    monkeypatch.setattr(path_visibility, "KNOWN_LIBRARY_MOUNTS", (str(mount),))
    monkeypatch.setattr(path_visibility, "is_live_mount", os.path.isdir)
    await seed(initialized=True, app_api_key=_API_KEY)

    put = await client.put(
        "/api/v1/settings",
        json={"movies_root": "/definitely-not-a-real-host-path/Media/Movies"},
        headers={"X-Api-Key": _API_KEY},
    )
    assert put.status_code == 200
    assert put.json()["movies_root"] == str(mount / "Movies")

    got = await client.get("/api/v1/settings", headers={"X-Api-Key": _API_KEY})
    assert got.json()["movies_root"] == str(mount / "Movies")


async def test_put_blank_root_clears_without_probing(
    client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    # Whitespace normalizes to "" (SettingsUpdate._blank_root_clears_to_unset) BEFORE
    # the root-visibility gate ever runs -- a clear must never probe or 422.
    await seed(initialized=True, app_api_key=_API_KEY)
    put = await client.put(
        "/api/v1/settings",
        json={"movies_root": "   "},
        headers={"X-Api-Key": _API_KEY},
    )
    assert put.status_code == 200
    assert put.json()["movies_root"] == ""

    async with sessionmaker_() as session:
        assert await SettingsStore(session).get("movies_root") == ""
        assert await get_movies_root_optional(session) is None


async def test_put_round_trips_and_redacts(client: httpx.AsyncClient, seed: SeedFn) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    update = {"plex_url": "http://plex.local:32400", "tmdb_api_key": "super-secret-key"}
    put = await client.put("/api/v1/settings", json=update, headers={"X-Api-Key": _API_KEY})
    assert put.status_code == 200
    put_body = put.json()
    assert put_body["plex_url"] == "http://plex.local:32400"
    assert put_body["tmdb_api_key"] == "***"
    assert "super-secret-key" not in put.text

    # GET reflects the same redacted view.
    got = (await client.get("/api/v1/settings", headers={"X-Api-Key": _API_KEY})).json()
    assert got["plex_url"] == "http://plex.local:32400"
    assert got["tmdb_api_key"] == "***"


# --------------------------------------------------------------------------- #
# Repointing Plex is VERIFIED before commit (post-init sign-in trusts           #
# PLEX_MACHINE_ID_SETTING; a changed plex_url/plex_token must probe the NEW     #
# server's /identity first, then cache the freshly DERIVED id)                  #
# --------------------------------------------------------------------------- #
async def _seed_plex_identity(
    sessionmaker_: SessionMaker,
    *,
    plex_url: str,
    machine_id: str,
    plex_token: str | None = _SEED_PLEX_TOKEN,
) -> None:
    """Store a plex_url (+ optional plex_token) + cached machine id, as setup would."""
    async with sessionmaker_() as session:
        store = SettingsStore(session)
        await store.set("plex_url", plex_url)
        if plex_token is not None:
            await store.set("plex_token", plex_token)
        await store.set(PLEX_MACHINE_ID_SETTING, machine_id)
        await session.commit()


async def _stored_machine_id(sessionmaker_: SessionMaker) -> str | None:
    async with sessionmaker_() as session:
        return await SettingsStore(session).get(PLEX_MACHINE_ID_SETTING)


async def _use_transport(app: FastAPI, transport: httpx.MockTransport) -> None:
    """Swap the app's shared HTTP client for one backed by ``transport``."""
    await app.state.http_client.aclose()
    app.state.http_client = httpx.AsyncClient(transport=transport)


def _owned_resource(machine_id: str) -> dict[str, object]:
    """A plex.tv resource entry for a server the account OWNS."""
    return {
        "name": "New Box",
        "clientIdentifier": machine_id,
        "provides": "server",
        "owned": True,
        "connections": [],
    }


def _shared_resource(machine_id: str) -> dict[str, object]:
    """A plex.tv resource entry for a server merely SHARED with the account."""
    return {
        "name": "Someone Elses Box",
        "clientIdentifier": machine_id,
        "provides": "server",
        "owned": False,
        "connections": [],
    }


def _repoint_transport(
    *,
    identity: str,
    authorized: bool = True,
    resources: list[dict[str, object]] | None = None,
    probes: list[httpx.Request] | None = None,
) -> httpx.MockTransport:
    """Serve the repoint verification ladder: /identity, /library/sections, plex.tv.

    ``authorized=False`` makes the AUTHENTICATED ``/library/sections`` check
    answer 401 (a reachable server that rejects the token). ``resources=None``
    makes any plex.tv ownership lookup FAIL the test loudly — the api-key path
    must never consult it; session-caller tests pass a resource list instead.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if probes is not None:
            probes.append(request)
        if request.url.host == "plex.tv" and request.url.path == "/api/v2/resources":
            if resources is None:
                raise AssertionError("ownership must not be consulted on this path")
            return httpx.Response(200, json=resources)
        if request.url.path == "/identity":
            return httpx.Response(200, json={"MediaContainer": {"machineIdentifier": identity}})
        if request.url.path == "/library/sections":
            if not authorized:
                return httpx.Response(401)
            return httpx.Response(200, json={"MediaContainer": {"Directory": []}})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    return httpx.MockTransport(handler)


def _no_probe_transport() -> httpx.MockTransport:
    """Fail the test loudly if PUT /settings issues ANY live request."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"PUT /settings must not probe here: {request.url}")

    return httpx.MockTransport(handler)


def _unreachable_transport() -> httpx.MockTransport:
    """Simulate a syntactically-valid but unreachable/typo'd Plex url."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    return httpx.MockTransport(handler)


async def test_put_changed_plex_url_stores_the_freshly_derived_machine_id(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """Repointing plex_url probes the NEW server's /identity BEFORE committing and
    caches the DERIVED id (better than the earlier clear-and-reprobe-per-sign-in:
    the id was just derived, so sign-in keeps its no-re-probe fast path). The FE
    must explicitly re-enter the token before a different origin may receive it,
    so BOTH verification calls (the /identity derive and the authenticated
    /library/sections check) use that explicitly authorized value. An
    api-key caller has no Plex account, so plex.tv ownership is never consulted
    (``resources=None`` fails loudly if it were)."""
    await seed(initialized=True, app_api_key=_API_KEY)
    await _seed_plex_identity(sessionmaker_, plex_url="http://old:32400", machine_id="OLD-MID")
    probes: list[httpx.Request] = []
    await _use_transport(app, _repoint_transport(identity="NEW-MID", probes=probes))

    put = await client.put(
        "/api/v1/settings",
        json={"plex_url": "http://new:32400", "plex_token": _SEED_PLEX_TOKEN},
        headers={"X-Api-Key": _API_KEY},
    )

    assert put.status_code == 200
    assert await _stored_machine_id(sessionmaker_) == "NEW-MID"  # derived, not cleared
    # Both probes hit the SUBMITTED url with the explicitly re-entered token.
    assert [str(p.url) for p in probes] == [
        "http://new:32400/identity",
        "http://new:32400/library/sections",
    ]
    assert all(p.headers.get("X-Plex-Token") == _SEED_PLEX_TOKEN for p in probes)


@pytest.mark.parametrize("new_url", ["http://new:32400", "http://old:32400/capture"])
async def test_put_changed_plex_destination_refuses_stored_token_reuse(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    new_url: str,
) -> None:
    """A URL-only repoint never discloses the encrypted stored token.

    This includes another reverse-proxy path on the same origin: it may route to
    a completely different backend just as surely as another host can.
    """
    await seed(initialized=True, app_api_key=_API_KEY)
    await _seed_plex_identity(sessionmaker_, plex_url="http://old:32400", machine_id="OLD-MID")
    await _use_transport(app, _no_probe_transport())

    put = await client.put(
        "/api/v1/settings",
        json={"plex_url": new_url, "plex_token": "***"},
        headers={"X-Api-Key": _API_KEY},
    )

    assert put.status_code == 422
    assert put.json()["detail"] == "credential_reentry_required"
    async with sessionmaker_() as session:
        store = SettingsStore(session)
        assert await store.get("plex_url") == "http://old:32400"
        assert await store.get("plex_token") == _SEED_PLEX_TOKEN
    assert await _stored_machine_id(sessionmaker_) == "OLD-MID"


@pytest.mark.parametrize(
    ("url_field", "secret_field"),
    [
        ("prowlarr_url", "prowlarr_api_key"),
        ("qbittorrent_url", "qbittorrent_password"),
    ],
)
@pytest.mark.parametrize("new_url", ["http://new-service:8080", "http://old-service:8080/capture"])
async def test_put_changed_service_destination_refuses_stored_secret_reuse(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    url_field: str,
    secret_field: str,
    new_url: str,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    async with sessionmaker_() as session:
        store = SettingsStore(session)
        await store.set(url_field, "http://old-service:8080")
        await store.set(secret_field, "stored-secret")
        await session.commit()
    await _use_transport(app, _no_probe_transport())

    put = await client.put(
        "/api/v1/settings",
        json={url_field: new_url},
        headers={"X-Api-Key": _API_KEY},
    )

    assert put.status_code == 422
    assert put.json()["detail"] == "credential_reentry_required"
    async with sessionmaker_() as session:
        store = SettingsStore(session)
        assert await store.get(url_field) == "http://old-service:8080"
        assert await store.get(secret_field) == "stored-secret"


async def test_put_changed_qbittorrent_destination_refuses_stored_empty_password_reuse(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    """A configured empty password still requires explicit destination consent."""
    await seed(initialized=True, app_api_key=_API_KEY)
    async with sessionmaker_() as session:
        store = SettingsStore(session)
        await store.set("qbittorrent_url", "http://old-service:8080")
        await store.set("qbittorrent_password", "")
        await session.commit()
    await _use_transport(app, _no_probe_transport())

    put = await client.put(
        "/api/v1/settings",
        json={"qbittorrent_url": "http://new-service:8080"},
        headers={"X-Api-Key": _API_KEY},
    )

    assert put.status_code == 422
    assert put.json()["detail"] == "credential_reentry_required"
    async with sessionmaker_() as session:
        store = SettingsStore(session)
        assert await store.get("qbittorrent_url") == "http://old-service:8080"
        assert await store.get("qbittorrent_password") == ""


async def test_put_changed_prowlarr_destination_allows_unconfigured_empty_api_key(
    client: httpx.AsyncClient,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    """An empty Prowlarr key is unconfigured, so no credential can cross origins."""
    await seed(initialized=True, app_api_key=_API_KEY)
    async with sessionmaker_() as session:
        store = SettingsStore(session)
        await store.set("prowlarr_url", "http://old-service:8080")
        await store.set("prowlarr_api_key", "")
        await session.commit()

    put = await client.put(
        "/api/v1/settings",
        json={"prowlarr_url": "http://new-service:8080"},
        headers={"X-Api-Key": _API_KEY},
    )

    assert put.status_code == 200
    async with sessionmaker_() as session:
        store = SettingsStore(session)
        assert await store.get("prowlarr_url") == "http://new-service:8080"
        assert await store.get("prowlarr_api_key") == ""


@pytest.mark.parametrize(
    ("url_field", "secret_field"),
    [
        ("prowlarr_url", "prowlarr_api_key"),
        ("qbittorrent_url", "qbittorrent_password"),
    ],
)
async def test_put_cross_origin_service_url_accepts_explicit_secret_reentry(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    url_field: str,
    secret_field: str,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    async with sessionmaker_() as session:
        store = SettingsStore(session)
        await store.set(url_field, "http://old-service:8080")
        await store.set(secret_field, "stored-secret")
        await session.commit()
    await _use_transport(app, _no_probe_transport())

    put = await client.put(
        "/api/v1/settings",
        json={url_field: "http://new-service:8080", secret_field: "replacement-secret"},
        headers={"X-Api-Key": _API_KEY},
    )

    assert put.status_code == 200
    async with sessionmaker_() as session:
        store = SettingsStore(session)
        assert await store.get(url_field) == "http://new-service:8080"
        assert await store.get(secret_field) == "replacement-secret"


async def test_put_changed_qbittorrent_base_accepts_explicit_empty_password(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """An empty qBittorrent password is valid and cannot reuse the old secret."""
    await seed(initialized=True, app_api_key=_API_KEY)
    async with sessionmaker_() as session:
        store = SettingsStore(session)
        await store.set("qbittorrent_url", "http://old-service:8080")
        await store.set("qbittorrent_password", "stored-secret")
        await session.commit()
    await _use_transport(app, _no_probe_transport())

    put = await client.put(
        "/api/v1/settings",
        json={"qbittorrent_url": "http://new-service:8080", "qbittorrent_password": ""},
        headers={"X-Api-Key": _API_KEY},
    )

    assert put.status_code == 200
    async with sessionmaker_() as session:
        store = SettingsStore(session)
        assert await store.get("qbittorrent_url") == "http://new-service:8080"
        assert await store.get("qbittorrent_password") == ""


async def test_put_serializes_destination_and_secret_updates(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A secret rotation cannot be paired with a concurrently repointed URL.

    Hold a secret-only PUT immediately before its write, then start a URL+dummy
    secret PUT. Without the endpoint-wide lock, the repoint commits while the
    rotation is paused and the rotation subsequently overwrites only its secret,
    leaving ``capture + fresh-secret``. The lock makes the repoint wait, re-read
    the pair committed by the rotation, and atomically leave ``capture + dummy``.
    """
    await seed(initialized=True, app_api_key=_API_KEY)
    async with sessionmaker_() as session:
        store = SettingsStore(session)
        await store.set("prowlarr_url", "http://old-service:9696")
        await store.set("prowlarr_api_key", "old-secret")
        await session.commit()
    await _use_transport(app, _no_probe_transport())

    from plex_manager.web.routers import settings as settings_router

    class ObservedSettingsLock(asyncio.Lock):
        """Signal when the second PUT has reached the contended lock."""

        def __init__(self) -> None:
            super().__init__()
            self.acquire_attempts = 0
            self.second_waiting = asyncio.Event()

        async def acquire(self) -> Literal[True]:
            self.acquire_attempts += 1
            if self.acquire_attempts == 2:
                self.second_waiting.set()
            return await super().acquire()

    # A contended asyncio.Lock binds to this test's event loop. Install a fresh
    # one so this regression stays independent of tests running in other loops.
    observed_lock = ObservedSettingsLock()
    monkeypatch.setattr(settings_router, "_settings_update_lock", observed_lock)

    real_set = SettingsStore.set
    rotation_ready = asyncio.Event()
    release_rotation = asyncio.Event()

    async def gated_set(store: SettingsStore, key: str, value: str) -> None:
        if key == "prowlarr_api_key" and value == "fresh-secret":
            rotation_ready.set()
            await asyncio.wait_for(release_rotation.wait(), timeout=5.0)
        await real_set(store, key, value)

    monkeypatch.setattr(SettingsStore, "set", gated_set)

    rotation = asyncio.create_task(
        client.put(
            "/api/v1/settings",
            json={"prowlarr_api_key": "fresh-secret"},
            headers={"X-Api-Key": _API_KEY},
        )
    )
    await asyncio.wait_for(rotation_ready.wait(), timeout=5.0)

    repoint = asyncio.create_task(
        client.put(
            "/api/v1/settings",
            json={
                "prowlarr_url": "http://capture:9696",
                "prowlarr_api_key": "dummy-secret",
            },
            headers={"X-Api-Key": _API_KEY},
        )
    )
    await asyncio.wait_for(observed_lock.second_waiting.wait(), timeout=5.0)
    assert not repoint.done()

    release_rotation.set()
    rotation_response, repoint_response = await asyncio.wait_for(
        asyncio.gather(rotation, repoint), timeout=10.0
    )

    assert rotation_response.status_code == 200
    assert repoint_response.status_code == 200
    async with sessionmaker_() as session:
        store = SettingsStore(session)
        assert await store.get("prowlarr_url") == "http://capture:9696"
        assert await store.get("prowlarr_api_key") == "dummy-secret"


async def test_put_changed_plex_token_probes_with_the_new_token(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """A real (non-masked) plex_token change is a repoint too: the stored url is
    probed (identity + authenticated sections) with the REPLACEMENT token and the
    derived id replaces the cache."""
    await seed(initialized=True, app_api_key=_API_KEY)
    await _seed_plex_identity(sessionmaker_, plex_url="http://old:32400", machine_id="OLD-MID")
    probes: list[httpx.Request] = []
    await _use_transport(app, _repoint_transport(identity="NEW-MID", probes=probes))

    put = await client.put(
        "/api/v1/settings", json={"plex_token": "new-token"}, headers={"X-Api-Key": _API_KEY}
    )

    assert put.status_code == 200
    assert await _stored_machine_id(sessionmaker_) == "NEW-MID"
    assert [str(p.url) for p in probes] == [
        "http://old:32400/identity",
        "http://old:32400/library/sections",
    ]
    assert all(p.headers.get("X-Plex-Token") == "new-token" for p in probes)  # the NEW token


async def test_put_unreachable_new_plex_url_is_502_and_commits_nothing(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """THE lockout guard: an admin typos plex_url (syntactically valid, but
    unreachable / the wrong box). The PUT must fail with the same honest 502
    envelope setup uses and commit NOTHING — old url intact, cached machine id
    intact, every session intact. Committing + revoking here would leave a
    keyless install with no working sign-in AND nobody signed in (DB surgery,
    the never-locked-out violation)."""
    await seed(initialized=True, app_api_key=_API_KEY)
    await _seed_plex_identity(sessionmaker_, plex_url="http://old:32400", machine_id="OLD-MID")
    cookies, csrf = await _admin_session_cookies(app, plex_id=9205, tag="typo")
    await _use_transport(app, _unreachable_transport())

    put = await client.put(
        "/api/v1/settings",
        json={"plex_url": "http://typo-wrong-host:32400", "plex_token": _SEED_PLEX_TOKEN},
        cookies=cookies,
        headers=csrf,
    )

    assert put.status_code == 502
    assert put.json()["detail"] == "server_unreachable_from_backend"
    async with sessionmaker_() as session:
        assert await SettingsStore(session).get("plex_url") == "http://old:32400"  # unchanged
    assert await _stored_machine_id(sessionmaker_) == "OLD-MID"  # unchanged
    assert await _active_session_count(sessionmaker_) == 1  # nobody signed out
    # The caller's session still works — they can immediately fix the typo.
    assert (await client.get("/api/v1/settings", cookies=cookies)).status_code == 200


async def test_put_masked_and_unchanged_plex_values_keep_cached_machine_id(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """A round-tripped masked secret ('***') and a same-value plex_url are NOT
    changes: no /identity probe fires (the transport fails loudly on ANY request)
    and the still-valid cached machine id is kept."""
    await seed(initialized=True, app_api_key=_API_KEY)
    await _seed_plex_identity(sessionmaker_, plex_url="http://old:32400", machine_id="OLD-MID")
    await _use_transport(app, _no_probe_transport())

    # The FE round-trips the whole object: unchanged plex_url + masked plex_token.
    put = await client.put(
        "/api/v1/settings",
        json={"plex_url": "http://old:32400", "plex_token": "***"},
        headers={"X-Api-Key": _API_KEY},
    )
    assert put.status_code == 200
    assert await _stored_machine_id(sessionmaker_) == "OLD-MID"  # kept, not dropped


async def test_put_url_change_with_no_stored_token_skips_probe_and_keeps_sessions(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """A HALF-configured identity (url changes, but no token exists anywhere)
    cannot be verified: the write proceeds without a probe, the STALE cached id
    is dropped (nothing may keep anchoring sign-in to the old server), and
    sessions are NOT revoked — revocation rides only a VERIFIED repoint (an
    incomplete identity can't mint sign-ins, so revoking would be the lockout
    trap again)."""
    await seed(initialized=True, app_api_key=_API_KEY)
    await _seed_plex_identity(
        sessionmaker_, plex_url="http://old:32400", machine_id="OLD-MID", plex_token=None
    )
    cookies, csrf = await _admin_session_cookies(app, plex_id=9206, tag="half")
    await _use_transport(app, _no_probe_transport())

    put = await client.put(
        "/api/v1/settings",
        json={"plex_url": "http://new:32400"},
        cookies=cookies,
        headers=csrf,
    )

    assert put.status_code == 200
    assert await _stored_machine_id(sessionmaker_) is None  # stale anchor dropped
    assert await _active_session_count(sessionmaker_) == 1  # nobody signed out
    assert (await client.get("/api/v1/settings", cookies=cookies)).status_code == 200


async def _active_session_count(sessionmaker_: SessionMaker) -> int:
    """Count auth sessions that are still usable (``revoked_at`` unset)."""
    async with sessionmaker_() as session:
        result = await session.execute(
            select(func.count()).select_from(AuthSession).where(AuthSession.revoked_at.is_(None))
        )
        return result.scalar_one()


async def test_put_plex_repoint_revokes_every_active_session_including_the_callers(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """A VERIFIED repoint is an auth-domain change (ADR-0016): swapping the cached
    machine id only fixes FUTURE sign-ins, so every already-minted session — whose
    persisted ``User.permissions`` still encodes the OLD server's authority — must
    be revoked in the same transaction. That includes the admin performing the
    repoint (deliberate, honest self-lockout): their PUT still completes cleanly
    (auth ran at dependency time), and their very NEXT request re-authenticates
    against a server the probe just PROVED is answering."""
    await seed(initialized=True, app_api_key=_API_KEY)
    await _seed_plex_identity(sessionmaker_, plex_url="http://old:32400", machine_id="OLD-MID")
    admin_cookies, admin_csrf = await _admin_session_cookies(app, plex_id=9201, tag="repoint-adm")
    other_cookies, _ = await _admin_session_cookies(app, plex_id=9202, tag="repoint-other")
    assert await _active_session_count(sessionmaker_) == 2
    hub = get_event_hub(app)
    admin_stream = hub.subscribe(auth_method=AuthMethod.plex_session.value, user_id=1)
    other_stream = hub.subscribe(auth_method=AuthMethod.plex_session.value, user_id=2)
    api_key_stream = hub.subscribe(auth_method=AuthMethod.api_key.value)
    for subscription in (admin_stream, other_stream, api_key_stream):
        _ = await subscription.get()  # initial sync
    probes: list[httpx.Request] = []
    await _use_transport(
        app,
        _repoint_transport(
            identity="NEW-MID", resources=[_owned_resource("NEW-MID")], probes=probes
        ),
    )

    put = await client.put(
        "/api/v1/settings",
        json={"plex_url": "http://new:32400", "plex_token": _SEED_PLEX_TOKEN},
        cookies=admin_cookies,
        headers=admin_csrf,
    )

    # The write itself completes for the now-revoked caller — never a mid-request 401.
    assert put.status_code == 200
    assert await _stored_machine_id(sessionmaker_) == "NEW-MID"  # the verified anchor
    assert await _active_session_count(sessionmaker_) == 0  # everyone, caller included
    assert admin_stream.closed
    assert other_stream.closed
    assert not api_key_stream.closed  # recovery-key authority survives a Plex repoint
    api_key_stream.close()
    # The ownership check presented the CALLER's own account OAuth token to plex.tv.
    ownership = [p for p in probes if p.url.host == "plex.tv"]
    assert ownership and ownership[0].headers.get("X-Plex-Token") == _SEED_OAUTH_TOKEN

    # Both old-server sessions must re-sign-in against the NEW server.
    assert (await client.get("/api/v1/settings", cookies=admin_cookies)).status_code == 401
    assert (await client.get("/api/v1/settings", cookies=other_cookies)).status_code == 401
    # The X-Api-Key recovery path is untouched — the repoint never locks the API out.
    assert (
        await client.get("/api/v1/settings", headers={"X-Api-Key": _API_KEY})
    ).status_code == 200


async def test_put_reachable_but_unauthorized_token_is_422_and_commits_nothing(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """/identity is UNAUTHENTICATED, so reachability alone must not verify a
    repoint: a reachable replacement server that REJECTS the effective token is
    a failed verification — 422 ``plex_token_invalid`` (the same code the
    envelope vocabulary already uses for a rejected Plex credential), with
    NOTHING committed and every session intact."""
    await seed(initialized=True, app_api_key=_API_KEY)
    await _seed_plex_identity(sessionmaker_, plex_url="http://old:32400", machine_id="OLD-MID")
    cookies, csrf = await _admin_session_cookies(app, plex_id=9207, tag="badtoken")
    await _use_transport(app, _repoint_transport(identity="NEW-MID", authorized=False))

    put = await client.put(
        "/api/v1/settings",
        json={"plex_url": "http://new:32400", "plex_token": _SEED_PLEX_TOKEN},
        cookies=cookies,
        headers=csrf,
    )

    assert put.status_code == 422
    assert put.json()["detail"] == "plex_token_invalid"
    async with sessionmaker_() as session:
        assert await SettingsStore(session).get("plex_url") == "http://old:32400"  # unchanged
    assert await _stored_machine_id(sessionmaker_) == "OLD-MID"  # unchanged
    assert await _active_session_count(sessionmaker_) == 1  # nobody signed out
    assert (await client.get("/api/v1/settings", cookies=cookies)).status_code == 200


async def test_put_session_admin_repoint_to_non_owned_server_is_403(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """The round-6 lockout class: a SESSION admin repointing to a valid,
    reachable, token-accepting server they do NOT own would commit + revoke
    everyone — and their next sign-in resolves NON-admin against the new machine
    id, locking a keyless install out of Settings. The wizard's ownership bar
    (403 ``server_not_owned``) must reject it BEFORE committing/revoking."""
    await seed(initialized=True, app_api_key=_API_KEY)
    await _seed_plex_identity(sessionmaker_, plex_url="http://old:32400", machine_id="OLD-MID")
    cookies, csrf = await _admin_session_cookies(app, plex_id=9208, tag="notowned")
    # The account can SEE the new server (it is shared with them) but does not own it.
    await _use_transport(
        app,
        _repoint_transport(identity="NEW-MID", resources=[_shared_resource("NEW-MID")]),
    )

    put = await client.put(
        "/api/v1/settings",
        json={"plex_url": "http://new:32400", "plex_token": _SEED_PLEX_TOKEN},
        cookies=cookies,
        headers=csrf,
    )

    assert put.status_code == 403
    assert put.json()["detail"] == "server_not_owned"
    async with sessionmaker_() as session:
        assert await SettingsStore(session).get("plex_url") == "http://old:32400"  # unchanged
    assert await _stored_machine_id(sessionmaker_) == "OLD-MID"  # unchanged
    assert await _active_session_count(sessionmaker_) == 1  # nobody signed out
    assert (await client.get("/api/v1/settings", cookies=cookies)).status_code == 200


async def test_put_api_key_repoint_skips_ownership_and_still_revokes(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """The documented asymmetry: an api-key admin has no Plex account, so its
    bar is reachability + the AUTHENTICATED token check only — plex.tv ownership
    is never consulted (``resources=None`` fails loudly if it were). A verified
    api-key repoint still revokes every browser session; the api key itself is
    untouched, which is exactly why the asymmetry is recoverable."""
    await seed(initialized=True, app_api_key=_API_KEY)
    await _seed_plex_identity(sessionmaker_, plex_url="http://old:32400", machine_id="OLD-MID")
    cookies, _csrf = await _admin_session_cookies(app, plex_id=9209, tag="apikey-repoint")
    assert await _active_session_count(sessionmaker_) == 1
    await _use_transport(app, _repoint_transport(identity="NEW-MID"))

    put = await client.put(
        "/api/v1/settings",
        json={"plex_url": "http://new:32400", "plex_token": _SEED_PLEX_TOKEN},
        headers={"X-Api-Key": _API_KEY},
    )

    assert put.status_code == 200
    assert await _stored_machine_id(sessionmaker_) == "NEW-MID"
    assert await _active_session_count(sessionmaker_) == 0  # browser sessions revoked
    assert (await client.get("/api/v1/settings", cookies=cookies)).status_code == 401
    # The api key keeps working — the recoverable half of the asymmetry.
    assert (
        await client.get("/api/v1/settings", headers={"X-Api-Key": _API_KEY})
    ).status_code == 200


async def test_verified_repoint_wakes_watchlist_worker(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """A verified repoint (new machine identifier) leaves old-server tokens STALE;
    the watchlist worker is what clears their eviction-protection snapshots (#296).
    Wake it immediately instead of waiting out the configured interval (hours/days),
    even though this PUT touched no watchlist-sync field."""
    await seed(initialized=True, app_api_key=_API_KEY)
    await _seed_plex_identity(sessionmaker_, plex_url="http://old:32400", machine_id="OLD-MID")
    app.state.watchlist_wake_event = asyncio.Event()
    await _use_transport(app, _repoint_transport(identity="NEW-MID"))

    put = await client.put(
        "/api/v1/settings",
        json={"plex_url": "http://new:32400", "plex_token": _SEED_PLEX_TOKEN},
        headers={"X-Api-Key": _API_KEY},
    )

    assert put.status_code == 200
    assert await _stored_machine_id(sessionmaker_) == "NEW-MID"
    assert app.state.watchlist_wake_event.is_set()


async def test_non_repoint_edit_does_not_wake_watchlist_worker(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    tmp_path: Path,
) -> None:
    """The mirror guard: a PUT that is neither a repoint nor a watchlist-sync change
    must NOT wake the worker, so an unrelated edit can't spam immediate ticks."""
    await seed(initialized=True, app_api_key=_API_KEY)
    await _seed_plex_identity(sessionmaker_, plex_url="http://old:32400", machine_id="OLD-MID")
    app.state.watchlist_wake_event = asyncio.Event()
    await _use_transport(app, _no_probe_transport())
    root = tmp_path / "tv"
    root.mkdir()

    put = await client.put(
        "/api/v1/settings",
        json={"tv_root": str(root)},
        headers={"X-Api-Key": _API_KEY},
    )

    assert put.status_code == 200
    assert not app.state.watchlist_wake_event.is_set()


async def test_explicit_unconfigure_wakes_watchlist_worker(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """An explicit clear of plex_url/plex_token (an UNVERIFIABLE identity change:
    the cached machine-id anchor is dropped, nothing is probed) must wake the
    watchlist worker just like a verified repoint: the worker's not_configured
    branch is what clears the now-orphaned snapshot rows (#327), and with a long
    sync interval they would otherwise keep protecting titles from eviction for
    hours/days after the operator explicitly walked away from Plex."""
    await seed(initialized=True, app_api_key=_API_KEY)
    await _seed_plex_identity(sessionmaker_, plex_url="http://old:32400", machine_id="OLD-MID")
    app.state.watchlist_wake_event = asyncio.Event()
    # An incomplete pair is unverifiable: the PUT must not issue any live probe.
    await _use_transport(app, _no_probe_transport())

    put = await client.put(
        "/api/v1/settings",
        json={"plex_url": "", "plex_token": ""},
        headers={"X-Api-Key": _API_KEY},
    )

    assert put.status_code == 200
    assert await _stored_machine_id(sessionmaker_) is None  # stale anchor dropped
    assert app.state.watchlist_wake_event.is_set()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("auto_grab_enabled", False),
        ("auto_grab_interval_seconds", 30.0),
        ("auto_grab_max_searches_per_cycle", 8),
    ],
)
async def test_autograb_timing_change_wakes_autograb_worker(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    field: str,
    value: object,
) -> None:
    """A shortened interval, a changed search cap, or a re-enable must be
    observed on the next tick, not after the OLD (up to 1h) sleep expires
    (issue #332): the PUT sets ``app.state.autograb_wake_event``."""
    await seed(initialized=True, app_api_key=_API_KEY)
    app.state.autograb_wake_event = asyncio.Event()

    put = await client.put(
        "/api/v1/settings",
        json={field: value},
        headers={"X-Api-Key": _API_KEY},
    )

    assert put.status_code == 200
    assert app.state.autograb_wake_event.is_set()


async def test_non_autograb_edit_does_not_wake_autograb_worker(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    tmp_path: Path,
) -> None:
    """The mirror guard: a PUT touching no auto-grab timing field must NOT wake
    the auto-grab worker, so an unrelated edit can't spam immediate ticks."""
    await seed(initialized=True, app_api_key=_API_KEY)
    app.state.autograb_wake_event = asyncio.Event()
    root = tmp_path / "tv"
    root.mkdir()

    put = await client.put(
        "/api/v1/settings",
        json={"tv_root": str(root)},
        headers={"X-Api-Key": _API_KEY},
    )

    assert put.status_code == 200
    assert not app.state.autograb_wake_event.is_set()


async def test_put_non_plex_fields_keep_sessions_active(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    tmp_path: Path,
) -> None:
    """A PUT that touches no Plex identity field is NOT a repoint: nobody is
    signed out — and no live /identity probe fires — over a library-root or
    Prowlarr edit (the transport fails loudly on ANY request)."""
    await seed(initialized=True, app_api_key=_API_KEY)
    await _seed_plex_identity(sessionmaker_, plex_url="http://old:32400", machine_id="OLD-MID")
    cookies, csrf = await _admin_session_cookies(app, plex_id=9203, tag="non-plex")
    await _use_transport(app, _no_probe_transport())
    root = tmp_path / "tv"
    root.mkdir()

    put = await client.put(
        "/api/v1/settings",
        json={"tv_root": str(root), "prowlarr_url": "http://prowlarr.local:9696"},
        cookies=cookies,
        headers=csrf,
    )

    assert put.status_code == 200
    assert await _active_session_count(sessionmaker_) == 1  # still signed in
    assert (await client.get("/api/v1/settings", cookies=cookies)).status_code == 200


async def test_put_masked_and_unchanged_plex_values_keep_sessions_active(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """The masked-secret round-trip ('***') and a same-value plex_url are NOT
    repoints (the same non-changes that keep the cached machine id): no probe
    fires and the FE saving an unrelated field never signs the install out."""
    await seed(initialized=True, app_api_key=_API_KEY)
    await _seed_plex_identity(sessionmaker_, plex_url="http://old:32400", machine_id="OLD-MID")
    cookies, csrf = await _admin_session_cookies(app, plex_id=9204, tag="masked")
    await _use_transport(app, _no_probe_transport())

    put = await client.put(
        "/api/v1/settings",
        json={"plex_url": "http://old:32400", "plex_token": "***"},
        cookies=cookies,
        headers=csrf,
    )

    assert put.status_code == 200
    assert await _active_session_count(sessionmaker_) == 1  # still signed in
    assert (await client.get("/api/v1/settings", cookies=cookies)).status_code == 200


async def test_secret_is_stored_encrypted(
    client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    plaintext = "another-secret-key"
    await client.put(
        "/api/v1/settings",
        json={"tmdb_api_key": plaintext, "plex_url": "http://plex.local"},
        headers={"X-Api-Key": _API_KEY},
    )

    # Inspect the raw columns, bypassing the EncryptedStr decryption layer.
    async with sessionmaker_() as session:
        secret_row = (
            await session.execute(
                text(
                    "SELECT value, encrypted_value, is_secret "
                    "FROM settings WHERE key = 'tmdb_api_key'"
                )
            )
        ).one()
        plain_row = (
            await session.execute(
                text("SELECT value, encrypted_value FROM settings WHERE key = 'plex_url'")
            )
        ).one()

    raw_value, raw_encrypted, is_secret = secret_row
    assert bool(is_secret) is True
    assert raw_value is None  # the plaintext column is never used for a secret
    assert raw_encrypted is not None
    assert plaintext not in raw_encrypted  # at-rest value is ciphertext, not plaintext

    # The non-secret url is stored in the plaintext column, unencrypted.
    assert plain_row[0] == "http://plex.local"
    assert plain_row[1] is None


async def test_put_mask_round_trip_does_not_clobber_secret(
    client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    headers = {"X-Api-Key": _API_KEY}

    # Establish a real secret.
    await client.put(
        "/api/v1/settings",
        json={"tmdb_api_key": "real-tmdb-secret", "plex_url": "http://plex.local"},
        headers=headers,
    )

    # FE GETs the redacted view (secret shows as the mask), edits only a non-secret
    # field, and PUTs the whole object back verbatim — mask and all.
    got = (await client.get("/api/v1/settings", headers=headers)).json()
    assert got["tmdb_api_key"] == "***"
    got["plex_url"] = "http://plex.local:32400"
    put = await client.put("/api/v1/settings", json=got, headers=headers)
    assert put.status_code == 200
    assert put.json()["plex_url"] == "http://plex.local:32400"

    # The real secret must survive — the mask write was a no-op, not a wipe.
    async with sessionmaker_() as session:
        assert await SettingsStore(session).get("tmdb_api_key") == "real-tmdb-secret"


async def test_empty_string_root_reads_back_as_unset(
    client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """PUT only skips a field when it's ``None`` (absent) — an empty-string
    ``movies_root``/``tv_root`` (e.g. a frontend "clear" that submits ``""``
    instead of omitting the field) is written verbatim. The importer's
    ``get_*_root_optional`` deps must still report that as unset (``None``), not
    a falsy-but-truthy-looking path: otherwise it would sail past a downstream
    ``is None`` guard and silently resolve relative paths against the process
    CWD instead of tripping the honest ``ImportBlocked`` it's meant to."""
    await seed(initialized=True, app_api_key=_API_KEY)
    put = await client.put(
        "/api/v1/settings",
        json={"movies_root": "", "tv_root": ""},
        headers={"X-Api-Key": _API_KEY},
    )
    assert put.status_code == 200

    async with sessionmaker_() as session:
        assert await SettingsStore(session).get("movies_root") == ""
        assert await SettingsStore(session).get("tv_root") == ""
        assert await get_movies_root_optional(session) is None
        assert await get_tv_root_optional(session) is None


async def test_whitespace_only_root_reads_back_as_unset(
    client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """Issue #83: a whitespace-only root (e.g. an operator submitting a stray
    space) is truthy in Python and has no "leave unchanged" ``None`` meaning --
    unlike ``SetupCompleteRequest``, ``SettingsUpdate`` had no validator
    stripping this, so it would previously be persisted VERBATIM and then read
    back as a non-``None``, seemingly-configured root. ``PUT /settings`` must
    normalize it to the SAME "" clear-to-unset write ``test_empty_string_root_
    reads_back_as_unset`` above already covers, and every ``get_*_root_optional``
    read must independently treat it as unset too (defense in depth)."""
    await seed(initialized=True, app_api_key=_API_KEY)
    put = await client.put(
        "/api/v1/settings",
        json={"movies_root": "   ", "tv_root": "\t\n "},
        headers={"X-Api-Key": _API_KEY},
    )
    assert put.status_code == 200
    # The redacted response mirrors the raw stored ("") value exactly like
    # test_empty_string_root_reads_back_as_unset above -- it is the TYPED
    # get_*_root_optional deps (checked below), not this response, that report
    # "unset".
    assert put.json()["movies_root"] == ""
    assert put.json()["tv_root"] == ""

    async with sessionmaker_() as session:
        # Normalized to "" at write time (the model_validator), matching the
        # established empty-string clear-to-unset convention.
        assert await SettingsStore(session).get("movies_root") == ""
        assert await SettingsStore(session).get("tv_root") == ""
        assert await get_movies_root_optional(session) is None
        assert await get_tv_root_optional(session) is None


async def test_padded_non_blank_root_reads_back_byte_identical(
    seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """Codex P2: a stored root that is non-blank but carries incidental
    leading/trailing whitespace (distinct from the ALL-whitespace case
    ``test_whitespace_only_root_reads_back_as_unset`` above covers) must read
    back through ``get_*_root_optional`` BYTE-IDENTICAL to what ``GET /settings``
    displays (``SettingsStore.get``/``redacted`` never strip). ``_blank_to_none``
    previously ``.strip()``-ed every non-``None`` value, which would silently
    retarget import/scan/evict to a different directory than the one the
    operator sees configured -- writing directly via ``SettingsStore`` here
    (bypassing ``PUT /settings``'s schema validator, which only touches the
    ALL-whitespace case) isolates ``_blank_to_none``'s own behavior against
    already-stored data of this shape."""
    await seed(initialized=True, app_api_key=_API_KEY)
    padded = "  /media/movies  "
    async with sessionmaker_() as session:
        await SettingsStore(session).set("movies_root", padded)
        await session.commit()

    async with sessionmaker_() as session:
        # What GET /settings would display (SettingsStore.redacted() -> row.value,
        # never stripped) ...
        assert await SettingsStore(session).get("movies_root") == padded
        # ... must match EXACTLY what the importer/eviction dependency resolves --
        # never a trimmed variant of it.
        assert await get_movies_root_optional(session) == padded


# --------------------------------------------------------------------------- #
# Service URL shape validation at write time (issue #44)
# --------------------------------------------------------------------------- #
_BAD_SERVICE_URLS = [
    "http://[::1",  # unterminated IPv6 literal -- urlsplit() itself raises ValueError
    "localhost:9696",  # scheme-less
    "ftp://x",  # wrong scheme
    "http://",  # empty host
    "not a url at all",
    "http://x:bad",  # non-numeric port -> would otherwise raise httpx.InvalidURL
    "http://x:0",  # port 0 parses cleanly but is never connectable
    "http://x:99999",  # out-of-range port
    "http://\nx",  # embedded control char (CR/LF log-forging shape)
    "http://x/\x01",  # control char in path
    "http://plex local",  # whitespace in the authority -- urlsplit still yields a host
    "http://x/base path",  # whitespace anywhere (here in the path) is rejected too
    "http://user:password@x/base",  # URL userinfo can redirect credentials to another host
    "http://good\\@evil/base",  # browser-style backslash/userinfo authority ambiguity
    "http://x/a/../admin",  # raw dot-segment escapes the configured proxy prefix
    "http://x/a/%2e%2e/admin",  # encoded traversal may be decoded by an intermediary
    "http://x/%2fadmin",  # encoded slash changes path segmentation downstream
    "http://x/a//admin",  # empty segment is normalized inconsistently by proxies
    "http://x?y=1",  # query -- adapters append API paths, so a query is swallowed
    "http://x#frag",  # fragment -- likewise swallows the appended API path
    "http://x?",  # BARE query delimiter -- urlsplit yields an EMPTY query, raw '?' remains
    "http://x#",  # bare fragment delimiter -- likewise
    "http://999.999.999.999",  # IPv4-shaped host with out-of-range octets
    "http://01.02.03.04",  # IPv4-shaped host with leading-zero octets
    "http://[v7.abc]",  # IPvFuture -- urlsplit tolerates it, httpx raises InvalidURL
    "http://[fe80::1%eth0]",  # IPv6 zone id -- rejected by policy for a base URL
    "http://[fe80::1%25eth0]",  # RFC 6874 percent-encoded zone id -- likewise
    "http://\N{PILE OF POO}.local",  # IDNA-unencodable label -- httpx.URL() ctor raises
    "http://xn--zzzzzz",  # bogus punycode A-label -- raises only from httpx .host decode
    "http://xn--ls8h.local",  # pre-encoded emoji label -- same class, punycode form
]


@pytest.mark.parametrize("field", ["plex_url", "prowlarr_url", "qbittorrent_url"])
@pytest.mark.parametrize("bad_url", _BAD_SERVICE_URLS)
async def test_put_settings_rejects_malformed_service_url_and_does_not_persist(
    client: httpx.AsyncClient,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    field: str,
    bad_url: str,
) -> None:
    # Shape-validated at write time (issue #44): the exact predicate the setup
    # wizard's "Test connection" probes use (``url_validation.url_shape_error``),
    # now ALSO enforced on the authenticated PUT /settings write path so a
    # malformed url is a visible 422 before it is ever persisted, not just a
    # later opaque failure from the downstream service.
    await seed(initialized=True, app_api_key=_API_KEY)
    headers = {"X-Api-Key": _API_KEY}

    put = await client.put("/api/v1/settings", json={field: bad_url}, headers=headers)
    assert put.status_code == 422

    async with sessionmaker_() as session:
        assert await SettingsStore(session).get(field) is None


@pytest.mark.parametrize("field", ["plex_url", "prowlarr_url", "qbittorrent_url"])
async def test_put_settings_accepts_valid_https_service_url(
    client: httpx.AsyncClient, seed: SeedFn, field: str
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    headers = {"X-Api-Key": _API_KEY}

    put = await client.put(
        "/api/v1/settings", json={field: "https://example.com:8443"}, headers=headers
    )
    assert put.status_code == 200
    assert put.json()[field] == "https://example.com:8443"


@pytest.mark.parametrize("field", ["plex_url", "prowlarr_url", "qbittorrent_url"])
@pytest.mark.parametrize(
    "good_url",
    [
        "http://prowlarr.local:9696/prowlarr",  # path-prefix (reverse-proxy) base URL
        "http://prowlarr.local:9696/",  # bare trailing slash
        "http://192.168.1.10:32400",  # valid dotted-quad IPv4 host
        "http://[::1]:32400",  # IPv6 literal host (untouched by the IPv4 check)
        # VALID IPv6, despite looking suspicious: 9999 is a legal hex group. This
        # was Codex PR #53 wave 4's claimed-broken example -- empirically urlsplit,
        # ipaddress AND httpx all accept it, so it must stay accepted.
        "http://[9999::1]:32400",
        # VALID punycode (café.local) -- guards the wave-5 httpx gate's .host
        # touch against over-tightening: only UNdecodable xn-- labels reject.
        "http://xn--caf-dma.local:32400",
    ],
)
async def test_put_settings_accepts_legitimate_base_url_shapes(
    client: httpx.AsyncClient, seed: SeedFn, field: str, good_url: str
) -> None:
    # Tightening the shared predicate (query/fragment, IPv4-shaped hosts) must NOT
    # reject a legitimate base URL: a path prefix (reverse-proxy mount), a bare
    # trailing slash, a valid dotted-quad IPv4, and an IPv6 literal all round-trip
    # through the write path unchanged.
    await seed(initialized=True, app_api_key=_API_KEY)
    headers = {"X-Api-Key": _API_KEY}

    put = await client.put("/api/v1/settings", json={field: good_url}, headers=headers)
    assert put.status_code == 200
    assert put.json()[field] == good_url


async def test_put_settings_partial_update_omitting_urls_leaves_them_untouched(
    client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    headers = {"X-Api-Key": _API_KEY}
    await client.put(
        "/api/v1/settings",
        json={
            "plex_url": "http://plex.local:32400",
            "prowlarr_url": "http://prowlarr.local:9696",
            "qbittorrent_url": "http://qb.local:8080",
        },
        headers=headers,
    )

    # A later partial update naming only an unrelated field must not fire the
    # validator for the omitted (absent -> ``None``) url fields, and must leave
    # every previously-stored url exactly as it was.
    put = await client.put(
        "/api/v1/settings", json={"qbittorrent_username": "admin"}, headers=headers
    )
    assert put.status_code == 200
    body = put.json()
    assert body["plex_url"] == "http://plex.local:32400"
    assert body["prowlarr_url"] == "http://prowlarr.local:9696"
    assert body["qbittorrent_url"] == "http://qb.local:8080"


@pytest.mark.parametrize("field", ["plex_url", "prowlarr_url", "qbittorrent_url"])
async def test_put_settings_empty_string_service_url_clears_and_is_not_shape_checked(
    client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker, field: str
) -> None:
    # '' is an explicit clear-to-unset (matching movies_root's convention) --
    # ALLOWED, never shape-checked/rejected. The adapters already treat a falsy
    # stored url as unconfigured (an honest 409 service_not_configured), so this
    # is a valid, intentional write.
    await seed(initialized=True, app_api_key=_API_KEY)
    headers = {"X-Api-Key": _API_KEY}
    await client.put(
        "/api/v1/settings", json={field: "http://configured.example:1234"}, headers=headers
    )

    put = await client.put("/api/v1/settings", json={field: ""}, headers=headers)
    assert put.status_code == 200

    async with sessionmaker_() as session:
        assert await SettingsStore(session).get(field) == ""


# --------------------------------------------------------------------------- #
# Anime library routing (ADR-0015): anime_movie_root / anime_tv_root
# --------------------------------------------------------------------------- #
async def test_get_starts_with_anime_roots_unset(client: httpx.AsyncClient, seed: SeedFn) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    response = await client.get("/api/v1/settings", headers={"X-Api-Key": _API_KEY})
    body = response.json()
    assert body["anime_movie_root"] is None
    assert body["anime_tv_root"] is None


async def test_put_anime_roots_round_trip_independently_of_movies_and_tv_root(
    client: httpx.AsyncClient, seed: SeedFn, tmp_path: Path
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    anime_movies = tmp_path / "anime-movies"
    anime_movies.mkdir()
    anime_tv = tmp_path / "anime-tv"
    anime_tv.mkdir()
    put = await client.put(
        "/api/v1/settings",
        json={"anime_movie_root": str(anime_movies), "anime_tv_root": str(anime_tv)},
        headers={"X-Api-Key": _API_KEY},
    )
    assert put.status_code == 200
    body = put.json()
    assert body["anime_movie_root"] == str(anime_movies)
    assert body["anime_tv_root"] == str(anime_tv)
    # Untouched by the anime-only PUT.
    assert body["movies_root"] is None
    assert body["tv_root"] is None

    got = (await client.get("/api/v1/settings", headers={"X-Api-Key": _API_KEY})).json()
    assert got["anime_movie_root"] == str(anime_movies)
    assert got["anime_tv_root"] == str(anime_tv)


async def test_put_partial_anime_root_only_leaves_the_other_and_normal_roots_untouched(
    client: httpx.AsyncClient, seed: SeedFn, tmp_path: Path
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    movies_root = tmp_path / "movies"
    movies_root.mkdir()
    anime_movies = tmp_path / "anime-movies"
    anime_movies.mkdir()
    anime_tv = tmp_path / "anime-tv"
    anime_tv.mkdir()
    await client.put(
        "/api/v1/settings",
        json={"movies_root": str(movies_root), "anime_movie_root": str(anime_movies)},
        headers={"X-Api-Key": _API_KEY},
    )
    put = await client.put(
        "/api/v1/settings",
        json={"anime_tv_root": str(anime_tv)},
        headers={"X-Api-Key": _API_KEY},
    )
    assert put.status_code == 200
    body = put.json()
    assert body["anime_tv_root"] == str(anime_tv)
    assert body["anime_movie_root"] == str(anime_movies)  # untouched by this partial PUT
    assert body["movies_root"] == str(movies_root)  # untouched by this partial PUT


async def test_empty_string_anime_root_reads_back_as_unset(
    client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """Mirrors ``test_empty_string_root_reads_back_as_unset``: an empty-string
    anime root (a frontend clear-on-Plex-reconnect) reads back as unset, never
    a falsy-but-truthy path that would sail past the importer's ``is None``
    guard."""
    await seed(initialized=True, app_api_key=_API_KEY)
    put = await client.put(
        "/api/v1/settings",
        json={"anime_movie_root": "", "anime_tv_root": ""},
        headers={"X-Api-Key": _API_KEY},
    )
    assert put.status_code == 200

    async with sessionmaker_() as session:
        assert await SettingsStore(session).get("anime_movie_root") == ""
        assert await SettingsStore(session).get("anime_tv_root") == ""
        assert await get_anime_movie_root_optional(session) is None
        assert await get_anime_tv_root_optional(session) is None


# --------------------------------------------------------------------------- #
# Operability beta (ADR-0012) settings: disk-pressure eviction + log retention
# --------------------------------------------------------------------------- #
async def test_put_round_trips_operability_settings(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    headers = {"X-Api-Key": _API_KEY}
    app.state.watchlist_wake_event = asyncio.Event()
    update = {
        "disk_pressure_threshold_percent": 88.5,
        "disk_pressure_target_percent": 75,
        "eviction_grace_days": 14,
        "eviction_enabled": False,
        "eviction_proactive_enabled": True,
        "eviction_interval_minutes": 45,
        "watchlist_sync_enabled": False,
        "watchlist_sync_interval_minutes": 20,
        "log_retention_days": 3,
        "log_max_rows": 50_000,
    }
    put = await client.put("/api/v1/settings", json=update, headers=headers)
    assert put.status_code == 200
    assert app.state.watchlist_wake_event.is_set()
    body = put.json()
    assert body["disk_pressure_threshold_percent"] == 88.5
    assert body["disk_pressure_target_percent"] == 75.0
    assert body["eviction_grace_days"] == 14
    assert body["eviction_enabled"] is False
    assert body["eviction_proactive_enabled"] is True
    assert body["eviction_interval_minutes"] == 45.0
    assert body["watchlist_sync_enabled"] is False
    assert body["watchlist_sync_interval_minutes"] == 20.0
    assert body["log_retention_days"] == 3
    assert body["log_max_rows"] == 50_000

    # GET reflects the identical stored values.
    got = (await client.get("/api/v1/settings", headers=headers)).json()
    assert got == body

    # The typed getters the eviction/log-retention loops actually read must see
    # the SAME values -- not just a wire-level round trip (guards against e.g.
    # a bool serialized in a form the case-insensitive parser wouldn't accept).
    async with sessionmaker_() as session:
        assert await get_disk_pressure_threshold_percent(session) == 88.5
        assert await get_disk_pressure_target_percent(session) == 75.0
        assert await get_eviction_grace_days(session) == 14
        assert await get_eviction_enabled(session) is False
        assert await get_eviction_proactive_enabled(session) is True
        assert await get_eviction_interval_minutes(session) == 45.0
        assert await get_watchlist_sync_interval_minutes(session) == 20.0
        assert await get_log_retention_days(session) == 3
        assert await get_log_max_rows(session) == 50_000


async def test_put_round_trips_automatic_update_policy(
    client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    headers = {"X-Api-Key": _API_KEY}
    update = {
        "automatic_updates_enabled": True,
        "automatic_update_timezone": "America/Toronto",
        "automatic_update_weekdays": ["friday", "monday"],
        "automatic_update_window_start": "23:00",
        "automatic_update_window_end": "02:00",
        "automatic_update_idle_only": False,
    }
    put = await client.put("/api/v1/settings", json=update, headers=headers)
    assert put.status_code == 200
    body = put.json()
    assert body["automatic_updates_enabled"] is True
    assert body["automatic_update_timezone"] == "America/Toronto"
    assert body["automatic_update_weekdays"] == ["monday", "friday"]
    assert body["automatic_update_window_start"] == "23:00"
    assert body["automatic_update_window_end"] == "02:00"
    assert body["automatic_update_idle_only"] is False

    got = await client.get("/api/v1/settings", headers=headers)
    assert got.status_code == 200
    assert got.json() == body
    async with sessionmaker_() as session:
        stored = await SettingsStore(session).get("automatic_update_weekdays")
    assert stored == '["monday","friday"]'


async def test_put_rejects_effectively_equal_partial_update_window(
    client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    async with sessionmaker_() as session:
        await SettingsStore(session).set("automatic_update_window_end", "04:30")
        await session.commit()

    response = await client.put(
        "/api/v1/settings",
        json={"automatic_update_window_start": "04:30"},
        headers={"X-Api-Key": _API_KEY},
    )
    assert response.status_code == 422
    async with sessionmaker_() as session:
        assert await SettingsStore(session).get("automatic_update_window_start") is None


@pytest.mark.parametrize(
    ("field", "corrupt"),
    [
        ("automatic_update_timezone", "not/a-zone"),
        ("automatic_update_weekdays", "['monday']"),
        ("automatic_update_weekdays", '["monday","monday"]'),
        ("automatic_update_window_start", "25:00"),
    ],
)
async def test_get_settings_degrades_corrupt_automatic_update_policy(
    client: httpx.AsyncClient,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    field: str,
    corrupt: str,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    async with sessionmaker_() as session:
        await SettingsStore(session).set(field, corrupt)
        await session.commit()
    response = await client.get("/api/v1/settings", headers={"X-Api-Key": _API_KEY})
    assert response.status_code == 200
    expected = "UTC" if field == "automatic_update_timezone" else None
    assert response.json()[field] == expected


async def test_partial_update_policy_displays_the_effective_utc_timezone(
    client: httpx.AsyncClient,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    async with sessionmaker_() as session:
        await SettingsStore(session).set("automatic_updates_enabled", "true")
        await session.commit()

    response = await client.get("/api/v1/settings", headers={"X-Api-Key": _API_KEY})
    assert response.status_code == 200
    assert response.json()["automatic_update_timezone"] == "UTC"


async def test_get_settings_degrades_equal_automatic_update_window(
    client: httpx.AsyncClient,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    async with sessionmaker_() as session:
        store = SettingsStore(session)
        await store.set("automatic_update_window_start", "04:30")
        await store.set("automatic_update_window_end", "04:30")
        await session.commit()

    response = await client.get("/api/v1/settings", headers={"X-Api-Key": _API_KEY})
    assert response.status_code == 200
    assert response.json()["automatic_update_window_start"] is None
    assert response.json()["automatic_update_window_end"] is None


async def test_put_rejects_out_of_range_operability_settings(
    client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    headers = {"X-Api-Key": _API_KEY}

    over_100 = await client.put(
        "/api/v1/settings",
        json={"disk_pressure_threshold_percent": 150},
        headers=headers,
    )
    assert over_100.status_code == 422

    zero_interval = await client.put(
        "/api/v1/settings",
        json={"eviction_interval_minutes": 0},
        headers=headers,
    )
    assert zero_interval.status_code == 422

    zero_watchlist_interval = await client.put(
        "/api/v1/settings",
        json={"watchlist_sync_interval_minutes": 0},
        headers=headers,
    )
    assert zero_watchlist_interval.status_code == 422

    negative_days = await client.put(
        "/api/v1/settings",
        json={"log_retention_days": -1},
        headers=headers,
    )
    assert negative_days.status_code == 422

    negative_max_rows = await client.put(
        "/api/v1/settings",
        json={"log_max_rows": -1},
        headers=headers,
    )
    assert negative_max_rows.status_code == 422


async def test_put_single_field_threshold_below_stored_target_rejects_and_does_not_persist(
    client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """R4-2: ``SettingsUpdate``'s own ``model_validator`` only catches a target
    above the threshold when BOTH fields are sent in the SAME request -- ``PUT``
    is a PARTIAL update, so a request naming just ONE side against an
    already-stored (now-inverted) other side must ALSO 422, cross-checked
    against what is actually persisted (see
    ``routers.settings._validate_disk_pressure_pair``), or the whole
    threshold-to-target band silently stops relieving pressure."""
    await seed(initialized=True, app_api_key=_API_KEY)
    headers = {"X-Api-Key": _API_KEY}

    # Establish a stored target of 80 (paired with a valid threshold of 95).
    seeded = await client.put(
        "/api/v1/settings",
        json={"disk_pressure_threshold_percent": 95.0, "disk_pressure_target_percent": 80.0},
        headers=headers,
    )
    assert seeded.status_code == 200

    # A split update naming ONLY the threshold, now BELOW the stored target (80).
    put = await client.put(
        "/api/v1/settings",
        json={"disk_pressure_threshold_percent": 70.0},
        headers=headers,
    )
    assert put.status_code == 422

    # Never persisted -- both sides stay at their last valid stored values.
    async with sessionmaker_() as session:
        assert await get_disk_pressure_threshold_percent(session) == 95.0
        assert await get_disk_pressure_target_percent(session) == 80.0


async def test_put_single_field_threshold_above_stored_target_still_succeeds(
    client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """The stored-value cross-check only rejects an INVERTED effective pair -- a
    valid partial update (threshold alone, still above the stored target) must
    succeed normally, never over-rejected by the new check."""
    await seed(initialized=True, app_api_key=_API_KEY)
    headers = {"X-Api-Key": _API_KEY}

    seeded = await client.put(
        "/api/v1/settings",
        json={"disk_pressure_threshold_percent": 85.0, "disk_pressure_target_percent": 80.0},
        headers=headers,
    )
    assert seeded.status_code == 200

    put = await client.put(
        "/api/v1/settings",
        json={"disk_pressure_threshold_percent": 90.0},
        headers=headers,
    )
    assert put.status_code == 200
    assert put.json()["disk_pressure_threshold_percent"] == 90.0

    async with sessionmaker_() as session:
        assert await get_disk_pressure_threshold_percent(session) == 90.0
        assert await get_disk_pressure_target_percent(session) == 80.0  # untouched


# --------------------------------------------------------------------------- #
# Non-finite / overflow typed settings (issue #92): reject on write, tolerate
# on read. A stored ``inf``/``nan`` (or a finite-but-huge value) must never
# hang the eviction loop's ``asyncio.sleep`` or 500 ``GET /settings``.
# --------------------------------------------------------------------------- #
def test_settings_update_rejects_non_finite_interval() -> None:
    """Model-level guard (mirrors ``test_settings_update_rejects_target_above_
    threshold`` above): every non-finite value AND anything past the upper
    ceiling must fail construction, while the ceiling itself and an ordinary
    value both still construct fine."""
    for bad in (float("inf"), float("nan"), float("-inf"), EVICTION_INTERVAL_MAX_MINUTES + 1):
        with pytest.raises(ValidationError):
            SettingsUpdate(eviction_interval_minutes=bad)
    SettingsUpdate(eviction_interval_minutes=EVICTION_INTERVAL_MAX_MINUTES)  # boundary, inclusive
    SettingsUpdate(eviction_interval_minutes=45.0)  # an ordinary value still works
    for bad in (float("inf"), float("nan"), float("-inf"), EVICTION_INTERVAL_MAX_MINUTES + 1):
        with pytest.raises(ValidationError):
            SettingsUpdate(watchlist_sync_interval_minutes=bad)
    SettingsUpdate(watchlist_sync_interval_minutes=EVICTION_INTERVAL_MAX_MINUTES)
    SettingsUpdate(watchlist_sync_interval_minutes=WATCHLIST_SYNC_INTERVAL_MINUTES_DEFAULT)


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("eviction_interval_minutes", float("inf")),
        ("eviction_interval_minutes", float("nan")),
        ("eviction_interval_minutes", EVICTION_INTERVAL_MAX_MINUTES + 1),
        ("watchlist_sync_interval_minutes", float("inf")),
        ("watchlist_sync_interval_minutes", float("nan")),
        ("watchlist_sync_interval_minutes", EVICTION_INTERVAL_MAX_MINUTES + 1),
        ("disk_pressure_threshold_percent", float("inf")),
        ("disk_pressure_threshold_percent", float("nan")),
        ("eviction_grace_days", EVICTION_GRACE_DAYS_MAX + 1),
        ("log_retention_days", LOG_RETENTION_DAYS_MAX + 1),
        ("log_max_rows", LOG_MAX_ROWS_MAX + 1),
    ],
)
async def test_put_rejects_non_finite_and_overflow_operability_settings(
    client: httpx.AsyncClient,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    field: str,
    bad_value: float,
) -> None:
    """Extends ``test_put_rejects_out_of_range_operability_settings`` (which
    already covers ``150``/``0``/``-1``) with the non-finite and
    finite-but-over-the-new-ceiling cases -- each must 422 AND never persist
    (mirroring ``..._does_not_persist`` above).

    Built as raw wire bytes via the stdlib's OWN ``json.dumps`` (default
    ``allow_nan=True``) rather than httpx's ``json=`` convenience: httpx
    encodes with ``allow_nan=False`` and would raise ``ValueError`` in the
    TEST CLIENT itself before a non-finite value ever reached the server --
    masking the very case under test. The ``Infinity``/``NaN`` tokens this
    produces are exactly what a non-Python client (or hand-crafted request)
    would send, and Starlette's request-side ``json.loads`` (default
    ``allow_nan=True``) accepts them.
    """
    await seed(initialized=True, app_api_key=_API_KEY)
    headers = {"X-Api-Key": _API_KEY, "Content-Type": "application/json"}

    put = await client.put(
        "/api/v1/settings",
        content=json.dumps({field: bad_value}).encode(),
        headers=headers,
    )
    assert put.status_code == 422

    async with sessionmaker_() as session:
        assert await SettingsStore(session).get(field) is None


async def test_put_rejects_wire_level_numeric_overflow_as_non_finite(
    client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """A ``1e400`` numeral folds to ``inf`` at the JSON PARSER (``json.loads``),
    not via Python already having collapsed the float literal at import time --
    distinct from the ``float("inf")`` case above, which never touches the
    number parser at all. Sent as raw wire bytes (not httpx's ``json=`` kwarg,
    which would just re-serialize an already-``inf`` Python float as the
    ``Infinity`` token) so the overflow-during-parse path is genuinely
    exercised."""
    await seed(initialized=True, app_api_key=_API_KEY)
    headers = {"X-Api-Key": _API_KEY, "Content-Type": "application/json"}

    put = await client.put(
        "/api/v1/settings",
        content=b'{"eviction_interval_minutes": 1e400}',
        headers=headers,
    )
    assert put.status_code == 422

    async with sessionmaker_() as session:
        assert await SettingsStore(session).get("eviction_interval_minutes") is None


async def test_put_accepts_upper_bound_operability_settings(
    client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """The new ``le`` bounds must not over-reject their own boundary value."""
    await seed(initialized=True, app_api_key=_API_KEY)
    headers = {"X-Api-Key": _API_KEY}
    update = {
        "eviction_interval_minutes": EVICTION_INTERVAL_MAX_MINUTES,
        "watchlist_sync_interval_minutes": EVICTION_INTERVAL_MAX_MINUTES,
        "eviction_grace_days": EVICTION_GRACE_DAYS_MAX,
        "log_retention_days": LOG_RETENTION_DAYS_MAX,
        "log_max_rows": LOG_MAX_ROWS_MAX,
    }

    put = await client.put("/api/v1/settings", json=update, headers=headers)
    assert put.status_code == 200
    body = put.json()
    assert body["eviction_interval_minutes"] == EVICTION_INTERVAL_MAX_MINUTES
    assert body["watchlist_sync_interval_minutes"] == EVICTION_INTERVAL_MAX_MINUTES
    assert body["eviction_grace_days"] == EVICTION_GRACE_DAYS_MAX
    assert body["log_retention_days"] == LOG_RETENTION_DAYS_MAX
    assert body["log_max_rows"] == LOG_MAX_ROWS_MAX

    got = (await client.get("/api/v1/settings", headers=headers)).json()
    assert got == body

    async with sessionmaker_() as session:
        assert await get_eviction_interval_minutes(session) == EVICTION_INTERVAL_MAX_MINUTES
        assert await get_watchlist_sync_interval_minutes(session) == EVICTION_INTERVAL_MAX_MINUTES
        assert await get_eviction_grace_days(session) == EVICTION_GRACE_DAYS_MAX
        assert await get_log_retention_days(session) == LOG_RETENTION_DAYS_MAX
        assert await get_log_max_rows(session) == LOG_MAX_ROWS_MAX


@pytest.mark.parametrize("stored", ["inf", "nan", "-inf"])
async def test_get_eviction_interval_minutes_falls_back_on_non_finite_stored_value(
    sessionmaker_: SessionMaker,
    caplog: pytest.LogCaptureFixture,
    stored: str,
) -> None:
    async with sessionmaker_() as session:
        await SettingsStore(session).set("eviction_interval_minutes", stored)
        await session.commit()

    async with sessionmaker_() as session:
        with caplog.at_level(logging.WARNING, logger="plex_manager.web.deps"):
            value = await get_eviction_interval_minutes(session)

    assert value == EVICTION_INTERVAL_MINUTES_DEFAULT
    assert "eviction_interval_minutes" in caplog.text


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        # Non-positive: an EXCLUSIVE lower bound with no safe floor to clamp to
        # (a zero/negative sleep hot-spins the loop) -> the default.
        ("0", EVICTION_INTERVAL_MINUTES_DEFAULT),
        ("-5", EVICTION_INTERVAL_MINUTES_DEFAULT),
        # Above the cap: CLAMPED to it (round 3) -- a pre-bounds huge interval
        # meant "sweep almost never"; degrading it to the 30-minute default
        # would make sweeps orders of magnitude more frequent on upgrade.
        (str(EVICTION_INTERVAL_MAX_MINUTES + 1), EVICTION_INTERVAL_MAX_MINUTES),
        ("45", 45.0),
        (str(EVICTION_INTERVAL_MAX_MINUTES), EVICTION_INTERVAL_MAX_MINUTES),
    ],
)
async def test_get_eviction_interval_minutes_degrades_out_of_range_stored_value(
    sessionmaker_: SessionMaker, stored: str, expected: float
) -> None:
    async with sessionmaker_() as session:
        await SettingsStore(session).set("eviction_interval_minutes", stored)
        await session.commit()

    async with sessionmaker_() as session:
        assert await get_eviction_interval_minutes(session) == expected


@pytest.mark.parametrize(
    ("stored", "expected", "degraded"),
    [
        # Negative: the floor (0 = immediately evictable) is the DESTRUCTIVE
        # end of the scale, so never a floor-clamp -> the safe default.
        ("-1", EVICTION_GRACE_DAYS_DEFAULT, True),
        # Above the cap: CLAMPED (round 3) -- a pre-bounds huge grace was a
        # legitimate "never age-evict"; the 30-day default would suddenly make
        # month-old titles evictable on upgrade (data-destructive).
        (str(EVICTION_GRACE_DAYS_MAX + 1), EVICTION_GRACE_DAYS_MAX, True),
        ("14", 14, False),
        ("abc", EVICTION_GRACE_DAYS_DEFAULT, True),
    ],
)
async def test_get_eviction_grace_days_degrades_negative_or_overflow(
    sessionmaker_: SessionMaker,
    caplog: pytest.LogCaptureFixture,
    stored: str,
    expected: int,
    degraded: bool,
) -> None:
    async with sessionmaker_() as session:
        await SettingsStore(session).set("eviction_grace_days", stored)
        await session.commit()

    async with sessionmaker_() as session:
        with caplog.at_level(logging.WARNING, logger="plex_manager.web.deps"):
            value = await get_eviction_grace_days(session)

    assert value == expected
    if degraded:
        assert "eviction_grace_days" in caplog.text


@pytest.mark.parametrize(
    ("stored", "expected", "degraded"),
    [
        # Negative -> default (a future cutoff would wholesale-delete logs; the
        # 0 floor would retain nothing -- both destructive, never clamped to).
        ("-1", LOG_RETENTION_DAYS_DEFAULT, True),
        # Above the cap: CLAMPED (round 3) -- a pre-bounds huge retention meant
        # "keep everything"; the 7-day default would delete logs on upgrade.
        (str(LOG_RETENTION_DAYS_MAX + 1), LOG_RETENTION_DAYS_MAX, True),
        ("14", 14, False),
        ("abc", LOG_RETENTION_DAYS_DEFAULT, True),
    ],
)
async def test_get_log_retention_days_degrades_negative_or_overflow(
    sessionmaker_: SessionMaker,
    caplog: pytest.LogCaptureFixture,
    stored: str,
    expected: int,
    degraded: bool,
) -> None:
    async with sessionmaker_() as session:
        await SettingsStore(session).set("log_retention_days", stored)
        await session.commit()

    async with sessionmaker_() as session:
        with caplog.at_level(logging.WARNING, logger="plex_manager.web.deps"):
            value = await get_log_retention_days(session)

    assert value == expected
    if degraded:
        assert "log_retention_days" in caplog.text


@pytest.mark.parametrize(
    ("stored", "expected", "degraded"),
    [
        # Negative -> default (0 rows kept is the destructive end of the
        # scale -- never what a corrupt value should silently mean).
        ("-1", LOG_MAX_ROWS_DEFAULT, True),
        # Above the cap: CLAMPED (mirrors the day-count settings) -- a
        # pre-bounds huge value meant "keep effectively everything".
        (str(LOG_MAX_ROWS_MAX + 1), LOG_MAX_ROWS_MAX, True),
        ("5000", 5000, False),
        ("abc", LOG_MAX_ROWS_DEFAULT, True),
    ],
)
async def test_get_log_max_rows_degrades_negative_or_overflow(
    sessionmaker_: SessionMaker,
    caplog: pytest.LogCaptureFixture,
    stored: str,
    expected: int,
    degraded: bool,
) -> None:
    async with sessionmaker_() as session:
        await SettingsStore(session).set("log_max_rows", stored)
        await session.commit()

    async with sessionmaker_() as session:
        with caplog.at_level(logging.WARNING, logger="plex_manager.web.deps"):
            value = await get_log_max_rows(session)

    assert value == expected
    if degraded:
        assert "log_max_rows" in caplog.text


async def test_get_settings_does_not_500_on_non_finite_stored_interval(
    client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """Headline AC2: a stored non-finite value must not 500 ``GET /settings`` --
    proves the ``json.dumps(..., allow_nan=False)`` render path is avoided."""
    await seed(initialized=True, app_api_key=_API_KEY)
    async with sessionmaker_() as session:
        await SettingsStore(session).set("eviction_interval_minutes", "inf")
        await session.commit()

    got = await client.get("/api/v1/settings", headers={"X-Api-Key": _API_KEY})
    assert got.status_code == 200
    assert got.json()["eviction_interval_minutes"] is None


@pytest.mark.parametrize(
    ("field", "corrupt"),
    [
        ("disk_pressure_threshold_percent", "not-a-number"),
        ("eviction_grace_days", "1.5"),
        ("log_retention_days", "abc"),
        ("log_max_rows", "abc"),
        ("eviction_enabled", "maybe"),
        ("eviction_interval_minutes", "inf"),
        ("watchlist_sync_interval_minutes", "inf"),
    ],
)
async def test_get_settings_tolerates_corrupt_stored_typed_values(
    client: httpx.AsyncClient,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    field: str,
    corrupt: str,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    async with sessionmaker_() as session:
        store = SettingsStore(session)
        await store.set(field, corrupt)
        await store.set("plex_url", "http://plex.example.com:32400")
        await session.commit()

    got = await client.get("/api/v1/settings", headers={"X-Api-Key": _API_KEY})
    assert got.status_code == 200
    body = got.json()
    assert body[field] is None
    # An unrelated, uncorrupted plaintext field is unaffected by the sanitizer.
    assert body["plex_url"] == "http://plex.example.com:32400"


async def test_get_settings_preserves_valid_typed_values(
    client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """Regression guard: the sanitizer must null ONLY corrupt values, never a
    valid stored one."""
    await seed(initialized=True, app_api_key=_API_KEY)
    valid: dict[str, str] = {
        "disk_pressure_threshold_percent": "88.5",
        "disk_pressure_target_percent": "75.0",
        "eviction_grace_days": "14",
        "eviction_enabled": "false",
        "eviction_proactive_enabled": "true",
        "eviction_interval_minutes": "45.0",
        "watchlist_sync_interval_minutes": "20.0",
        "log_retention_days": "3",
        "log_max_rows": "50000",
    }
    async with sessionmaker_() as session:
        store = SettingsStore(session)
        for key, value in valid.items():
            await store.set(key, value)
        await session.commit()

    got = await client.get("/api/v1/settings", headers={"X-Api-Key": _API_KEY})
    assert got.status_code == 200
    body = got.json()
    assert body["disk_pressure_threshold_percent"] == 88.5
    assert body["disk_pressure_target_percent"] == 75.0
    assert body["eviction_grace_days"] == 14
    assert body["eviction_enabled"] is False
    assert body["eviction_proactive_enabled"] is True
    assert body["eviction_interval_minutes"] == 45.0
    assert body["watchlist_sync_interval_minutes"] == 20.0
    assert body["log_retention_days"] == 3
    assert body["log_max_rows"] == 50000


@pytest.mark.parametrize(
    ("field", "stored", "expected"),
    [
        # Finite, in-shape, JSON-serialisable -- so the unparsable/non-finite
        # checks alone all pass it -- yet each is out of the SettingsUpdate bound
        # the write path enforces. GET must present the EFFECTIVE value the
        # runtime getter resolves it to (round 3): a CLAMPED upper-bound value
        # is displayed (nulling it would make the page claim the default while
        # the loop runs the clamped MAX), a value degraded to the DEFAULT is
        # null (the page then displays that same default). Either way the
        # displayed value is one PUT accepts, so the form always re-saves.
        (
            "eviction_interval_minutes",
            str(EVICTION_INTERVAL_MAX_MINUTES + 1),
            EVICTION_INTERVAL_MAX_MINUTES,
        ),
        ("eviction_interval_minutes", "0", None),  # gt=0 -- exclusive bound, no floor to clamp to
        (
            "watchlist_sync_interval_minutes",
            str(EVICTION_INTERVAL_MAX_MINUTES + 1),
            EVICTION_INTERVAL_MAX_MINUTES,
        ),
        ("watchlist_sync_interval_minutes", "0", None),
        ("disk_pressure_threshold_percent", "150", 100.0),  # clamp: stored >100 meant "never trip"
        ("disk_pressure_target_percent", "-1", None),  # default 80 (<= default threshold 90)
        ("eviction_grace_days", str(EVICTION_GRACE_DAYS_MAX + 1), EVICTION_GRACE_DAYS_MAX),
        ("log_retention_days", str(LOG_RETENTION_DAYS_MAX + 1), LOG_RETENTION_DAYS_MAX),
        ("log_max_rows", str(LOG_MAX_ROWS_MAX + 1), LOG_MAX_ROWS_MAX),
    ],
)
async def test_get_settings_presents_finite_out_of_range_typed_values_as_effective(
    client: httpx.AsyncClient,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    field: str,
    stored: str,
    expected: float | None,
) -> None:
    """GET presents the same effective value the runtime getter resolves an
    out-of-range stored value to: the clamped bound for upper-bound violations
    (upgrade-safe -- see the ``web.deps`` resolver policy), null (-> the default
    the page renders) when the fallback IS the default. Never the raw
    out-of-range number PUT would 422 on the next save."""
    await seed(initialized=True, app_api_key=_API_KEY)
    async with sessionmaker_() as session:
        store = SettingsStore(session)
        await store.set(field, stored)
        await store.set("plex_url", "http://plex.example.com:32400")
        await session.commit()

    got = await client.get("/api/v1/settings", headers={"X-Api-Key": _API_KEY})
    assert got.status_code == 200
    body = got.json()
    assert body[field] == expected
    # An unrelated, in-range plaintext field is untouched by the sanitizer.
    assert body["plex_url"] == "http://plex.example.com:32400"


async def test_get_settings_preserves_boundary_typed_values(
    client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """The inclusive bounds themselves are valid values PUT accepts -- GET must
    preserve a stored value sitting exactly AT a ceiling (or the ge=0 floor),
    never over-null it as if it were out of range."""
    await seed(initialized=True, app_api_key=_API_KEY)
    boundary: dict[str, str] = {
        "disk_pressure_threshold_percent": "100",  # le=100, inclusive
        "disk_pressure_target_percent": "0",  # ge=0, inclusive
        "eviction_interval_minutes": str(EVICTION_INTERVAL_MAX_MINUTES),  # le, inclusive
        "watchlist_sync_interval_minutes": str(EVICTION_INTERVAL_MAX_MINUTES),
        "eviction_grace_days": str(EVICTION_GRACE_DAYS_MAX),
        "log_retention_days": str(LOG_RETENTION_DAYS_MAX),
        "log_max_rows": str(LOG_MAX_ROWS_MAX),
    }
    async with sessionmaker_() as session:
        store = SettingsStore(session)
        for key, value in boundary.items():
            await store.set(key, value)
        await session.commit()

    got = await client.get("/api/v1/settings", headers={"X-Api-Key": _API_KEY})
    assert got.status_code == 200
    body = got.json()
    assert body["disk_pressure_threshold_percent"] == 100.0
    assert body["disk_pressure_target_percent"] == 0.0
    assert body["eviction_interval_minutes"] == EVICTION_INTERVAL_MAX_MINUTES
    assert body["watchlist_sync_interval_minutes"] == EVICTION_INTERVAL_MAX_MINUTES
    assert body["eviction_grace_days"] == EVICTION_GRACE_DAYS_MAX
    assert body["log_retention_days"] == LOG_RETENTION_DAYS_MAX
    assert body["log_max_rows"] == LOG_MAX_ROWS_MAX


# --------------------------------------------------------------------------- #
# Auto-grab timing (issue #150): the cycle interval + per-cycle search cap,
# web-editable exactly like the operability beta knobs above. Defaults equal
# the pre-#150 hardcoded constants (60s / 5 searches), so an unset install
# behaves identically to before this feature existed.
# --------------------------------------------------------------------------- #
def test_settings_update_rejects_out_of_range_auto_grab_timing() -> None:
    """Model-level guard mirroring ``test_settings_update_rejects_non_finite_
    interval``: every non-finite value and anything outside the closed bound
    must fail construction; the boundaries themselves and an ordinary value
    all still construct fine."""
    for bad in (
        float("inf"),
        float("nan"),
        float("-inf"),
        AUTO_GRAB_INTERVAL_SECONDS_MIN - 1,
        AUTO_GRAB_INTERVAL_SECONDS_MAX + 1,
    ):
        with pytest.raises(ValidationError):
            SettingsUpdate(auto_grab_interval_seconds=bad)
    SettingsUpdate(auto_grab_interval_seconds=AUTO_GRAB_INTERVAL_SECONDS_MIN)  # boundary
    SettingsUpdate(auto_grab_interval_seconds=AUTO_GRAB_INTERVAL_SECONDS_MAX)  # boundary
    SettingsUpdate(auto_grab_interval_seconds=90.0)  # an ordinary value still works

    # 1 is now below the floor (issue #332): a cap of 1 wedges whole-season TV.
    for bad_count in (0, 1, -1, AUTO_GRAB_MAX_SEARCHES_PER_CYCLE_MAX + 1):
        with pytest.raises(ValidationError):
            SettingsUpdate(auto_grab_max_searches_per_cycle=bad_count)
    assert AUTO_GRAB_MAX_SEARCHES_PER_CYCLE_MIN == 2
    SettingsUpdate(
        auto_grab_max_searches_per_cycle=AUTO_GRAB_MAX_SEARCHES_PER_CYCLE_MIN
    )  # floor, inclusive
    SettingsUpdate(
        auto_grab_max_searches_per_cycle=AUTO_GRAB_MAX_SEARCHES_PER_CYCLE_MAX
    )  # ceiling, inclusive
    SettingsUpdate(auto_grab_max_searches_per_cycle=10)  # an ordinary value still works


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("auto_grab_interval_seconds", float("inf")),
        ("auto_grab_interval_seconds", float("nan")),
        ("auto_grab_interval_seconds", AUTO_GRAB_INTERVAL_SECONDS_MIN - 1),
        ("auto_grab_interval_seconds", AUTO_GRAB_INTERVAL_SECONDS_MAX + 1),
        ("auto_grab_max_searches_per_cycle", 0),
        # 1 is below the raised floor of 2 (issue #332).
        ("auto_grab_max_searches_per_cycle", 1),
        ("auto_grab_max_searches_per_cycle", AUTO_GRAB_MAX_SEARCHES_PER_CYCLE_MAX + 1),
    ],
)
async def test_put_rejects_out_of_range_auto_grab_timing(
    client: httpx.AsyncClient,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    field: str,
    bad_value: float,
) -> None:
    """Write-time 422 for every out-of-range/non-finite auto-grab timing
    value -- and it must never persist (mirrors the operability-settings
    equivalent above)."""
    await seed(initialized=True, app_api_key=_API_KEY)
    headers = {"X-Api-Key": _API_KEY, "Content-Type": "application/json"}

    put = await client.put(
        "/api/v1/settings",
        content=json.dumps({field: bad_value}).encode(),
        headers=headers,
    )
    assert put.status_code == 422

    async with sessionmaker_() as session:
        assert await SettingsStore(session).get(field) is None


async def test_put_round_trips_auto_grab_timing(
    client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    headers = {"X-Api-Key": _API_KEY}
    update = {
        "auto_grab_interval_seconds": 120.0,
        "auto_grab_max_searches_per_cycle": 10,
    }
    put = await client.put("/api/v1/settings", json=update, headers=headers)
    assert put.status_code == 200
    body = put.json()
    assert body["auto_grab_interval_seconds"] == 120.0
    assert body["auto_grab_max_searches_per_cycle"] == 10

    got = (await client.get("/api/v1/settings", headers=headers)).json()
    assert got == body

    # The typed getters the auto-grab loop actually reads must see the SAME
    # values -- not just a wire-level round trip.
    async with sessionmaker_() as session:
        assert await get_auto_grab_interval_seconds(session) == 120.0
        assert await get_auto_grab_max_searches_per_cycle(session) == 10


async def test_put_accepts_boundary_auto_grab_timing(
    client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """The new bounds must not over-reject their own boundary values."""
    await seed(initialized=True, app_api_key=_API_KEY)
    headers = {"X-Api-Key": _API_KEY}
    update = {
        "auto_grab_interval_seconds": AUTO_GRAB_INTERVAL_SECONDS_MIN,
        "auto_grab_max_searches_per_cycle": AUTO_GRAB_MAX_SEARCHES_PER_CYCLE_MIN,
    }
    put = await client.put("/api/v1/settings", json=update, headers=headers)
    assert put.status_code == 200
    body = put.json()
    assert body["auto_grab_interval_seconds"] == AUTO_GRAB_INTERVAL_SECONDS_MIN
    assert body["auto_grab_max_searches_per_cycle"] == AUTO_GRAB_MAX_SEARCHES_PER_CYCLE_MIN

    update_max = {
        "auto_grab_interval_seconds": AUTO_GRAB_INTERVAL_SECONDS_MAX,
        "auto_grab_max_searches_per_cycle": AUTO_GRAB_MAX_SEARCHES_PER_CYCLE_MAX,
    }
    put_max = await client.put("/api/v1/settings", json=update_max, headers=headers)
    assert put_max.status_code == 200
    body_max = put_max.json()
    assert body_max["auto_grab_interval_seconds"] == AUTO_GRAB_INTERVAL_SECONDS_MAX
    assert body_max["auto_grab_max_searches_per_cycle"] == AUTO_GRAB_MAX_SEARCHES_PER_CYCLE_MAX

    async with sessionmaker_() as session:
        assert await get_auto_grab_interval_seconds(session) == AUTO_GRAB_INTERVAL_SECONDS_MAX
        assert (
            await get_auto_grab_max_searches_per_cycle(session)
            == AUTO_GRAB_MAX_SEARCHES_PER_CYCLE_MAX
        )


async def test_get_settings_default_auto_grab_timing_when_unset(
    client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """Regression guard: unset keys must resolve to the pre-#150 hardcoded
    defaults (60s interval, 5 searches/cycle) -- default behavior unchanged."""
    await seed(initialized=True, app_api_key=_API_KEY)

    got = await client.get("/api/v1/settings", headers={"X-Api-Key": _API_KEY})
    assert got.status_code == 200
    body = got.json()
    assert body["auto_grab_interval_seconds"] is None
    assert body["auto_grab_max_searches_per_cycle"] is None

    async with sessionmaker_() as session:
        assert await get_auto_grab_interval_seconds(session) == AUTO_GRAB_INTERVAL_SECONDS_DEFAULT
        assert (
            await get_auto_grab_max_searches_per_cycle(session)
            == AUTO_GRAB_MAX_SEARCHES_PER_CYCLE_DEFAULT
        )
    assert AUTO_GRAB_INTERVAL_SECONDS_DEFAULT == 60.0
    assert AUTO_GRAB_MAX_SEARCHES_PER_CYCLE_DEFAULT == 5


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        # Below the (closed) floor -- including 0/negative, which would
        # hot-spin the loop -- falls back to the default (no safe floor-clamp).
        ("0", AUTO_GRAB_INTERVAL_SECONDS_DEFAULT),
        ("-5", AUTO_GRAB_INTERVAL_SECONDS_DEFAULT),
        (str(AUTO_GRAB_INTERVAL_SECONDS_MIN - 1), AUTO_GRAB_INTERVAL_SECONDS_DEFAULT),
        # At/above the floor and at/below the ceiling: honored verbatim.
        (str(AUTO_GRAB_INTERVAL_SECONDS_MIN), AUTO_GRAB_INTERVAL_SECONDS_MIN),
        ("90", 90.0),
        (str(AUTO_GRAB_INTERVAL_SECONDS_MAX), AUTO_GRAB_INTERVAL_SECONDS_MAX),
        # Above the ceiling: CLAMPED (a pre-bounds huge interval meant "search
        # almost never"; the 60s default would suddenly search far more often).
        (str(AUTO_GRAB_INTERVAL_SECONDS_MAX + 1), AUTO_GRAB_INTERVAL_SECONDS_MAX),
        # Unparsable / non-finite: default.
        ("abc", AUTO_GRAB_INTERVAL_SECONDS_DEFAULT),
        ("inf", AUTO_GRAB_INTERVAL_SECONDS_DEFAULT),
        ("nan", AUTO_GRAB_INTERVAL_SECONDS_DEFAULT),
    ],
)
async def test_get_auto_grab_interval_seconds_degrades_out_of_range_stored_value(
    sessionmaker_: SessionMaker,
    caplog: pytest.LogCaptureFixture,
    stored: str,
    expected: float,
) -> None:
    async with sessionmaker_() as session:
        await SettingsStore(session).set("auto_grab_interval_seconds", stored)
        await session.commit()

    async with sessionmaker_() as session:
        with caplog.at_level(logging.WARNING, logger="plex_manager.web.deps"):
            value = await get_auto_grab_interval_seconds(session)

    assert value == expected


@pytest.mark.parametrize(
    ("stored", "expected", "degraded"),
    [
        # Below the floor of 2 all DEFAULT (never clamp) -- a 0 cap would
        # silently disable the worker while auto_grab_enabled stays True, and a
        # 1 cap wedges whole-season TV at budget_skipped forever (issue #332).
        ("0", AUTO_GRAB_MAX_SEARCHES_PER_CYCLE_DEFAULT, True),
        ("1", AUTO_GRAB_MAX_SEARCHES_PER_CYCLE_DEFAULT, True),
        ("-1", AUTO_GRAB_MAX_SEARCHES_PER_CYCLE_DEFAULT, True),
        # Above the cap: CLAMPED (a pre-bounds huge value meant "search
        # aggressively", not "revert to the 5-per-cycle default").
        (str(AUTO_GRAB_MAX_SEARCHES_PER_CYCLE_MAX + 1), AUTO_GRAB_MAX_SEARCHES_PER_CYCLE_MAX, True),
        ("10", 10, False),
        (str(AUTO_GRAB_MAX_SEARCHES_PER_CYCLE_MAX), AUTO_GRAB_MAX_SEARCHES_PER_CYCLE_MAX, False),
        # The floor itself (2) is honored, not degraded.
        (str(AUTO_GRAB_MAX_SEARCHES_PER_CYCLE_MIN), AUTO_GRAB_MAX_SEARCHES_PER_CYCLE_MIN, False),
        ("abc", AUTO_GRAB_MAX_SEARCHES_PER_CYCLE_DEFAULT, True),
    ],
)
async def test_get_auto_grab_max_searches_per_cycle_degrades_out_of_range_stored_value(
    sessionmaker_: SessionMaker,
    caplog: pytest.LogCaptureFixture,
    stored: str,
    expected: int,
    degraded: bool,
) -> None:
    async with sessionmaker_() as session:
        await SettingsStore(session).set("auto_grab_max_searches_per_cycle", stored)
        await session.commit()

    async with sessionmaker_() as session:
        with caplog.at_level(logging.WARNING, logger="plex_manager.web.deps"):
            value = await get_auto_grab_max_searches_per_cycle(session)

    assert value == expected
    if degraded:
        assert "auto_grab_max_searches_per_cycle" in caplog.text


async def test_get_settings_presents_corrupt_auto_grab_timing_as_effective_value(
    client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """``GET /settings`` must show exactly what the runtime getter is using --
    the clamped value for an over-ceiling store, never a page-vs-runtime lie."""
    await seed(initialized=True, app_api_key=_API_KEY)
    async with sessionmaker_() as session:
        store = SettingsStore(session)
        await store.set("auto_grab_interval_seconds", str(AUTO_GRAB_INTERVAL_SECONDS_MAX + 100))
        await store.set(
            "auto_grab_max_searches_per_cycle", str(AUTO_GRAB_MAX_SEARCHES_PER_CYCLE_MAX + 5)
        )
        await session.commit()

    got = await client.get("/api/v1/settings", headers={"X-Api-Key": _API_KEY})
    assert got.status_code == 200
    body = got.json()
    assert body["auto_grab_interval_seconds"] == AUTO_GRAB_INTERVAL_SECONDS_MAX
    assert body["auto_grab_max_searches_per_cycle"] == AUTO_GRAB_MAX_SEARCHES_PER_CYCLE_MAX

    async with sessionmaker_() as session:
        assert await get_auto_grab_interval_seconds(session) == AUTO_GRAB_INTERVAL_SECONDS_MAX
        assert (
            await get_auto_grab_max_searches_per_cycle(session)
            == AUTO_GRAB_MAX_SEARCHES_PER_CYCLE_MAX
        )


async def test_get_settings_does_not_500_on_non_finite_stored_auto_grab_interval(
    client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    async with sessionmaker_() as session:
        await SettingsStore(session).set("auto_grab_interval_seconds", "inf")
        await session.commit()

    got = await client.get("/api/v1/settings", headers={"X-Api-Key": _API_KEY})
    assert got.status_code == 200
    assert got.json()["auto_grab_interval_seconds"] is None


# --------------------------------------------------------------------------- #
# Getter <-> GET sanitizer agreement on a corrupt stored value (rounds 2-3).
# For EVERY typed setting the runtime getter (what the eviction/auto-grab loops
# read), the GET /settings sanitizer (what the page shows), and the PUT validator
# must agree on what an out-of-range / unrecognized stored value means -- the page
# must never claim a state the running service isn't in (honesty over silence).
# Round 3 adds the DIRECTIONAL policy: upper-bound violations CLAMP (and GET
# shows the clamped value); lower-bound violations and garbage DEFAULT (and GET
# shows null, which the page renders as that same default).
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("field", "getter", "stored", "expected_value", "expected_get"),
    [
        # >100 CLAMPS to 100 (a pre-bounds 150 meant "never trip"; the default
        # 90 would START evicting at 90% on upgrade) -- and GET shows 100.0,
        # never null (null would render as the default the sweep is NOT using).
        (
            "disk_pressure_threshold_percent",
            get_disk_pressure_threshold_percent,
            "150",
            100.0,
            100.0,
        ),
        # <0 defaults (the 0 floor = permanently "under pressure") -- GET null.
        (
            "disk_pressure_threshold_percent",
            get_disk_pressure_threshold_percent,
            "-1",
            DISK_PRESSURE_THRESHOLD_PERCENT_DEFAULT,
            None,
        ),
        # Target >100 clamps to 100, then the PAIR rule pulls it down to the
        # (defaulted, unset-threshold) 90 -- displayed as 90, the exact value
        # select_evictions runs with.
        (
            "disk_pressure_target_percent",
            get_disk_pressure_target_percent,
            "150",
            DISK_PRESSURE_THRESHOLD_PERCENT_DEFAULT,
            DISK_PRESSURE_THRESHOLD_PERCENT_DEFAULT,
        ),
        # Target <0 defaults to 80, which sits below the default threshold 90 --
        # the effective value IS the default, so GET's null is truthful.
        (
            "disk_pressure_target_percent",
            get_disk_pressure_target_percent,
            "-1",
            DISK_PRESSURE_TARGET_PERCENT_DEFAULT,
            None,
        ),
    ],
)
async def test_corrupt_disk_pressure_percent_getter_and_get_agree(
    client: httpx.AsyncClient,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    caplog: pytest.LogCaptureFixture,
    field: str,
    getter: Callable[[AsyncSession], Awaitable[float]],
    stored: str,
    expected_value: float,
    expected_get: float | None,
) -> None:
    """Round-2 finding 1 + round-3 finding 1: an out-of-``[0, 100]`` stored
    disk-pressure percent degrades DIRECTIONALLY (clamp high / default low), and
    the runtime getter and ``GET /settings`` are asserted together on the same
    stored value so they can never drift: the getter returns the effective value
    (with a WARNING naming the key) AND GET presents exactly that state (the
    clamped number, or null when the effective value IS the default)."""
    await seed(initialized=True, app_api_key=_API_KEY)
    async with sessionmaker_() as session:
        await SettingsStore(session).set(field, stored)
        await session.commit()

    # Runtime getter (what _eviction_tick reads): the effective value + WARNING.
    async with sessionmaker_() as session:
        with caplog.at_level(logging.WARNING, logger="plex_manager.web.deps"):
            value = await getter(session)
    assert value == expected_value
    assert field in caplog.text

    # GET /settings (what the page shows) presents the SAME state the getter
    # resolved -- agreement, never a page claiming a percentage the sweep isn't
    # actually using.
    got = await client.get("/api/v1/settings", headers={"X-Api-Key": _API_KEY})
    assert got.status_code == 200
    assert got.json()[field] == expected_get


async def test_over_cap_grace_days_clamps_instead_of_defaulting(
    client: httpx.AsyncClient,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Round-3 P1: a stored ``eviction_grace_days`` past the cap (``3651`` -- a
    legitimate pre-bounds way to effectively DISABLE age-based eviction) must
    CLAMP to ``EVICTION_GRACE_DAYS_MAX``, not fall back to the 30-day default:
    the default would suddenly make every watched title older than 30 days
    eviction-eligible on upgrade (data-destructive). GET presents the clamped
    value -- nulling it would make the page claim a 30-day grace while
    ``_eviction_tick`` runs the 3650-day MAX."""
    await seed(initialized=True, app_api_key=_API_KEY)
    over_cap = str(EVICTION_GRACE_DAYS_MAX + 1)
    async with sessionmaker_() as session:
        await SettingsStore(session).set("eviction_grace_days", over_cap)
        await session.commit()

    async with sessionmaker_() as session:
        with caplog.at_level(logging.WARNING, logger="plex_manager.web.deps"):
            value = await get_eviction_grace_days(session)
    assert value == EVICTION_GRACE_DAYS_MAX  # the safer LONGER grace, not the default
    assert value != EVICTION_GRACE_DAYS_DEFAULT
    assert "eviction_grace_days" in caplog.text

    got = await client.get("/api/v1/settings", headers={"X-Api-Key": _API_KEY})
    assert got.status_code == 200
    assert got.json()["eviction_grace_days"] == EVICTION_GRACE_DAYS_MAX


async def test_corrupt_target_with_valid_threshold_degrades_to_workable_pair(
    client: httpx.AsyncClient,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Round-3 finding 2: a valid stored ``threshold=50`` with a corrupt
    ``target=-1`` must NOT resolve per-side to the inverted pair ``(50, 80)`` --
    ``select_evictions`` starts ``projected = used_pct`` and stops the moment
    ``projected <= target``, so any used%% in the 50-80 band would trip the
    sweep yet select NOTHING (a silent dead band the form cannot even re-save).
    The pair rule clamps the substituted target down to the threshold:
    ``(50, 50)`` -- the minimal eviction consistent with the operator's own
    threshold -- and getter/GET agree on that exact pair."""
    await seed(initialized=True, app_api_key=_API_KEY)
    async with sessionmaker_() as session:
        store = SettingsStore(session)
        await store.set("disk_pressure_threshold_percent", "50")
        await store.set("disk_pressure_target_percent", "-1")
        await session.commit()

    async with sessionmaker_() as session:
        with caplog.at_level(logging.WARNING, logger="plex_manager.web.deps"):
            threshold = await get_disk_pressure_threshold_percent(session)
            target = await get_disk_pressure_target_percent(session)
    assert threshold == 50.0  # the valid stored side is preserved
    assert target == 50.0  # pair-clamped, never the inverted default 80
    assert "disk_pressure_target_percent" in caplog.text

    # The resolved pair is genuinely WORKABLE: at 60% used (inside what would
    # have been the dead band), select_evictions actually selects the eligible
    # candidate instead of stopping instantly on projected <= target.
    candidate = EvictionCandidate(
        request_id=1,
        media_type="movie",
        title="Stale Movie",
        season=None,
        status="available",
        watched=True,
        last_viewed_at=datetime.now(UTC) - timedelta(days=365),
        keep_forever=False,
        in_flight=False,
        library_path="/library/movies/stale.mkv",
        size_percent=15.0,
    )
    selected = select_evictions(
        [candidate],
        used_pct=60.0,
        threshold_pct=threshold,
        target_pct=target,
        grace_cutoff=datetime.now(UTC) - timedelta(days=30),
    )
    assert selected == [candidate]

    # GET presents the SAME effective pair the sweep runs with -- and (50, 50)
    # is a pair the form can re-save (target <= threshold passes PUT).
    got = await client.get("/api/v1/settings", headers={"X-Api-Key": _API_KEY})
    assert got.status_code == 200
    body = got.json()
    assert body["disk_pressure_threshold_percent"] == 50.0
    assert body["disk_pressure_target_percent"] == 50.0


@pytest.mark.parametrize("stored", [" false ", "False", "FALSE", "false\n"])
@pytest.mark.parametrize(
    ("field", "getter"),
    [
        ("eviction_enabled", get_eviction_enabled),
        ("auto_grab_enabled", get_auto_grab_enabled),
    ],
)
async def test_padded_or_cased_false_bool_still_disables(
    client: httpx.AsyncClient,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    field: str,
    getter: Callable[[AsyncSession], Awaitable[bool]],
    stored: str,
) -> None:
    """Round-3 finding 3: the pre-#142 parser compared ``raw.strip().lower()``,
    so a persisted whitespace-padded/case-variant ``false`` meant DISABLED. The
    ``TypeAdapter`` path must preserve that contract (strip + case-insensitive
    tokens) -- otherwise ``" false "`` becomes "unrecognized" and the ``True``
    default silently re-enables eviction/auto-grab on upgrade. GET agrees:
    the page shows ``false``, never null (which would render the enabled
    default)."""
    await seed(initialized=True, app_api_key=_API_KEY)
    async with sessionmaker_() as session:
        await SettingsStore(session).set(field, stored)
        await session.commit()

    async with sessionmaker_() as session:
        assert await getter(session) is False  # still disabled, not defaulted to True

    got = await client.get("/api/v1/settings", headers={"X-Api-Key": _API_KEY})
    assert got.status_code == 200
    assert got.json()[field] is False


async def test_padded_true_bool_still_enables_proactive(
    client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """The strip/case contract cuts both ways: a padded ``" TRUE "`` stored for
    a default-FALSE setting (proactive eviction) keeps meaning enabled -- and
    GET shows ``true`` rather than null (which would render the disabled
    default)."""
    await seed(initialized=True, app_api_key=_API_KEY)
    async with sessionmaker_() as session:
        await SettingsStore(session).set("eviction_proactive_enabled", " TRUE ")
        await session.commit()

    async with sessionmaker_() as session:
        assert await get_eviction_proactive_enabled(session) is True

    got = await client.get("/api/v1/settings", headers={"X-Api-Key": _API_KEY})
    assert got.status_code == 200
    assert got.json()["eviction_proactive_enabled"] is True


@pytest.mark.parametrize(
    ("field", "getter", "default"),
    [
        ("eviction_enabled", get_eviction_enabled, EVICTION_ENABLED_DEFAULT),
        ("auto_grab_enabled", get_auto_grab_enabled, AUTO_GRAB_ENABLED_DEFAULT),
    ],
)
async def test_unrecognized_bool_getter_and_get_agree_on_default(
    client: httpx.AsyncClient,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    caplog: pytest.LogCaptureFixture,
    field: str,
    getter: Callable[[AsyncSession], Awaitable[bool]],
    default: bool,
) -> None:
    """Finding 2: an UNRECOGNIZED stored boolean (``"maybe"``) used to be silently
    read as ``False`` by the loop while ``GET /settings`` nulled it (→ the page
    showed the default ``True``) -- the loop was OFF while the page said ON. The
    getter must now fall back to the default (with a WARNING) for an unrecognized
    token, matching the GET sanitizer's null (→ default display) exactly."""
    await seed(initialized=True, app_api_key=_API_KEY)
    async with sessionmaker_() as session:
        await SettingsStore(session).set(field, "maybe")
        await session.commit()

    async with sessionmaker_() as session:
        with caplog.at_level(logging.WARNING, logger="plex_manager.web.deps"):
            value = await getter(session)
    assert value is default
    assert field in caplog.text

    got = await client.get("/api/v1/settings", headers={"X-Api-Key": _API_KEY})
    assert got.status_code == 200
    assert got.json()[field] is None


async def test_bool_getter_honors_explicit_stored_false(sessionmaker_: SessionMaker) -> None:
    """The unrecognized-value fallback must NOT swallow a legitimately stored
    ``"false"``: it is a recognized token, so an operator who turned a loop OFF
    keeps it OFF (``False``), never resurrected to the ``True`` default. Guards the
    fix from over-degrading a valid negative into the enabled default."""
    async with sessionmaker_() as session:
        store = SettingsStore(session)
        await store.set("eviction_enabled", "false")
        await store.set("auto_grab_enabled", "false")
        await store.set("eviction_proactive_enabled", "false")
        await session.commit()

    async with sessionmaker_() as session:
        assert await get_eviction_enabled(session) is False
        assert await get_auto_grab_enabled(session) is False
        assert await get_eviction_proactive_enabled(session) is False


def test_typed_setting_key_groups_cover_all_typed_response_fields() -> None:
    """Parity guard (mirrors ``test_every_known_setting_key_has_a_response_and_
    update_field`` above): the union of the three ``_*_TYPED_SETTING_KEYS``
    tuples plus the collection group the sanitizer walks must equal every
    ``KNOWN_SETTING_KEYS`` entry whose ``SettingsResponse`` field is NOT a plain
    ``str`` -- i.e. every numeric/bool/collection setting -- and none may be a secret. Otherwise a
    future typed field could be added to the response without ever being
    routed through ``_sanitize_typed_settings``, silently reopening the 500."""
    from typing import get_args

    typed_keys = (
        set(_FLOAT_TYPED_SETTING_KEYS)
        | set(_INT_TYPED_SETTING_KEYS)
        | set(_BOOL_TYPED_SETTING_KEYS)
        | set(_COLLECTION_TYPED_SETTING_KEYS)
    )
    expected = {
        key
        for key in KNOWN_SETTING_KEYS
        if str not in get_args(SettingsResponse.model_fields[key].annotation)
    }
    assert typed_keys == expected
    assert typed_keys.isdisjoint(SECRET_SETTING_KEYS)


# --------------------------------------------------------------------------- #
# Health-probe cache invalidation on credential save (issue #93)
# --------------------------------------------------------------------------- #
_HEALTH_HEADERS = {"X-Api-Key": _API_KEY}


async def test_put_settings_invalidates_health_cache_for_the_written_subsystem(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    """A credential save must not leave the OLD cached probe sitting around for
    up to ``SUBSYSTEM_PROBE_TTL_SECONDS`` -- the very next ``GET /health`` after
    fixing (or breaking) a subsystem must re-probe it, not echo the pre-edit
    result. Uses prowlarr (not plex) fields: a changed prowlarr_url/api_key never
    enters the Plex repoint verification ladder (:func:`_verify_plex_repoint`),
    so this stays a plain settings save with no live upstream probe -- the
    honest, simplest path to exercise the cache-invalidation contract itself."""
    await seed(initialized=True, app_api_key=_API_KEY)
    # Warm every subsystem's cache entry (all unconfigured -> "not_configured").
    warm = await client.get("/api/v1/ops/health", headers=_HEALTH_HEADERS)
    assert warm.status_code == 200
    cache = app.state.health_cache
    assert cache.get("prowlarr") is not None
    assert cache.get("tmdb") is not None

    put = await client.put(
        "/api/v1/settings",
        json={"prowlarr_url": "http://prowlarr.local", "prowlarr_api_key": "tok"},
        headers=_HEALTH_HEADERS,
    )
    assert put.status_code == 200

    # The edited subsystem's cache entry is gone...
    assert cache.get("prowlarr") is None
    # ...but this is a TARGETED invalidation -- an unrelated subsystem's still-
    # valid cached probe must survive untouched.
    assert cache.get("tmdb") is not None


async def test_put_settings_with_no_credential_fields_leaves_health_cache_alone(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    """A PUT that writes only a non-credential field (e.g. an eviction knob) must
    invalidate nothing -- no subsystem's config actually changed."""
    await seed(initialized=True, app_api_key=_API_KEY)
    warm = await client.get("/api/v1/ops/health", headers=_HEALTH_HEADERS)
    assert warm.status_code == 200
    cache = app.state.health_cache
    assert cache.get("plex") is not None

    put = await client.put(
        "/api/v1/settings",
        json={"eviction_enabled": False},
        headers=_HEALTH_HEADERS,
    )
    assert put.status_code == 200
    assert cache.get("plex") is not None


async def test_put_settings_secret_mask_round_trip_does_not_invalidate_health_cache(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """A secret field sent back as the ``"***"`` redaction mask is a documented
    no-op write (see ``put_settings_endpoint``) -- it must not count as "this
    subsystem's credentials changed" and invalidate a still-valid cached probe."""
    await seed(initialized=True, app_api_key=_API_KEY)
    async with sessionmaker_() as session:
        store = SettingsStore(session)
        await store.set("plex_url", "http://plex.local")
        await store.set("plex_token", "tok")
        await session.commit()

    warm = await client.get("/api/v1/ops/health", headers=_HEALTH_HEADERS)
    assert warm.status_code == 200
    cache = app.state.health_cache
    assert cache.get("plex") is not None

    # Sending back ONLY the masked secret (e.g. a FE form that re-submits every
    # field it displays, including the "***" it read from GET) writes nothing
    # for plex -- SettingsStore.set is never called for this field.
    put = await client.put(
        "/api/v1/settings",
        json={"plex_token": "***"},
        headers=_HEALTH_HEADERS,
    )
    assert put.status_code == 200
    assert cache.get("plex") is not None


async def test_put_settings_does_not_invalidate_health_cache_on_failed_save(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    """A rejected (422) write must leave any still-valid cached probe exactly as
    it was -- a failed save changed nothing, so nothing should be invalidated."""
    await seed(initialized=True, app_api_key=_API_KEY)
    warm = await client.get("/api/v1/ops/health", headers=_HEALTH_HEADERS)
    assert warm.status_code == 200
    cache = app.state.health_cache
    assert cache.get("plex") is not None

    put = await client.put(
        "/api/v1/settings",
        json={"plex_url": "not a url at all"},
        headers=_HEALTH_HEADERS,
    )
    assert put.status_code == 422
    assert cache.get("plex") is not None


# --------------------------------------------------------------------------- #
# App-key reveal / rotate (issue #28's OAuth-deferral hardening)
# --------------------------------------------------------------------------- #
async def test_reveal_app_key_returns_the_current_key(
    client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    response = await client.get("/api/v1/settings/app-key", headers={"X-Api-Key": _API_KEY})
    assert response.status_code == 200
    assert response.json() == {"app_api_key": _API_KEY}


async def test_reveal_app_key_requires_authentication(
    client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    response = await client.get("/api/v1/settings/app-key")
    assert response.status_code == 401


async def test_reveal_app_key_response_is_never_cached(
    client: httpx.AsyncClient, seed: SeedFn
) -> None:
    """Issue #208: a caching intermediary must never persist the plaintext key."""
    await seed(initialized=True, app_api_key=_API_KEY)
    response = await client.get("/api/v1/settings/app-key", headers={"X-Api-Key": _API_KEY})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store, private"
    assert response.headers["pragma"] == "no-cache"


async def test_normal_settings_response_is_unaffected_by_no_store_headers(
    client: httpx.AsyncClient, seed: SeedFn
) -> None:
    """Negative case: the no-store treatment is specific to the plaintext key routes."""
    await seed(initialized=True, app_api_key=_API_KEY)
    response = await client.get("/api/v1/settings", headers=_HEALTH_HEADERS)
    assert response.status_code == 200
    assert "cache-control" not in response.headers
    assert "pragma" not in response.headers


async def test_generic_secret_replacement_rewrites_durable_and_live_logs(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    old_secret = "old-generic-secret-for-rotation"  # noqa: S105 -- fixture credential
    new_secret = "new-generic-secret-for-rotation"  # noqa: S105 -- fixture credential
    await seed(initialized=True, app_api_key=_API_KEY)
    async with sessionmaker_() as session:
        await SettingsStore(session).set("tmdb_api_key", old_secret)
        session.add(
            LogEvent(
                level="INFO",
                logger="test",
                message=f"durable {old_secret}",
                context_json={old_secret: {"nested": [old_secret]}},
            )
        )
        await session.commit()

    handler = log_capture_service.LogCaptureHandler()
    handler.secret_values = frozenset({old_secret})
    record = log_capture_service.CapturedLogRecord(
        created_at=datetime.now(UTC),
        level="INFO",
        logger="test",
        message=f"live {old_secret}",
        context={"nested": old_secret},
    )
    handler.queue.put_nowait(record)
    handler.ring_buffer.append(record)
    app.state.log_handler = handler

    response = await client.put(
        "/api/v1/settings", json={"tmdb_api_key": new_secret}, headers=_HEALTH_HEADERS
    )

    assert response.status_code == 200
    async with sessionmaker_() as session:
        row = (await session.execute(select(LogEvent))).scalar_one()
        assert old_secret not in row.message
        assert old_secret not in json.dumps(row.context_json)
    queued = handler.queue.get_nowait()
    assert old_secret not in queued.message
    assert old_secret not in json.dumps(queued.context)
    ring = handler.snapshot_tail(1)[0]
    assert old_secret not in ring.message
    assert old_secret not in json.dumps(ring.context)
    assert new_secret in handler.secret_values
    assert old_secret not in handler.secret_values


async def test_generic_two_secret_replacement_rewrites_both_retired_values(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    old_tmdb = "old-tmdb-secret-for-rotation"
    old_prowlarr = "old-prowlarr-secret-for-rotation"
    new_tmdb = "new-tmdb-secret-for-rotation"
    new_prowlarr = "new-prowlarr-secret-for-rotation"
    await seed(initialized=True, app_api_key=_API_KEY)
    async with sessionmaker_() as session:
        store = SettingsStore(session)
        await store.set("tmdb_api_key", old_tmdb)
        await store.set("prowlarr_api_key", old_prowlarr)
        session.add(
            LogEvent(
                level="INFO",
                logger="test",
                message=f"durable {old_tmdb} {old_prowlarr}",
                context_json={old_tmdb: [old_prowlarr]},
            )
        )
        await session.commit()
    handler = log_capture_service.LogCaptureHandler()
    record = log_capture_service.CapturedLogRecord(
        created_at=datetime.now(UTC),
        level="INFO",
        logger="test",
        message=f"live {old_tmdb} {old_prowlarr}",
        context={old_tmdb: [old_prowlarr]},
    )
    handler.queue.put_nowait(record)
    handler.ring_buffer.append(record)
    app.state.log_handler = handler

    response = await client.put(
        "/api/v1/settings",
        json={"tmdb_api_key": new_tmdb, "prowlarr_api_key": new_prowlarr},
        headers=_HEALTH_HEADERS,
    )

    assert response.status_code == 200
    async with sessionmaker_() as session:
        row = (await session.execute(select(LogEvent))).scalar_one()
        persisted = await SettingsStore(session).secret_values()
    for retired in (old_tmdb, old_prowlarr):
        assert retired not in row.message
        assert retired not in json.dumps(row.context_json)
        assert retired not in handler.queue.get_nowait().message
        handler.queue.put_nowait(handler.snapshot_tail(1)[0])
        assert retired not in handler.snapshot_tail(1)[0].message
        assert retired not in persisted
    assert {new_tmdb, new_prowlarr}.issubset(handler.secret_values)


async def test_generic_first_secret_replacement_uses_rotation_completion(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    first_secret = "first-generic-secret-for-rotation"  # noqa: S105 -- fixture credential
    await seed(initialized=True, app_api_key=_API_KEY)
    handler = log_capture_service.LogCaptureHandler()
    app.state.log_handler = handler

    response = await client.put(
        "/api/v1/settings", json={"tmdb_api_key": first_secret}, headers=_HEALTH_HEADERS
    )

    assert response.status_code == 200
    assert first_secret in handler.secret_values


async def test_masked_generic_secret_does_not_start_a_rotation(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plex_manager.web.routers import settings as settings_router

    old_secret = "unchanged-generic-secret"  # noqa: S105 -- fixture credential
    await seed(initialized=True, app_api_key=_API_KEY)
    async with sessionmaker_() as session:
        await SettingsStore(session).set("tmdb_api_key", old_secret)
        session.add(LogEvent(level="INFO", logger="test", message=old_secret))
        await session.commit()
    called = False

    async def must_not_rewrite(*_args: object, **_kwargs: object) -> int:
        nonlocal called
        called = True
        return 0

    monkeypatch.setattr(settings_router, "_rewrite_before_secret_replacement", must_not_rewrite)
    response = await client.put(
        "/api/v1/settings", json={"tmdb_api_key": "***"}, headers=_HEALTH_HEADERS
    )

    assert response.status_code == 200
    assert called is False
    async with sessionmaker_() as session:
        assert await SettingsStore(session).get("tmdb_api_key") == old_secret
        assert (await session.execute(select(LogEvent))).scalar_one().message == old_secret


async def test_generic_secret_clear_rewrites_retired_value(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    retired_value = "generic-value-cleared-at-runtime"
    await seed(initialized=True, app_api_key=_API_KEY)
    async with sessionmaker_() as session:
        await SettingsStore(session).set("tmdb_api_key", retired_value)
        session.add(
            LogEvent(
                level="INFO",
                logger="test",
                message=retired_value,
                context_json={retired_value: [retired_value]},
            )
        )
        await session.commit()
    handler = log_capture_service.LogCaptureHandler()
    record = log_capture_service.CapturedLogRecord(
        created_at=datetime.now(UTC),
        level="INFO",
        logger="test",
        message=retired_value,
        context={retired_value: retired_value},
    )
    handler.queue.put_nowait(record)
    handler.ring_buffer.append(record)
    app.state.log_handler = handler

    response = await client.put(
        "/api/v1/settings", json={"tmdb_api_key": ""}, headers=_HEALTH_HEADERS
    )

    assert response.status_code == 200
    async with sessionmaker_() as session:
        assert await SettingsStore(session).get("tmdb_api_key") == ""
        row = (await session.execute(select(LogEvent))).scalar_one()
    for rendered in (
        row.message,
        json.dumps(row.context_json),
        handler.queue.get_nowait().message,
        str(handler.snapshot_tail(1)[0]),
    ):
        assert retired_value not in rendered


@pytest.mark.parametrize("failure", ["rewrite", "commit"])
async def test_generic_secret_replacement_failure_restores_database_and_handler_exactly(
    failure: str,
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plex_manager.web.routers import settings as settings_router

    old_secret = "generic-secret-before-failure"  # noqa: S105 -- fixture credential
    new_secret = "generic-secret-after-failure"  # noqa: S105 -- fixture credential
    await seed(initialized=True, app_api_key=_API_KEY)
    async with sessionmaker_() as session:
        await SettingsStore(session).set("tmdb_api_key", old_secret)
        session.add(
            LogEvent(
                level="INFO", logger="test", message=old_secret, context_json={"secret": old_secret}
            )
        )
        await session.commit()
    handler = log_capture_service.LogCaptureHandler()
    handler.secret_values = frozenset({old_secret})
    record = log_capture_service.CapturedLogRecord(
        created_at=datetime.now(UTC),
        level="INFO",
        logger="test",
        message=old_secret,
        context={"secret": old_secret},
    )
    handler.queue.put_nowait(record)
    handler.ring_buffer.append(record)
    app.state.log_handler = handler
    before_queue = handler.queue.get_nowait()
    handler.queue.put_nowait(before_queue)
    before_ring = tuple(handler.ring_buffer)
    if failure == "rewrite":

        async def failing_rewrite(*_args: object, **_kwargs: object) -> int:
            raise RuntimeError("rewrite failed")

        monkeypatch.setattr(settings_router, "_rewrite_before_secret_replacement", failing_rewrite)
    else:

        async def failing_commit(self: AsyncSession) -> None:
            raise RuntimeError("commit failed")

        monkeypatch.setattr(AsyncSession, "commit", failing_commit)

    if failure == "rewrite":
        with pytest.raises(RuntimeError):
            await client.put(
                "/api/v1/settings", json={"tmdb_api_key": new_secret}, headers=_HEALTH_HEADERS
            )
    else:
        response = await client.put(
            "/api/v1/settings", json={"tmdb_api_key": new_secret}, headers=_HEALTH_HEADERS
        )
        assert response.status_code == 503

    assert handler.secret_values == frozenset({old_secret})
    assert handler.queue.get_nowait() == before_queue
    assert tuple(handler.ring_buffer) == before_ring
    async with sessionmaker_() as session:
        assert await SettingsStore(session).get("tmdb_api_key") == old_secret
        row = (await session.execute(select(LogEvent))).scalar_one()
        assert row.message == old_secret
        assert row.context_json == {"secret": old_secret}


async def test_rotate_app_key_rewrites_durable_and_live_logs(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    async with sessionmaker_() as session:
        session.add(
            LogEvent(
                level="INFO",
                logger="test",
                message=f"durable {_API_KEY}",
                context_json={"nested": [_API_KEY]},
            )
        )
        await session.commit()
    handler = log_capture_service.LogCaptureHandler()
    record = log_capture_service.CapturedLogRecord(
        created_at=datetime.now(UTC),
        level="INFO",
        logger="test",
        message=f"live {_API_KEY}",
        context={"nested": _API_KEY},
    )
    handler.queue.put_nowait(record)
    handler.ring_buffer.append(record)
    app.state.log_handler = handler

    response = await client.post("/api/v1/settings/app-key/rotate", headers={"X-Api-Key": _API_KEY})

    assert response.status_code == 200
    async with sessionmaker_() as session:
        row = (await session.execute(select(LogEvent))).scalar_one()
        assert _API_KEY not in row.message
        assert _API_KEY not in json.dumps(row.context_json)
    assert _API_KEY not in handler.queue.get_nowait().message
    assert _API_KEY not in handler.snapshot_tail(1)[0].message


async def test_rotate_app_key_cancelled_before_commit_launch_fails_safe(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex #399 round 3: ``POST /app-key/rotate`` returns the NEW key exactly
    once -- committing a rotation for a caller that already disconnected would
    revoke the old recovery credential with the replacement undeliverable,
    locking a recovery-only operator out. A cancellation already pending when
    the boundary reaches its point of no return (the pre-commit checkpoint)
    must land BEFORE the commit unit launches and fail SAFE: no key change, no
    historical rewrite committed, snapshot restored, the OLD key still the
    live credential. (The mirror ordering -- cancellation arriving once the
    commit unit has genuinely started -- is pinned by the facet-1 test
    ``test_cancel_during_commit_still_runs_the_completion_sweep``: the unit
    stays atomic and the sweep still runs; both orderings must coexist.)"""
    from plex_manager.web.routers import settings as settings_router

    await seed(initialized=True, app_api_key=_API_KEY)
    async with sessionmaker_() as session:
        session.add(LogEvent(level="INFO", logger="test", message=f"durable {_API_KEY}"))
        await session.commit()
    handler = log_capture_service.LogCaptureHandler()
    handler.secret_values = frozenset({_API_KEY})
    app.state.log_handler = handler

    entered = asyncio.Event()
    release = asyncio.Event()
    real_checkpoint = settings_router._cancellation_checkpoint  # pyright: ignore[reportPrivateUsage]

    async def paused_checkpoint() -> None:
        entered.set()
        await _wait_for_event(release)
        await real_checkpoint()

    monkeypatch.setattr(settings_router, "_cancellation_checkpoint", paused_checkpoint)

    task = asyncio.create_task(
        client.post("/api/v1/settings/app-key/rotate", headers={"X-Api-Key": _API_KEY})
    )
    await _wait_for_event(entered)
    task.cancel()
    await assert_task_raises(task, asyncio.CancelledError)

    # Fail SAFE: nothing durable landed, the snapshot is restored...
    assert handler.secret_values == frozenset({_API_KEY})
    assert handler.retiring_values == frozenset()
    async with sessionmaker_() as session:
        system = await load_system_settings(session)
        row = (await session.execute(select(LogEvent))).scalar_one()
    assert system is not None and system.app_api_key == _API_KEY
    assert row.message == f"durable {_API_KEY}"  # historical rewrite rolled back
    # ...and the OLD key is still the live credential for a later attempt.
    status_response = await client.get(
        "/api/v1/settings/app-key/status", headers={"X-Api-Key": _API_KEY}
    )
    assert status_response.status_code == 200


async def test_rotate_app_key_cancelled_during_commit_still_runs_post_commit_invalidations(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex #399 round 4 (finding 1): a cancellation that arrives once the
    rotation commit is durable must NOT skip the caller's must-run post-commit
    invalidations. Before the ``on_committed`` hook they lived after
    ``async with secret_rotation(...)``, and the boundary's remembered-
    cancellation re-raise happened before control ever returned there -- the
    key rotation was committed, but every already-open stream authenticated by
    the OLD key stayed live until its lease caught up. The boundary now runs
    them itself (synchronously, before honoring the remembered cancellation),
    so a disconnected caller still tears down old-key streams and publishes
    the access change."""
    from plex_manager.web.routers import settings as settings_router

    await seed(initialized=True, app_api_key=_API_KEY)
    async with sessionmaker_() as session:
        session.add(LogEvent(level="INFO", logger="test", message=f"durable {_API_KEY}"))
        await session.commit()
    handler = log_capture_service.LogCaptureHandler()
    handler.secret_values = frozenset({_API_KEY})
    app.state.log_handler = handler

    invalidations: list[str] = []
    real_close = settings_router.close_realtime_streams
    real_publish = settings_router.publish_realtime

    def spy_close(*args: object, **kwargs: object) -> None:
        invalidations.append(f"close:{kwargs.get('reason')}")
        real_close(*args, **kwargs)  # type: ignore[arg-type]

    def spy_publish(*args: object, **kwargs: object) -> None:
        invalidations.append(f"publish:{kwargs.get('reason')}")
        real_publish(*args, **kwargs)  # type: ignore[arg-type]

    real_rewrite = settings_router._rewrite_before_secret_replacement  # pyright: ignore[reportPrivateUsage]
    real_commit = AsyncSession.commit
    rewrite_finished = False
    committed = asyncio.Event()
    release = asyncio.Event()

    async def mark_rewrite(session: AsyncSession, values: frozenset[str]) -> int:
        nonlocal rewrite_finished
        result = await real_rewrite(session, values)
        rewrite_finished = True
        return result

    async def paused_commit(self: AsyncSession) -> None:
        await real_commit(self)
        if rewrite_finished:
            # The boundary's commit is now DURABLE. Pause so the test can
            # cancel the request while the protected unit is mid-flight.
            committed.set()
            await _wait_for_event(release)

    monkeypatch.setattr(settings_router, "close_realtime_streams", spy_close)
    monkeypatch.setattr(settings_router, "publish_realtime", spy_publish)
    monkeypatch.setattr(settings_router, "_rewrite_before_secret_replacement", mark_rewrite)
    monkeypatch.setattr(AsyncSession, "commit", paused_commit)

    task = asyncio.create_task(
        client.post("/api/v1/settings/app-key/rotate", headers={"X-Api-Key": _API_KEY})
    )
    await _wait_for_event(committed)
    task.cancel()
    # Deliver the cancellation while the commit unit is still paused, so the
    # request genuinely ends cancelled with the rotation already durable.
    for _ in range(5):
        await asyncio.sleep(0)
    release.set()
    await assert_task_raises(task, asyncio.CancelledError)

    # The rotation landed durably AND the invalidations still ran.
    async with sessionmaker_() as session:
        system = await load_system_settings(session)
    assert system is not None and system.app_api_key not in (None, _API_KEY)
    assert "close:app_key_rotated" in invalidations
    assert "publish:app_key_rotated" in invalidations


async def test_revoke_app_key_rewrites_durable_and_live_logs(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    async with sessionmaker_() as session:
        session.add(
            LogEvent(
                level="INFO",
                logger="test",
                message=f"durable {_API_KEY}",
                context_json={_API_KEY: {"nested": [_API_KEY]}},
            )
        )
        await session.commit()
    handler = log_capture_service.LogCaptureHandler()
    record = log_capture_service.CapturedLogRecord(
        created_at=datetime.now(UTC),
        level="INFO",
        logger="test",
        message=f"live {_API_KEY}",
        context={_API_KEY: [_API_KEY]},
    )
    handler.queue.put_nowait(record)
    handler.ring_buffer.append(record)
    app.state.log_handler = handler

    response = await client.delete("/api/v1/settings/app-key", headers={"X-Api-Key": _API_KEY})

    assert response.status_code == 204
    async with sessionmaker_() as session:
        system = await load_system_settings(session)
        row = (await session.execute(select(LogEvent))).scalar_one()
    assert system is not None and system.app_api_key is None
    for record in (row, handler.queue.get_nowait(), handler.snapshot_tail(1)[0]):
        assert _API_KEY not in record.message
        assert _API_KEY not in json.dumps(
            record.context_json if isinstance(record, LogEvent) else record.context
        )


@pytest.mark.parametrize("path", ["/api/v1/settings/app-key/rotate", "/api/v1/settings/app-key"])
async def test_app_key_rewrite_failure_restores_handler_and_recovery_session(
    path: str,
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plex_manager.web.routers import settings as settings_router

    await seed(initialized=True, app_api_key=_API_KEY)
    await _recovery_session_cookies(app, tag=f"failure-{path.rsplit('/', 1)[-1]}")
    async with sessionmaker_() as session:
        session.add(LogEvent(level="INFO", logger="test", message=_API_KEY))
        await session.commit()
    handler = log_capture_service.LogCaptureHandler()
    handler.secret_values = frozenset({_API_KEY})
    record = log_capture_service.CapturedLogRecord(
        created_at=datetime.now(UTC), level="INFO", logger="test", message=_API_KEY, context=None
    )
    handler.queue.put_nowait(record)
    handler.ring_buffer.append(record)
    app.state.log_handler = handler
    before_queue = handler.queue.get_nowait()
    handler.queue.put_nowait(before_queue)
    before_ring = tuple(handler.ring_buffer)

    async def failing_rewrite(*_args: object, **_kwargs: object) -> int:
        raise RuntimeError("rewrite failed")

    monkeypatch.setattr(settings_router, "_rewrite_before_secret_replacement", failing_rewrite)
    with pytest.raises(RuntimeError):
        await (client.post if path.endswith("rotate") else client.delete)(
            path, headers={"X-Api-Key": _API_KEY}
        )

    assert handler.secret_values == frozenset({_API_KEY})
    assert handler.queue.get_nowait() == before_queue
    assert tuple(handler.ring_buffer) == before_ring
    async with sessionmaker_() as session:
        system = await load_system_settings(session)
        row = (await session.execute(select(LogEvent))).scalar_one()
    assert system is not None and system.app_api_key == _API_KEY
    assert row.message == _API_KEY
    assert (
        await _recovery_session_revoked(sessionmaker_, tag=f"failure-{path.rsplit('/', 1)[-1]}")
        is False
    )


async def test_app_key_commit_failure_restores_handler_and_recovery_session(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    await _recovery_session_cookies(app, tag="commit-failure")
    handler = log_capture_service.LogCaptureHandler()
    handler.secret_values = frozenset({_API_KEY})
    record = log_capture_service.CapturedLogRecord(
        created_at=datetime.now(UTC), level="INFO", logger="test", message=_API_KEY, context=None
    )
    handler.queue.put_nowait(record)
    handler.ring_buffer.append(record)
    app.state.log_handler = handler
    before_queue = handler.queue.get_nowait()
    handler.queue.put_nowait(before_queue)
    before_ring = tuple(handler.ring_buffer)

    async def fail_key_mutation_commit(self: AsyncSession) -> None:
        raise RuntimeError("commit failed")

    monkeypatch.setattr(AsyncSession, "commit", fail_key_mutation_commit)
    response = await client.post("/api/v1/settings/app-key/rotate", headers={"X-Api-Key": _API_KEY})

    assert response.status_code == 503
    assert handler.secret_values == frozenset({_API_KEY})
    assert handler.queue.get_nowait() == before_queue
    assert tuple(handler.ring_buffer) == before_ring
    async with sessionmaker_() as session:
        system = await load_system_settings(session)
    assert system is not None and system.app_api_key == _API_KEY
    assert await _recovery_session_revoked(sessionmaker_, tag="commit-failure") is False


async def test_rotate_app_key_mints_a_new_key_and_invalidates_the_old_one(
    client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)

    rotate = await client.post("/api/v1/settings/app-key/rotate", headers={"X-Api-Key": _API_KEY})
    assert rotate.status_code == 200
    new_key = rotate.json()["app_api_key"]
    assert new_key != _API_KEY
    assert len(new_key) > 20  # matches setup.complete()'s token_urlsafe(32) shape

    # The OLD key (still in this request's headers) is immediately invalid --
    # rotation replaces the single live key, so every other device holding the
    # old value is locked out at once.
    old_key_check = await client.get("/api/v1/settings", headers={"X-Api-Key": _API_KEY})
    assert old_key_check.status_code == 401

    # The NEW key works.
    new_key_check = await client.get("/api/v1/settings", headers={"X-Api-Key": new_key})
    assert new_key_check.status_code == 200


async def test_rotate_app_key_response_is_never_cached(
    client: httpx.AsyncClient, seed: SeedFn
) -> None:
    """Issue #208: the rotate response carries the freshly minted plaintext key too."""
    await seed(initialized=True, app_api_key=_API_KEY)
    response = await client.post("/api/v1/settings/app-key/rotate", headers={"X-Api-Key": _API_KEY})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store, private"
    assert response.headers["pragma"] == "no-cache"


async def test_rotate_app_key_requires_authentication(
    client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    response = await client.post("/api/v1/settings/app-key/rotate")
    assert response.status_code == 401


async def test_rotate_app_key_cas_rejects_racing_rotation_with_stale_key(
    client: httpx.AsyncClient,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two rotations racing with the SAME old key must not clobber each other.

    Both requests clear ``require_api_key`` against the old stored key before
    either commits; the compare-and-swap must turn the loser into an honest 409
    instead of silently overwriting the winner's freshly minted key (which would
    leave the winner's client displaying an already-dead key).

    The race is simulated deterministically: while THIS request is in flight (it
    has already authenticated against the old key), a concurrent rotation commits
    a new key in a separate session. The handler's in-transaction re-read must
    observe that change and bail out 409, leaving the concurrent winner's key
    intact.
    """
    await seed(initialized=True, app_api_key=_API_KEY)

    from plex_manager.web.routers import settings as settings_router

    real_ensure = settings_router.ensure_system_settings
    winner_key = "winner-rotation-committed-mid-flight-0123456789"
    state = {"raced": False}

    async def racing_ensure(session: AsyncSession) -> object:
        row = await real_ensure(session)
        if not state["raced"]:
            # Fire exactly once: a competing rotation commits its own new key on a
            # separate session AFTER this request authenticated against the old key
            # but BEFORE it writes its own.
            state["raced"] = True
            async with sessionmaker_() as other:
                other_row = await real_ensure(other)
                other_row.app_api_key = winner_key
                await other.commit()
        return row

    monkeypatch.setattr(settings_router, "ensure_system_settings", racing_ensure)

    losing = await client.post("/api/v1/settings/app-key/rotate", headers={"X-Api-Key": _API_KEY})
    assert losing.status_code == 409
    assert losing.json()["detail"] == "app_key_changed"

    # The concurrent winner's key survived -- the loser did not overwrite it.
    async with sessionmaker_() as session:
        system = await load_system_settings(session)
        assert system is not None
        assert system.app_api_key == winner_key


async def test_rotate_app_key_lock_serializes_two_concurrent_rotations(
    client: httpx.AsyncClient,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two genuinely concurrent rotations with the SAME old key: exactly one wins.

    This exercises the window the previous CAS test could not: BOTH requests are
    forced into the handler and past authentication (against the old key) BEFORE
    EITHER commits, so a bare check-then-act would let both re-read the old key,
    both pass the compare, and both 200 -- the second silently clobbering the
    first's freshly minted key. The rendezvous is a barrier planted in
    ``ensure_system_settings`` (which runs BEFORE ``_rotate_lock`` is acquired):
    neither request can proceed to the locked read-modify-write until both have
    entered the handler, guaranteeing the both-in-flight-before-any-commit
    interleaving. ``_rotate_lock`` must then serialize them into one 200 + one 409;
    without the lock this assertion fails with two 200s.
    """
    await seed(initialized=True, app_api_key=_API_KEY)

    from plex_manager.web.routers import settings as settings_router

    real_ensure = settings_router.ensure_system_settings
    # Barrier(2): the first request to reach it blocks until the second arrives,
    # so BOTH are inside the handler (authenticated, nothing committed yet) before
    # either advances to acquire _rotate_lock.
    both_in_handler = asyncio.Barrier(2)

    async def rendezvous_ensure(session: AsyncSession) -> object:
        row = await real_ensure(session)
        # Timeout so a regression that never lets both sides in (or a broken lock)
        # fails loudly instead of hanging the suite.
        await asyncio.wait_for(both_in_handler.wait(), timeout=5.0)
        return row

    monkeypatch.setattr(settings_router, "ensure_system_settings", rendezvous_ensure)

    from plex_manager.web import middleware as middleware_module

    monkeypatch.setattr(
        middleware_module,
        "_MAINTENANCE_EXCLUDED_PREFIXES",
        (
            *middleware_module._MAINTENANCE_EXCLUDED_PREFIXES,  # pyright: ignore[reportPrivateUsage]
            "/api/v1/settings/app-key",
        ),
    )

    first, second = await asyncio.gather(
        client.post("/api/v1/settings/app-key/rotate", headers={"X-Api-Key": _API_KEY}),
        client.post("/api/v1/settings/app-key/rotate", headers={"X-Api-Key": _API_KEY}),
    )

    # Exactly one 200 (the winner) and one 409 (the loser) -- never two 200s.
    assert sorted([first.status_code, second.status_code]) == [200, 409]
    winner, loser = (first, second) if first.status_code == 200 else (second, first)
    assert loser.json()["detail"] == "app_key_changed"

    # The stored key is the winner's minted key, and the OLD key is dead -- the
    # loser did not clobber the winner with a second, unreturned key.
    new_key = winner.json()["app_api_key"]
    assert new_key != _API_KEY
    async with sessionmaker_() as session:
        system = await load_system_settings(session)
        assert system is not None
        assert system.app_api_key == new_key


async def test_rotate_app_key_cas_returns_409_when_stored_key_already_advanced(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    """A rotation that authenticated against a now-superseded key gets 409, not 200.

    A first rotation commits and advances the stored key. A second request that
    had already cleared auth against the OLD key (simulated by stubbing the auth
    dependency, exactly what a same-old-key racer would have done before the first
    committed) reaches the handler with that stale key; the CAS must reject it 409
    and must not clobber the first rotation's result.
    """
    await seed(initialized=True, app_api_key=_API_KEY)

    first = await client.post("/api/v1/settings/app-key/rotate", headers={"X-Api-Key": _API_KEY})
    assert first.status_code == 200
    new_key = first.json()["app_api_key"]

    # The racing second request already passed require_api_key against the old key.
    # ``require_api_key`` now returns an ``AuthContext`` (was ``None``); the stale
    # racer authenticated via the static key HEADER, so mirror that method AND
    # credential source (``via_api_key_header=True``) so the rotate handler takes
    # the header-baseline CAS path and the guard sees an admin.
    app.dependency_overrides[require_api_key] = lambda: AuthContext(
        method=AuthMethod.api_key, is_admin=True, via_api_key_header=True
    )
    try:
        stale = await client.post(
            "/api/v1/settings/app-key/rotate", headers={"X-Api-Key": _API_KEY}
        )
    finally:
        del app.dependency_overrides[require_api_key]

    assert stale.status_code == 409
    assert stale.json()["detail"] == "app_key_changed"

    async with sessionmaker_() as session:
        system = await load_system_settings(session)
        assert system is not None
        assert system.app_api_key == new_key


async def test_settings_store_set_recovers_from_concurrent_first_write(
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #91: two concurrent first writes to a brand-new key must not 500.

    ``SettingsStore.set()`` used to be a plain read-then-insert with no conflict
    recovery: two sessions racing to set the SAME never-before-seen key (e.g.
    two concurrent setup submissions both writing ``movies_root`` for the first
    time) could both pass the "row not found" check and both attempt an INSERT,
    and the loser's flush would raise an uncaught ``IntegrityError``.

    Simulated deterministically exactly like
    ``test_create_request_recovers_from_active_dedup_conflict``: a "winner"
    session commits the first row for the key, then this session's OWN initial
    ``_row`` lookup is stubbed to report "not found" once (as if it ran before
    the winner's insert became visible), forcing it down the insert path so its
    flush collides on the real unique index.
    """
    async with sessionmaker_() as winner_session:
        await SettingsStore(winner_session).set("movies_root", "/winner")
        await winner_session.commit()

    real_row = SettingsStore._row  # pyright: ignore[reportPrivateUsage]
    calls = {"n": 0}

    async def racing_row(self: SettingsStore, key: str) -> Setting | None:
        if calls["n"] == 0:
            calls["n"] = 1
            return None
        return await real_row(self, key)

    monkeypatch.setattr(SettingsStore, "_row", racing_row)

    async with sessionmaker_() as loser_session:
        # Must not raise IntegrityError (or anything else) out of set().
        await SettingsStore(loser_session).set("movies_root", "/loser")
        await loser_session.commit()

    async with sessionmaker_() as session:
        rows = (
            (await session.execute(select(Setting).where(Setting.key == "movies_root")))
            .scalars()
            .all()
        )
    assert len(rows) == 1  # never two rows for the same key
    # The recovered row was updated with THIS call's value, not silently
    # discarded in favor of the winner's -- an upsert, not a "first write wins".
    assert rows[0].value == "/loser"


async def test_settings_store_set_collision_recovery_does_not_lose_earlier_writes(
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A same-transaction collision on a LATER key must not roll back an EARLIER
    key's already-flushed write in the same request.

    Both ``PUT /settings`` (looping over ``body.model_fields_set``) and
    ``POST /setup/complete`` (writing ~12 keys) call ``set()`` repeatedly inside
    ONE transaction, committing once at the end. The first-write-collision
    recovery used to call a full ``session.rollback()``, which discards the
    ENTIRE transaction, not just the failed INSERT -- so a collision on a LATER
    key in the loop silently dropped every EARLIER key this same request had
    already written, returning 200 with quietly missing data. That directly
    violates "honesty over silence": the original loud, retryable 500 got traded
    for a silent partial write. The recovery must instead be scoped to a
    SAVEPOINT around just the failing INSERT, leaving prior flushes in the same
    transaction intact.
    """
    async with sessionmaker_() as winner_session:
        await SettingsStore(winner_session).set("tv_root", "/winner")
        await winner_session.commit()

    real_row = SettingsStore._row  # pyright: ignore[reportPrivateUsage]
    calls = {"n": 0}

    async def racing_row(self: SettingsStore, key: str) -> Setting | None:
        if key == "tv_root" and calls["n"] == 0:
            calls["n"] = 1
            return None
        return await real_row(self, key)

    monkeypatch.setattr(SettingsStore, "_row", racing_row)

    async with sessionmaker_() as session:
        store = SettingsStore(session)
        # Earlier key in the SAME transaction: a brand-new key, flushed
        # successfully and still only pending (not yet committed).
        await store.set("movies_root", "/same-txn")
        # Later key in the SAME transaction collides with the winner's
        # already-committed row and must recover without nuking the above.
        await store.set("tv_root", "/loser")
        await session.commit()

    async with sessionmaker_() as session:
        # The earlier write must have survived the later key's collision
        # recovery -- not silently discarded despite the 200/commit.
        assert await SettingsStore(session).get("movies_root") == "/same-txn"
        # The upsert semantics for the colliding key itself are unaffected.
        assert await SettingsStore(session).get("tv_root") == "/loser"


async def test_settings_store_set_if_absent_creates_and_returns_value_when_missing(
    sessionmaker_: SessionMaker,
) -> None:
    """The genuine first create: the key persists this call's value and returns
    it, so the caller can adopt the return value unconditionally."""
    async with sessionmaker_() as session:
        returned = await SettingsStore(session).set_if_absent("movies_root", "/minted")
        await session.commit()

    assert returned == "/minted"
    async with sessionmaker_() as session:
        assert await SettingsStore(session).get("movies_root") == "/minted"


async def test_settings_store_set_if_absent_returns_existing_value_without_overwrite(
    sessionmaker_: SessionMaker,
) -> None:
    """An already-persisted value is returned untouched and the candidate is
    discarded — create-once, where ``set()`` would have upserted the candidate."""
    async with sessionmaker_() as session:
        await SettingsStore(session).set("movies_root", "/original")
        await session.commit()

    async with sessionmaker_() as session:
        returned = await SettingsStore(session).set_if_absent("movies_root", "/candidate")
        await session.commit()

    assert returned == "/original"
    async with sessionmaker_() as session:
        assert await SettingsStore(session).get("movies_root") == "/original"


async def test_settings_store_set_if_absent_adopts_winner_on_concurrent_first_create(
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The create-once counterpart of
    ``test_settings_store_set_recovers_from_concurrent_first_write``: same race,
    OPPOSITE resolution. ``set()`` resolves its first-write collision to
    last-write-wins (right for preferences); ``set_if_absent()`` must resolve it
    to FIRST-write-wins (right for minted identities like the plex.tv client
    identifier, which must never rotate): the loser's recovery ADOPTS the
    winner's committed value — returning it to the caller — and never overwrites
    it. Simulated identically: the winner commits first, then the loser's own
    initial ``_row`` lookup is stubbed to report "not found" once (as if it ran
    before the winner's insert became visible), forcing it down the insert path
    so its flush collides on the real unique index.
    """
    async with sessionmaker_() as winner_session:
        await SettingsStore(winner_session).set("movies_root", "/winner")
        await winner_session.commit()

    real_row = SettingsStore._row  # pyright: ignore[reportPrivateUsage]
    calls = {"n": 0}

    async def racing_row(self: SettingsStore, key: str) -> Setting | None:
        if calls["n"] == 0:
            calls["n"] = 1
            return None
        return await real_row(self, key)

    monkeypatch.setattr(SettingsStore, "_row", racing_row)

    async with sessionmaker_() as loser_session:
        # Must not raise — and must hand back the WINNER's value, not its own.
        returned = await SettingsStore(loser_session).set_if_absent("movies_root", "/loser")
        await loser_session.commit()

    assert returned == "/winner"  # the loser converged on the persisted identity
    async with sessionmaker_() as session:
        rows = (
            (await session.execute(select(Setting).where(Setting.key == "movies_root")))
            .scalars()
            .all()
        )
    assert len(rows) == 1  # never two rows for the same key
    # The winner's value survived intact: create-once means the stored identity
    # never rotates, no matter how many racers arrive after it persisted.
    assert rows[0].value == "/winner"


async def test_settings_store_set_if_absent_routes_secret_keys_to_encrypted_column(
    sessionmaker_: SessionMaker,
) -> None:
    """Secret routing parity with ``set()``: a secret key created via
    ``set_if_absent`` lands in the encrypted column, never plaintext at rest."""
    plaintext = "first-write-tmdb-secret"
    async with sessionmaker_() as session:
        returned = await SettingsStore(session).set_if_absent("tmdb_api_key", plaintext)
        await session.commit()
    assert returned == plaintext

    # Inspect the raw columns, bypassing the EncryptedStr decryption layer.
    async with sessionmaker_() as session:
        raw_value, raw_encrypted, is_secret = (
            await session.execute(
                text(
                    "SELECT value, encrypted_value, is_secret "
                    "FROM settings WHERE key = 'tmdb_api_key'"
                )
            )
        ).one()
    assert bool(is_secret) is True
    assert raw_value is None  # the plaintext column is never used for a secret
    assert raw_encrypted is not None
    assert plaintext not in raw_encrypted  # at-rest value is ciphertext, not plaintext


# --------------------------------------------------------------------------- #
# SettingsStore.secret_values (issue #268) — the DECRYPTED value set fed to
# logsafe.redact_known_secrets, at both the log-capture handler (via
# web/app.py's _log_drain_loop) and the /ops/logs* read boundaries.
# --------------------------------------------------------------------------- #
async def test_secret_values_returns_every_configured_secret_decrypted(
    sessionmaker_: SessionMaker,
) -> None:
    async with sessionmaker_() as session:
        store = SettingsStore(session)
        await store.set("plex_token", "fake-plex-token-1")
        await store.set("prowlarr_api_key", "fake-prowlarr-key-1")
        await store.set("qbittorrent_password", "fake-qbt-password-1")
        await store.set("tmdb_api_key", "fake-tmdb-key-1")
        await session.commit()

    async with sessionmaker_() as session:
        values = await SettingsStore(session).secret_values()
    assert values == frozenset(
        {
            "fake-plex-token-1",
            "fake-prowlarr-key-1",
            "fake-qbt-password-1",
            "fake-tmdb-key-1",
        }
    )


async def test_secret_values_omits_unset_secrets(sessionmaker_: SessionMaker) -> None:
    """Only the secrets actually configured contribute a value -- an unset one
    is silently absent, never an empty string."""
    async with sessionmaker_() as session:
        await SettingsStore(session).set("prowlarr_api_key", "fake-prowlarr-key-2")
        await session.commit()

    async with sessionmaker_() as session:
        values = await SettingsStore(session).secret_values()
    assert values == frozenset({"fake-prowlarr-key-2"})


async def test_secret_values_is_empty_with_nothing_configured(
    sessionmaker_: SessionMaker,
) -> None:
    async with sessionmaker_() as session:
        values = await SettingsStore(session).secret_values()
    assert values == frozenset()


async def test_secret_values_never_returns_a_plaintext_setting(
    sessionmaker_: SessionMaker,
) -> None:
    """Only :data:`SECRET_SETTING_KEYS` are queried -- an ordinary plaintext
    setting (a url, a username) never leaks into the value-based redaction set,
    which would otherwise risk redacting innocuous config values out of logs."""
    async with sessionmaker_() as session:
        store = SettingsStore(session)
        await store.set("prowlarr_url", "http://prowlarr.local")
        await store.set("qbittorrent_username", "admin")
        await session.commit()

    async with sessionmaker_() as session:
        values = await SettingsStore(session).secret_values()
    assert values == frozenset()


async def test_secret_values_includes_the_system_app_api_key(
    seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """The recovery/automation break-glass ``X-Api-Key`` credential
    (``SystemSettings.app_api_key``) lives in a SEPARATE table from the
    generic ``settings`` key/value store -- it must still be folded into the
    value-based redaction set, or a bare occurrence of that credential in a
    log line (e.g. echoed back in an error message) would sail past the
    value-based pass entirely."""
    await seed(initialized=True, app_api_key="fake-app-api-key-1234567890")

    async with sessionmaker_() as session:
        values = await SettingsStore(session).secret_values()
    assert "fake-app-api-key-1234567890" in values


async def test_secret_values_combines_the_app_api_key_with_settings_secrets(
    seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """Both sources contribute to the SAME set -- the app api key does not
    replace or shadow the generic settings-table secrets."""
    await seed(initialized=True, app_api_key="fake-app-api-key-abcdefghij")
    async with sessionmaker_() as session:
        await SettingsStore(session).set("prowlarr_api_key", "fake-prowlarr-key-3")
        await session.commit()

    async with sessionmaker_() as session:
        values = await SettingsStore(session).secret_values()
    assert values == frozenset({"fake-app-api-key-abcdefghij", "fake-prowlarr-key-3"})


async def test_secret_values_omits_app_api_key_when_unset(
    sessionmaker_: SessionMaker,
) -> None:
    """No ``SystemSettings`` row at all (a fresh, pre-setup install) must not
    raise or contribute a value -- exactly the same honest-absence contract
    the generic settings query already keeps."""
    async with sessionmaker_() as session:
        values = await SettingsStore(session).secret_values()
    assert values == frozenset()


# --------------------------------------------------------------------------- #
# secret_values -- issue #292, items 5-6: the stored Plex OAuth token
# (User.encrypted_plex_token) and the optional PLEX_MANAGER_SETUP_TOKEN were
# both missing from the value-based redaction set.
# --------------------------------------------------------------------------- #
async def test_secret_values_includes_a_stored_user_plex_oauth_token(
    sessionmaker_: SessionMaker,
) -> None:
    """A signed-in Plex owner's per-user OAuth token
    (:class:`~plex_manager.models.User`.``encrypted_plex_token``) lives in a
    SEPARATE table from the generic ``settings`` store and is never a
    ``Setting`` row keyed by :data:`SECRET_SETTING_KEYS` -- without folding it
    in, an echoed/logged occurrence of that token (reused for the user's own
    Plex resource/ownership calls) would sail past the value-based pass
    entirely."""
    async with sessionmaker_() as session:
        session.add(
            User(
                plex_id=1001,
                username="owner-1",
                permissions=1,
                encrypted_plex_token="fake-user-oauth-token-1",  # noqa: S106
            )
        )
        await session.commit()

    async with sessionmaker_() as session:
        values = await SettingsStore(session).secret_values()
    assert "fake-user-oauth-token-1" in values


async def test_secret_values_includes_every_users_plex_oauth_token(
    sessionmaker_: SessionMaker,
) -> None:
    """Every signed-in user's token contributes -- the redaction set is not
    limited to a single (e.g. the first-claiming owner's) account."""
    async with sessionmaker_() as session:
        session.add_all(
            [
                User(
                    plex_id=1002,
                    username="owner-2",
                    permissions=1,
                    encrypted_plex_token="fake-user-oauth-token-2",  # noqa: S106
                ),
                User(
                    plex_id=1003,
                    username="member-3",
                    permissions=0,
                    encrypted_plex_token="fake-user-oauth-token-3",  # noqa: S106
                ),
            ]
        )
        await session.commit()

    async with sessionmaker_() as session:
        values = await SettingsStore(session).secret_values()
    assert "fake-user-oauth-token-2" in values
    assert "fake-user-oauth-token-3" in values


async def test_secret_values_omits_a_users_token_left_unset(
    sessionmaker_: SessionMaker,
) -> None:
    """A user row with no stored token (the degenerate token-less case) must
    not raise or contribute a value -- the same honest-absence contract as
    every other source."""
    async with sessionmaker_() as session:
        session.add(
            User(plex_id=1004, username="owner-4", permissions=1, encrypted_plex_token=None)
        )
        await session.commit()

    async with sessionmaker_() as session:
        values = await SettingsStore(session).secret_values()
    assert values == frozenset()


async def test_secret_values_includes_the_configured_setup_token(
    sessionmaker_: SessionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The optional pre-init hardening ``PLEX_MANAGER_SETUP_TOKEN`` is
    environment-sourced, never a DB row -- without folding it in directly,
    the one place it is deliberately printed to the operator (the startup
    setup-URL hint, issue #65/#294) would have nothing to redact it with when
    it is echoed into the durable, exportable log store."""
    monkeypatch.setenv("PLEX_MANAGER_SETUP_TOKEN", "fake-setup-token-12345")
    get_settings.cache_clear()

    async with sessionmaker_() as session:
        values = await SettingsStore(session).secret_values()
    assert "fake-setup-token-12345" in values


async def test_secret_values_omits_the_setup_token_when_unset(
    sessionmaker_: SessionMaker,
) -> None:
    async with sessionmaker_() as session:
        values = await SettingsStore(session).secret_values()
    assert values == frozenset()


async def test_secret_values_combines_all_sources(
    sessionmaker_: SessionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every source -- generic settings-table secrets, the break-glass app API
    key, every user's Plex OAuth token, and the setup token -- lands in the
    SAME set; none shadows or replaces another."""
    monkeypatch.setenv("PLEX_MANAGER_SETUP_TOKEN", "fake-setup-token-combo")
    get_settings.cache_clear()
    async with sessionmaker_() as session:
        await SettingsStore(session).set("prowlarr_api_key", "fake-prowlarr-key-combo")
        session.add(
            User(
                plex_id=1005,
                username="owner-5",
                permissions=1,
                encrypted_plex_token="fake-user-oauth-token-combo",  # noqa: S106
            )
        )
        await session.commit()

    async with sessionmaker_() as session:
        values = await SettingsStore(session).secret_values()
    assert values == frozenset(
        {
            "fake-prowlarr-key-combo",
            "fake-user-oauth-token-combo",
            "fake-setup-token-combo",
        }
    )


async def test_rotate_app_key_cas_rejects_rotate_after_concurrent_revoke(
    client: httpx.AsyncClient,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The revoke null-hole: a rotate that OBSERVED a key must not resurrect it.

    A rotation authenticates against the live key, then a concurrent REVOKE clears
    the stored key to NULL before this request writes. The old CAS skipped its
    check whenever the stored key was null ('nothing to compare, just mint') and so
    minted a fresh key — silently undoing the revoke. The fixed CAS treats a null
    stored key as the genuine first-key generate ONLY when this request also
    observed null; here it observed a non-null key, so it must 409 and leave the
    revoke standing (no key resurrected).
    """
    await seed(initialized=True, app_api_key=_API_KEY)

    from plex_manager.web.routers import settings as settings_router

    real_ensure = settings_router.ensure_system_settings
    state = {"raced": False}

    async def revoking_ensure(session: AsyncSession) -> object:
        row = await real_ensure(session)
        if not state["raced"]:
            # Fire once: a competing REVOKE clears the key on a separate session
            # AFTER this rotation authenticated against it but BEFORE it writes.
            state["raced"] = True
            async with sessionmaker_() as other:
                other_row = await real_ensure(other)
                other_row.app_api_key = None
                await other.commit()
        return row

    monkeypatch.setattr(settings_router, "ensure_system_settings", revoking_ensure)

    losing = await client.post("/api/v1/settings/app-key/rotate", headers={"X-Api-Key": _API_KEY})
    assert losing.status_code == 409
    assert losing.json()["detail"] == "app_key_changed"

    # The revoke held: no key was resurrected by the losing rotation.
    async with sessionmaker_() as session:
        system = await load_system_settings(session)
        assert system is not None
        assert system.app_api_key is None


async def _admin_session_cookies(
    app: FastAPI, *, plex_id: int, tag: str, plex_oauth_token: str | None = _SEED_OAUTH_TOKEN
) -> tuple[dict[str, str], dict[str, str]]:
    """Mint a live ADMIN (owner) browser session; returns (cookies, csrf headers).

    ``plex_oauth_token`` seeds ``User.encrypted_plex_token`` by default — Plex
    sign-in always stores the account token, and the repoint ownership check
    reads it. Pass ``None`` only to model the degenerate token-less row.
    """
    token = f"admin-session-{tag}"
    csrf = f"csrf-{tag}"
    async with app.state.sessionmaker() as session:
        user = User(
            plex_id=plex_id,
            username=f"owner-{tag}",
            permissions=1,
            encrypted_plex_token=plex_oauth_token,
        )
        session.add(user)
        await session.flush()
        session.add(
            AuthSession(
                user_id=user.id,
                token_hash=hash_session_token(token),
                expires_at=datetime.now(UTC) + timedelta(days=1),
                last_seen_at=datetime.now(UTC),
            )
        )
        await session.commit()
    return {"plexmgr.session": token, "plexmgr.csrf": csrf}, {"X-CSRF-Token": csrf}


async def test_rotate_app_key_cas_serializes_two_concurrent_session_rotations(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two Plex-SESSION admins rotating concurrently: exactly one wins, one 409s.

    Session auth never presents an ``X-Api-Key`` header, so the CAS compares the
    stored key against the value each request's session LOADED at auth time. Both
    requests are forced into the handler (each having observed the OLD key)
    before either commits — the same rendezvous as the api-key barrier test
    above. Were the CAS still gated to ``AuthMethod.api_key`` (the wave-2
    finding), both would 200 and the second would silently clobber the first's
    freshly minted key — the exact dead-key race the CAS exists to prevent.
    """
    await seed(initialized=True, app_api_key=_API_KEY)
    cookies_a, headers_a = await _admin_session_cookies(app, plex_id=9101, tag="a")
    cookies_b, headers_b = await _admin_session_cookies(app, plex_id=9102, tag="b")

    from plex_manager.web.routers import settings as settings_router

    real_ensure = settings_router.ensure_system_settings
    both_in_handler = asyncio.Barrier(2)

    async def rendezvous_ensure(session: AsyncSession) -> object:
        row = await real_ensure(session)
        await asyncio.wait_for(both_in_handler.wait(), timeout=5.0)
        return row

    monkeypatch.setattr(settings_router, "ensure_system_settings", rendezvous_ensure)
    # A CONTENDED asyncio.Lock binds to the event loop of the test that first
    # contended it (the api-key barrier test above); this test runs in its own
    # loop, so give it a fresh, loop-local lock — same serialization semantics.
    monkeypatch.setattr(settings_router, "_rotate_lock", asyncio.Lock())

    from plex_manager.web import middleware as middleware_module

    monkeypatch.setattr(
        middleware_module,
        "_MAINTENANCE_EXCLUDED_PREFIXES",
        (
            *middleware_module._MAINTENANCE_EXCLUDED_PREFIXES,  # pyright: ignore[reportPrivateUsage]
            "/api/v1/settings/app-key",
        ),
    )

    first, second = await asyncio.gather(
        client.post("/api/v1/settings/app-key/rotate", cookies=cookies_a, headers=headers_a),
        client.post("/api/v1/settings/app-key/rotate", cookies=cookies_b, headers=headers_b),
    )

    # Exactly one 200 (the winner) and one honest 409 (the loser) — never two 200s.
    assert sorted([first.status_code, second.status_code]) == [200, 409]
    winner, loser = (first, second) if first.status_code == 200 else (second, first)
    assert loser.json()["detail"] == "app_key_changed"

    # The stored key is the winner's minted key — the loser did not clobber it
    # with a second, unreturned key (which would strand the winner's client on a
    # dead key).
    new_key = winner.json()["app_api_key"]
    assert new_key != _API_KEY
    async with sessionmaker_() as session:
        system = await load_system_settings(session)
        assert system is not None
        assert system.app_api_key == new_key


async def _recovery_session_cookies(
    app: FastAPI, *, tag: str
) -> tuple[dict[str, str], dict[str, str]]:
    """Mint a live recovery/break-glass session (``AuthSession.user_id IS NULL``).

    The cookie a valid ``X-Api-Key`` exchange yields (``POST /auth/api-key``): an
    admin-authority session with NO Plex identity. Returns ``(cookies, csrf)`` in
    the same shape as :func:`_admin_session_cookies`.
    """
    token = f"recovery-session-{tag}"
    csrf = f"recovery-csrf-{tag}"
    async with app.state.sessionmaker() as session:
        session.add(
            AuthSession(
                user_id=None,
                token_hash=hash_session_token(token),
                expires_at=datetime.now(UTC) + timedelta(days=1),
                last_seen_at=datetime.now(UTC),
            )
        )
        await session.commit()
    return {"plexmgr.session": token, "plexmgr.csrf": csrf}, {"X-CSRF-Token": csrf}


async def _recovery_session_states(sessionmaker_: SessionMaker) -> list[bool]:
    """Return, for every recovery session (``user_id IS NULL``), whether it is
    revoked (``revoked_at is not None``)."""
    async with sessionmaker_() as session:
        rows = (
            (await session.execute(select(AuthSession).where(AuthSession.user_id.is_(None))))
            .scalars()
            .all()
        )
    return [row.revoked_at is not None for row in rows]


async def test_rotate_app_key_via_recovery_cookie_succeeds(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    """A break-glass admin signed in through the recovery-key exchange can ROTATE
    the very key they used (issue #293 finding 3).

    The recovery session reports ``AuthMethod.api_key`` but, being a cookie
    credential, sends no ``X-Api-Key`` header. The pre-fix CAS sourced ``observed``
    from that absent header whenever ``auth.method is api_key``, so it compared the
    stored key against ``None`` and ALWAYS 409'd — a break-glass admin could never
    manage the key from the browser (a north-star-#1 violation). The fixed CAS
    treats a header-less recovery session like a session caller.
    """
    await seed(initialized=True, app_api_key=_API_KEY)
    cookies, csrf = await _recovery_session_cookies(app, tag="rotate")

    rotate = await client.post("/api/v1/settings/app-key/rotate", cookies=cookies, headers=csrf)

    assert rotate.status_code == 200
    new_key = rotate.json()["app_api_key"]
    assert new_key != _API_KEY
    async with sessionmaker_() as session:
        system = await load_system_settings(session)
        assert system is not None
        assert system.app_api_key == new_key


async def test_revoke_app_key_via_recovery_cookie_succeeds(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    """A break-glass admin can also REVOKE the key they signed in with (finding 3):
    the same header-less-recovery CAS path, on the delete endpoint."""
    await seed(initialized=True, app_api_key=_API_KEY)
    cookies, csrf = await _recovery_session_cookies(app, tag="revoke")

    revoke = await client.delete("/api/v1/settings/app-key", cookies=cookies, headers=csrf)

    assert revoke.status_code == 204
    async with sessionmaker_() as session:
        system = await load_system_settings(session)
        assert system is not None
        assert system.app_api_key is None


async def test_rotate_app_key_revokes_active_recovery_sessions(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    """Rotating the app key invalidates every live recovery-cookie session (issue
    #293 finding 4): a break-glass cookie must not outlive the key it was minted
    from. Rotated here by a DIRECT ``X-Api-Key`` caller so the recovery session
    under test is a bystander, not the rotator."""
    await seed(initialized=True, app_api_key=_API_KEY)
    await _recovery_session_cookies(app, tag="bystander")
    assert await _recovery_session_states(sessionmaker_) == [False]

    rotate = await client.post("/api/v1/settings/app-key/rotate", headers={"X-Api-Key": _API_KEY})
    assert rotate.status_code == 200

    # The recovery session was revoked alongside the key change.
    assert await _recovery_session_states(sessionmaker_) == [True]


async def test_revoke_app_key_revokes_active_recovery_sessions(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    """Revoking the app key invalidates every live recovery-cookie session (finding
    4): with the key gone, the break-glass cookie must lose admin access too — the
    same immediate lockout a direct ``X-Api-Key`` caller already gets."""
    await seed(initialized=True, app_api_key=_API_KEY)
    cookies, _csrf = await _recovery_session_cookies(app, tag="revoked-bystander")
    assert await _recovery_session_states(sessionmaker_) == [False]

    revoke = await client.delete("/api/v1/settings/app-key", headers={"X-Api-Key": _API_KEY})
    assert revoke.status_code == 204

    assert await _recovery_session_states(sessionmaker_) == [True]
    # The revoked recovery cookie no longer authenticates a later request.
    later = await client.get("/api/v1/settings", cookies=cookies)
    assert later.status_code == 401


async def _recovery_session_revoked(sessionmaker_: SessionMaker, *, tag: str) -> bool:
    """Whether the recovery session minted by :func:`_recovery_session_cookies` for
    ``tag`` is revoked (``revoked_at is not None``)."""
    token_hash = hash_session_token(f"recovery-session-{tag}")
    async with sessionmaker_() as session:
        row = (
            await session.execute(select(AuthSession).where(AuthSession.token_hash == token_hash))
        ).scalar_one()
    return row.revoked_at is not None


async def test_rotate_via_recovery_cookie_keeps_actor_and_revokes_other_recovery_sessions(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    """P1 (issue #293): a break-glass admin who ROTATES from their OWN recovery cookie
    keeps that one session; every OTHER recovery session is still revoked.

    The rotate response hands the actor the new plaintext key exactly once. Revoking
    their own cookie in the same commit (as the blanket finding-4 sweep did) would 401
    the SPA's post-rotate refetch + realtime reconnect before it renders the key,
    potentially unmounting Settings and hiding it — leaving a Plex-less operator with
    NEITHER the old nor the new key. The actor legitimately performed the rotation and
    the response IS their copy of the new key, so their session survives; all OTHER
    recovery sessions lose authority with the old key.
    """
    await seed(initialized=True, app_api_key=_API_KEY)
    actor_cookies, actor_csrf = await _recovery_session_cookies(app, tag="actor")
    other_cookies, _ = await _recovery_session_cookies(app, tag="other")

    rotate = await client.post(
        "/api/v1/settings/app-key/rotate", cookies=actor_cookies, headers=actor_csrf
    )

    assert rotate.status_code == 200
    new_key = rotate.json()["app_api_key"]
    assert new_key != _API_KEY

    # The actor's own recovery session survived; the bystander recovery session did not.
    assert await _recovery_session_revoked(sessionmaker_, tag="actor") is False
    assert await _recovery_session_revoked(sessionmaker_, tag="other") is True

    # The actor can still authenticate with their cookie after the rotation …
    still_valid = await client.get("/api/v1/settings", cookies=actor_cookies)
    assert still_valid.status_code == 200
    # … while the revoked bystander recovery cookie no longer authenticates.
    locked_out = await client.get("/api/v1/settings", cookies=other_cookies)
    assert locked_out.status_code == 401


async def test_rotate_via_recovery_cookie_with_stale_header_succeeds_and_keeps_actor(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    """A recovery-cookie admin whose client/proxy ALSO sends a stale ``X-Api-Key``
    header can still rotate, and keeps the actor exemption (issue #293 round 2).

    ``authenticate_request`` rejects the stale header and falls back to the cookie,
    still reporting ``api_key`` auth. The pre-fix CAS keyed off the header's mere
    PRESENCE, so it adopted the REJECTED value as its baseline and 409'd
    (``app_key_changed``) — and the actor self-exemption, using the same heuristic,
    treated the actor as a header caller and revoked their session. Keying both off
    ``AuthContext.via_api_key_header`` (the credential that actually authenticated)
    fixes both: the rotate succeeds and the actor's session survives while other
    recovery sessions are swept.
    """
    await seed(initialized=True, app_api_key=_API_KEY)
    actor_cookies, actor_csrf = await _recovery_session_cookies(app, tag="stale-actor")
    await _recovery_session_cookies(app, tag="stale-other")

    rotate = await client.post(
        "/api/v1/settings/app-key/rotate",
        cookies=actor_cookies,
        headers={**actor_csrf, "X-Api-Key": "stale-or-mistyped-key"},
    )

    assert rotate.status_code == 200
    new_key = rotate.json()["app_api_key"]
    assert new_key != _API_KEY

    # The cookie authenticated, so the actor exemption still applies: the actor's
    # session survives, the bystander recovery session is revoked.
    assert await _recovery_session_revoked(sessionmaker_, tag="stale-actor") is False
    assert await _recovery_session_revoked(sessionmaker_, tag="stale-other") is True
    still_valid = await client.get("/api/v1/settings", cookies=actor_cookies)
    assert still_valid.status_code == 200


async def test_revoke_via_recovery_cookie_with_stale_header_succeeds(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    """The DELETE (revoke) CAS shares ``_observed_app_key``: a stale ``X-Api-Key``
    riding alongside the authenticated recovery cookie must not 409 the revoke
    either (issue #293 round 2). The actor's session is still swept — revoke has no
    exemption (there is no new key to hand back)."""
    await seed(initialized=True, app_api_key=_API_KEY)
    cookies, csrf = await _recovery_session_cookies(app, tag="stale-revoker")

    revoke = await client.delete(
        "/api/v1/settings/app-key",
        cookies=cookies,
        headers={**csrf, "X-Api-Key": "stale-or-mistyped-key"},
    )

    assert revoke.status_code == 204
    async with sessionmaker_() as session:
        system = await load_system_settings(session)
        assert system is not None
        assert system.app_api_key is None
    # No exemption on revoke: the actor's own recovery session was retired too.
    assert await _recovery_session_states(sessionmaker_) == [True]


async def test_rotate_via_plex_session_still_revokes_every_recovery_session(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    """A NORMAL Plex-admin rotation still revokes EVERY recovery session — the P1
    actor-exemption is unchanged behaviour here.

    The exemption applies ONLY when the rotator is themselves a recovery-cookie
    session (they need the response to keep their break-glass access). A Plex admin
    holds a separate, Plex-backed identity untouched by the app-key change, so every
    break-glass recovery cookie is swept exactly as finding 4 intends.
    """
    await seed(initialized=True, app_api_key=_API_KEY)
    await _recovery_session_cookies(app, tag="sweep-a")
    await _recovery_session_cookies(app, tag="sweep-b")
    admin_cookies, admin_csrf = await _admin_session_cookies(app, plex_id=9301, tag="rotator")

    rotate = await client.post(
        "/api/v1/settings/app-key/rotate", cookies=admin_cookies, headers=admin_csrf
    )

    assert rotate.status_code == 200
    # BOTH recovery sessions were revoked — the Plex rotator is not exempt.
    assert await _recovery_session_states(sessionmaker_) == [True, True]


async def test_exchange_blocks_on_the_shared_app_key_rotate_lock(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P2 (issue #293): the key exchange runs its re-read + validate + session insert
    under the SAME lock the app-key rotate/revoke critical section holds, so a key
    change cannot interleave and leave a recovery session minted from a retired key.

    Proven directly by holding that lock and firing the exchange: it must BLOCK for the
    whole time the lock is held (the rotate path holds it across mint+revoke), then
    complete once released. Were the exchange on its own lock — the pre-fix state — it
    would ignore the held lock and complete immediately (``task.done()`` would be true
    while we still hold it), reopening the race this serialization closes.
    """
    await seed(initialized=True, app_api_key=_API_KEY)

    from plex_manager.web.routers import auth as auth_router
    from plex_manager.web.routers import settings as settings_router

    # A contended asyncio.Lock binds to the loop that first contended it; give this
    # test a fresh, loop-local instance shared by BOTH the exchange (auth router) and
    # the rotate (settings router) so a hold here blocks the exchange exactly as a
    # concurrent rotate would.
    fresh_lock = asyncio.Lock()
    monkeypatch.setattr(settings_router, "_rotate_lock", fresh_lock)
    monkeypatch.setattr(auth_router, "app_key_rotate_lock", fresh_lock)

    await fresh_lock.acquire()
    try:
        task = asyncio.create_task(
            client.post("/api/v1/auth/api-key", headers={"X-Api-Key": _API_KEY})
        )
        # An unblocked in-process request finishes in low single-digit ms; give it far
        # longer, then assert it has NOT completed — it is parked on the shared lock.
        await asyncio.sleep(0.1)
        assert not task.done()
    finally:
        fresh_lock.release()

    exchange = await task
    assert exchange.status_code == 200
    # Releasing the lock let the exchange mint exactly one live recovery session.
    assert await _recovery_session_states(sessionmaker_) == [False]


# --------------------------------------------------------------------------- #
# Opt-in recovery key — status / generate-from-null / revoke (keyless setup)
# --------------------------------------------------------------------------- #
async def test_app_key_status_false_on_fresh_keyless_init(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    """A fresh install mints no key (setup is keyless), so status reports absence.

    ``GET /app-key/status`` answers the Settings→Access UI's Generate-vs-Rotate
    question WITHOUT the break-glass reveal. With no key stored, the only way in
    is a Plex-session admin, so authenticate that way.
    """
    await seed(initialized=True)
    cookies, _ = await _admin_session_cookies(app, plex_id=7001, tag="status-empty")

    response = await client.get("/api/v1/settings/app-key/status", cookies=cookies)
    assert response.status_code == 200
    assert response.json() == {"exists": False}


async def test_app_key_status_true_when_a_key_exists_without_revealing_it(
    client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    response = await client.get("/api/v1/settings/app-key/status", headers={"X-Api-Key": _API_KEY})
    assert response.status_code == 200
    assert response.json() == {"exists": True}
    # The status probe never discloses the plaintext key — only its existence.
    assert _API_KEY not in response.text


async def test_app_key_status_requires_authentication(
    client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    response = await client.get("/api/v1/settings/app-key/status")
    assert response.status_code == 401


async def test_reveal_app_key_404s_when_no_key_exists(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    """Reveal on a keyless install is an honest 404 envelope, not a bare 409.

    The structured ``app_key_not_set`` envelope carries an operator-facing hint so
    the UI can nudge toward Generate rather than surface an opaque failure.
    """
    await seed(initialized=True)
    cookies, _ = await _admin_session_cookies(app, plex_id=7002, tag="reveal-absent")

    response = await client.get("/api/v1/settings/app-key", cookies=cookies)
    assert response.status_code == 404
    body = response.json()
    assert body["detail"] == "app_key_not_set"
    assert body["hint"]  # a non-empty nudge toward generating one


async def test_generate_app_key_from_null_mints_and_flips_status_true(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    """Rotate IS the generate path when no key exists: it mints, returns once, and
    flips status to present; the freshly minted key then authenticates + reveals."""
    await seed(initialized=True)
    cookies, csrf = await _admin_session_cookies(app, plex_id=7003, tag="generate")

    generate = await client.post("/api/v1/settings/app-key/rotate", cookies=cookies, headers=csrf)
    assert generate.status_code == 200
    new_key = generate.json()["app_api_key"]
    assert len(new_key) > 20  # matches setup's historical token_urlsafe(32) shape

    # Status now reports a key exists, without disclosing it.
    status_after = await client.get(
        "/api/v1/settings/app-key/status", headers={"X-Api-Key": new_key}
    )
    assert status_after.status_code == 200
    assert status_after.json() == {"exists": True}

    # The freshly minted key authenticates and reveals its own plaintext.
    reveal = await client.get("/api/v1/settings/app-key", headers={"X-Api-Key": new_key})
    assert reveal.status_code == 200
    assert reveal.json() == {"app_api_key": new_key}


async def test_revoke_app_key_returns_204_and_old_key_401s(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    """Revoke clears the stored key: 204 no-content, the old key 401s everywhere,
    and status flips back to absent (checked via a Plex-session admin, since the
    revoked key can no longer authenticate)."""
    await seed(initialized=True, app_api_key=_API_KEY)
    key_headers = {"X-Api-Key": _API_KEY}

    revoke = await client.delete("/api/v1/settings/app-key", headers=key_headers)
    assert revoke.status_code == 204
    assert revoke.content == b""  # 204 carries no body

    # The revoked key no longer authenticates anywhere.
    dead = await client.get("/api/v1/settings", headers=key_headers)
    assert dead.status_code == 401

    # A Plex-session admin still gets in and sees the key is gone.
    cookies, _ = await _admin_session_cookies(app, plex_id=7004, tag="revoked")
    status_after = await client.get("/api/v1/settings/app-key/status", cookies=cookies)
    assert status_after.status_code == 200
    assert status_after.json() == {"exists": False}


async def test_revoke_app_key_requires_authentication(
    client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    response = await client.delete("/api/v1/settings/app-key")
    assert response.status_code == 401


async def test_revoke_app_key_is_idempotent_when_no_key_exists(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    """Revoking a keyless install is a no-op 204, not an error — the end state
    (no key) is the same whether or not one was present."""
    await seed(initialized=True)
    cookies, csrf = await _admin_session_cookies(app, plex_id=7005, tag="revoke-noop")

    revoke = await client.delete("/api/v1/settings/app-key", cookies=cookies, headers=csrf)
    assert revoke.status_code == 204
    assert revoke.content == b""


async def test_revoke_app_key_cas_rejects_stale_revoke_after_concurrent_rotation(
    client: httpx.AsyncClient,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale revoke must not wipe a key rotated in between (lost update).

    The revoke authenticates against the live key; a concurrent ROTATE then commits
    a fresh key before this request writes ``None``. The earlier draft loaded
    ``system`` and unconditionally cleared it, silently clobbering the rotation. The
    revoke CAS (mirroring the rotate CAS) must re-read under the lock, see the key
    is no longer the value it observed, and 409 — leaving the rotated key intact.
    """
    await seed(initialized=True, app_api_key=_API_KEY)

    from plex_manager.web.routers import settings as settings_router

    real_ensure = settings_router.ensure_system_settings
    winner_key = "rotated-mid-revoke-0123456789abcdef"
    state = {"raced": False}

    async def racing_ensure(session: AsyncSession) -> object:
        row = await real_ensure(session)
        if not state["raced"]:
            # Fire once: a competing ROTATE commits a new key on a separate session
            # AFTER this revoke authenticated against the old key but BEFORE it writes.
            state["raced"] = True
            async with sessionmaker_() as other:
                other_row = await real_ensure(other)
                other_row.app_api_key = winner_key
                await other.commit()
        return row

    monkeypatch.setattr(settings_router, "ensure_system_settings", racing_ensure)

    losing = await client.delete("/api/v1/settings/app-key", headers={"X-Api-Key": _API_KEY})
    assert losing.status_code == 409
    assert losing.json()["detail"] == "app_key_changed"

    # The concurrently-rotated key survived — the stale revoke did not wipe it.
    async with sessionmaker_() as session:
        system = await load_system_settings(session)
        assert system is not None
        assert system.app_api_key == winner_key


async def test_revoke_app_key_leaves_key_none_on_success(
    client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """A normal (non-raced) revoke still commits ``None`` and returns 204 under the
    new CAS: the observed key matches the stored key, so the clear proceeds."""
    await seed(initialized=True, app_api_key=_API_KEY)

    revoke = await client.delete("/api/v1/settings/app-key", headers={"X-Api-Key": _API_KEY})
    assert revoke.status_code == 204
    assert revoke.content == b""

    async with sessionmaker_() as session:
        system = await load_system_settings(session)
        assert system is not None
        assert system.app_api_key is None


async def test_revoke_app_key_via_session_auth_clears_present_key(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """A Plex-SESSION admin revoking a PRESENT key exercises the CAS's other
    ``observed`` source: session auth carries no ``X-Api-Key`` header, so the CAS
    compares against the value this request's session loaded at auth time. An
    unraced revoke must therefore still clear the key (204), not spuriously 409.
    """
    await seed(initialized=True, app_api_key=_API_KEY)
    cookies, csrf = await _admin_session_cookies(app, plex_id=7006, tag="revoke-present")

    revoke = await client.delete("/api/v1/settings/app-key", cookies=cookies, headers=csrf)
    assert revoke.status_code == 204
    assert revoke.content == b""

    async with sessionmaker_() as session:
        system = await load_system_settings(session)
        assert system is not None
        assert system.app_api_key is None


async def _mutation_request(
    client: httpx.AsyncClient,
    kind: Literal["generic", "rotate", "revoke"],
    new_secret: str,
    *,
    app_key: str = _API_KEY,
) -> httpx.Response:
    headers = {"X-Api-Key": app_key}
    if kind == "generic":
        return await client.put(
            "/api/v1/settings", json={"tmdb_api_key": new_secret}, headers=headers
        )
    if kind == "rotate":
        return await client.post("/api/v1/settings/app-key/rotate", headers=headers)
    return await client.delete("/api/v1/settings/app-key", headers=headers)


async def _seed_rotation_source(
    kind: Literal["generic", "rotate", "revoke"],
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    old_value: str,
) -> None:
    await seed(initialized=True, app_api_key=old_value if kind != "generic" else _API_KEY)
    if kind == "generic":
        async with sessionmaker_() as session:
            await SettingsStore(session).set("tmdb_api_key", old_value)
            await session.commit()


async def _run_one_drain(
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    entered_drain: asyncio.Event,
    release_drain: asyncio.Event,
) -> None:
    from plex_manager.web import app as app_module

    real_drain_once = app_module.log_capture_service.drain_once

    async def paused_drain(*args: object, **kwargs: object) -> int:
        entered_drain.set()
        await _wait_for_event(release_drain)
        return await real_drain_once(*args, **kwargs)  # type: ignore[arg-type]

    async def stop_after_tick(seconds: float) -> None:
        # ``app_module.asyncio`` IS the global asyncio module, so this patch
        # replaces ``asyncio.sleep`` everywhere for the test's duration. Let
        # zero-delay cooperative yield checkpoints (the batched log rewrite
        # yields between keyset batches) pass through; only the drain loop's
        # real interval sleep stops the loop.
        if seconds == 0:
            return
        raise _StopDrainLoop

    monkeypatch.setattr(app_module.log_capture_service, "drain_once", paused_drain)
    monkeypatch.setattr(app_module.asyncio, "sleep", stop_after_tick)
    await app_module._log_drain_loop(app)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("kind", ["generic", "rotate", "revoke"])
async def test_capture_before_rotation_and_drain_after_commit_redacts_retired_record(
    kind: Literal["generic", "rotate", "revoke"],
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A queued capture survives the boundary only in redacted form after commit."""

    old_value = f"old-{kind}-capture-boundary-secret"
    new_secret = "new-generic-capture-boundary-secret"  # noqa: S105 -- fixture credential
    await _seed_rotation_source(kind, seed, sessionmaker_, old_value)
    handler = log_capture_service.LogCaptureHandler()
    captured = log_capture_service.CapturedLogRecord(
        created_at=datetime.now(UTC),
        level="INFO",
        logger="test",
        message=f"captured {old_value}",
        context={old_value: [old_value]},
    )
    handler.queue.put_nowait(captured)
    handler.ring_buffer.append(captured)
    app.state.log_handler = handler
    lock = _OrderingLock()
    real_release = lock.release

    def release_after_live_cleanup() -> None:
        for record in (*tuple(handler.queue._queue), *handler.snapshot_tail(10)):  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]
            assert old_value not in record.message
            assert old_value not in json.dumps(record.context)
        real_release()

    monkeypatch.setattr(lock, "release", release_after_live_cleanup)
    monkeypatch.setattr(deps.secret_rotation_lock, "value", lock)

    response = await _mutation_request(
        client,
        kind,
        new_secret,
        app_key=old_value if kind != "generic" else _API_KEY,
    )

    assert response.status_code == (204 if kind == "revoke" else 200)
    assert lock.locked() is False
    queued = handler.queue.get_nowait()
    handler.queue.put_nowait(queued)
    ring = handler.snapshot_tail(1)[0]
    for record in (queued, ring):
        assert old_value not in record.message
        assert old_value not in json.dumps(record.context)
    async with sessionmaker_() as session:
        inserted = await log_capture_service.drain_once(
            handler.queue, SqlLogEventRepository(session), handler=handler
        )
        await session.commit()
        row = (await session.execute(select(LogEvent))).scalar_one()
    assert inserted == 1
    assert old_value not in row.message
    assert old_value not in json.dumps(row.context_json)


@pytest.mark.parametrize("kind", ["generic", "rotate", "revoke"])
async def test_capture_during_rotation_cannot_commit_retired_secret(
    kind: Literal["generic", "rotate", "revoke"],
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The widened handler snapshot redacts emits before a paused rewrite can commit."""
    from plex_manager.web.routers import settings as settings_router

    old_value = f"old-{kind}-capture-during-secret"
    new_secret = "new-generic-capture-during-secret"  # noqa: S105 -- fixture credential
    await _seed_rotation_source(kind, seed, sessionmaker_, old_value)
    handler = log_capture_service.LogCaptureHandler()
    app.state.log_handler = handler
    lock = _OrderingLock()
    monkeypatch.setattr(deps.secret_rotation_lock, "value", lock)
    rotation_ready = asyncio.Event()
    release_rotation = asyncio.Event()
    real_rewrite = settings_router._rewrite_before_secret_replacement  # pyright: ignore[reportPrivateUsage]

    async def paused_rewrite(session: AsyncSession, values: frozenset[str]) -> int:
        rotation_ready.set()
        await _wait_for_event(release_rotation)
        return await real_rewrite(session, values)

    monkeypatch.setattr(settings_router, "_rewrite_before_secret_replacement", paused_rewrite)
    mutation = asyncio.create_task(
        _mutation_request(
            client,
            kind,
            new_secret,
            app_key=old_value if kind != "generic" else _API_KEY,
        )
    )
    await _wait_for_event(rotation_ready)
    assert old_value in handler.secret_values
    logger = logging.getLogger(f"test.capture-during-{kind}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)
    try:
        logger.info("captured %s", old_value, extra={"request_id": old_value})
        await asyncio.sleep(0)
    finally:
        logger.removeHandler(handler)
    release_rotation.set()
    response = await asyncio.wait_for(mutation, timeout=5.0)

    assert response.status_code == (204 if kind == "revoke" else 200)
    assert lock.locked() is False
    async with sessionmaker_() as session:
        inserted = await log_capture_service.drain_once(
            handler.queue, SqlLogEventRepository(session), handler=handler
        )
        await session.commit()
        row = (await session.execute(select(LogEvent))).scalar_one()
    assert inserted == 1
    assert old_value not in row.message
    assert old_value not in json.dumps(row.context_json)
    ring = handler.snapshot_tail(1)[0]
    assert old_value not in ring.message
    assert old_value not in json.dumps(ring.context)


@pytest.mark.parametrize("kind", ["generic", "rotate", "revoke"])
async def test_drain_holds_rotation_lock_before_mutation_and_retired_row_is_rewritten(
    kind: Literal["generic", "rotate", "revoke"],
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A drain committed before each mutation is still rewritten before it returns."""

    old_value = f"old-{kind}-drain-ordering-secret"
    new_secret = "new-generic-drain-ordering-secret"  # noqa: S105 -- fixture credential
    await _seed_rotation_source(kind, seed, sessionmaker_, old_value)
    handler = log_capture_service.LogCaptureHandler()
    handler.queue.put_nowait(
        log_capture_service.CapturedLogRecord(
            created_at=datetime.now(UTC),
            level="INFO",
            logger="test",
            message=f"drained {old_value}",
            context={"retired": old_value},
        )
    )
    app.state.log_handler = handler
    lock = _OrderingLock()
    monkeypatch.setattr(deps.secret_rotation_lock, "value", lock)
    entered_drain = asyncio.Event()
    release_drain = asyncio.Event()
    drain = asyncio.create_task(_run_one_drain(app, monkeypatch, entered_drain, release_drain))
    await _wait_for_event(entered_drain)

    app_key = old_value if kind != "generic" else _API_KEY
    mutation = asyncio.create_task(_mutation_request(client, kind, new_secret, app_key=app_key))
    await _wait_for_event(lock.second_acquire_started)
    assert not mutation.done()
    release_drain.set()
    await assert_task_raises(drain, _StopDrainLoop)
    response = await asyncio.wait_for(mutation, timeout=5.0)

    assert response.status_code == (204 if kind == "revoke" else 200)
    async with sessionmaker_() as session:
        row = (await session.execute(select(LogEvent))).scalar_one()
    assert old_value not in row.message
    assert old_value not in json.dumps(row.context_json)


@pytest.mark.parametrize("kind", ["generic", "rotate", "revoke"])
async def test_mutation_holds_rotation_lock_before_drain_and_drain_reads_final_snapshot(
    kind: Literal["generic", "rotate", "revoke"],
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A drain waiting behind a mutation refreshes only after its final commit."""
    from plex_manager.web.routers import settings as settings_router

    old_value = f"old-{kind}-mutation-ordering-secret"
    new_secret = "new-generic-mutation-ordering-secret"  # noqa: S105 -- fixture credential
    await _seed_rotation_source(kind, seed, sessionmaker_, old_value)
    handler = log_capture_service.LogCaptureHandler()
    app.state.log_handler = handler
    lock = _OrderingLock()
    monkeypatch.setattr(deps.secret_rotation_lock, "value", lock)
    mutation_entered = asyncio.Event()
    release_mutation = asyncio.Event()
    real_rewrite = settings_router._rewrite_before_secret_replacement  # pyright: ignore[reportPrivateUsage]

    async def paused_rewrite(session: AsyncSession, values: frozenset[str]) -> int:
        mutation_entered.set()
        await _wait_for_event(release_mutation)
        return await real_rewrite(session, values)

    monkeypatch.setattr(settings_router, "_rewrite_before_secret_replacement", paused_rewrite)
    app_key = old_value if kind != "generic" else _API_KEY
    mutation = asyncio.create_task(_mutation_request(client, kind, new_secret, app_key=app_key))
    await _wait_for_event(mutation_entered)

    entered_drain = asyncio.Event()
    release_drain = asyncio.Event()
    drain = asyncio.create_task(_run_one_drain(app, monkeypatch, entered_drain, release_drain))
    await _wait_for_event(lock.second_acquire_started)
    assert not drain.done()
    release_mutation.set()
    response = await asyncio.wait_for(mutation, timeout=5.0)
    assert response.status_code == (204 if kind == "revoke" else 200)
    await _wait_for_event(entered_drain)
    release_drain.set()
    await assert_task_raises(drain, _StopDrainLoop)

    assert old_value not in handler.secret_values
    if kind == "generic":
        assert new_secret in handler.secret_values


@pytest.mark.parametrize("failure", ["rewrite", "commit", "cancel", "endpoint"])
async def test_failed_or_cancelled_rotation_releases_lock_for_following_drain_and_rotation(
    failure: Literal["rewrite", "commit", "cancel", "endpoint"],
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every exceptional mutation exit leaves the shared boundary usable."""
    from plex_manager.web.routers import settings as settings_router

    old_secret = "old-lock-release-secret"  # noqa: S105 -- fixture credential
    next_secret = "next-lock-release-secret"  # noqa: S105 -- fixture credential
    await _seed_rotation_source("generic", seed, sessionmaker_, old_secret)
    app.state.log_handler = log_capture_service.LogCaptureHandler()
    app.state.log_handler.secret_values = frozenset({old_secret})
    lock = _OrderingLock()
    monkeypatch.setattr(deps.secret_rotation_lock, "value", lock)

    if failure == "rewrite":
        real_rewrite = settings_router._rewrite_before_secret_replacement  # pyright: ignore[reportPrivateUsage]

        async def failing_rewrite(_session: AsyncSession, _values: frozenset[str]) -> int:
            raise RuntimeError("rewrite failed")

        monkeypatch.setattr(settings_router, "_rewrite_before_secret_replacement", failing_rewrite)
        with pytest.raises(RuntimeError, match="rewrite failed"):
            await _mutation_request(client, "generic", next_secret)
        await asyncio.wait_for(lock.releases.get(), timeout=5.0)
        monkeypatch.setattr(settings_router, "_rewrite_before_secret_replacement", real_rewrite)
    elif failure == "commit":
        real_commit = AsyncSession.commit
        real_rewrite = settings_router._rewrite_before_secret_replacement  # pyright: ignore[reportPrivateUsage]
        rewrite_finished = False

        async def mark_rewrite(session: AsyncSession, values: frozenset[str]) -> int:
            nonlocal rewrite_finished
            result = await real_rewrite(session, values)
            rewrite_finished = True
            return result

        async def failing_once(self: AsyncSession) -> None:
            if rewrite_finished:
                raise RuntimeError("commit failed")
            await real_commit(self)

        monkeypatch.setattr(settings_router, "_rewrite_before_secret_replacement", mark_rewrite)
        monkeypatch.setattr(AsyncSession, "commit", failing_once)
        with pytest.raises(RuntimeError, match="commit failed"):
            await _mutation_request(client, "generic", next_secret)
        await asyncio.wait_for(lock.releases.get(), timeout=5.0)
        monkeypatch.setattr(settings_router, "_rewrite_before_secret_replacement", real_rewrite)
        monkeypatch.setattr(AsyncSession, "commit", real_commit)
    elif failure == "cancel":
        entered = asyncio.Event()
        release = asyncio.Event()
        real_rewrite = settings_router._rewrite_before_secret_replacement  # pyright: ignore[reportPrivateUsage]

        async def blocked_rewrite(session: AsyncSession, values: frozenset[str]) -> int:
            entered.set()
            await _wait_for_event(release)
            return await real_rewrite(session, values)

        monkeypatch.setattr(settings_router, "_rewrite_before_secret_replacement", blocked_rewrite)
        cancelled = asyncio.create_task(_mutation_request(client, "generic", next_secret))
        await _wait_for_event(entered)
        cancelled.cancel()
        await assert_task_raises(cancelled, asyncio.CancelledError)
        await asyncio.wait_for(lock.releases.get(), timeout=5.0)
        monkeypatch.setattr(settings_router, "_rewrite_before_secret_replacement", real_rewrite)
    else:
        real_set = SettingsStore.set

        async def failing_endpoint_write(self: SettingsStore, key: str, value: str) -> None:
            if key == "tmdb_api_key" and value == next_secret:
                raise RuntimeError("endpoint write failed")
            await real_set(self, key, value)

        monkeypatch.setattr(SettingsStore, "set", failing_endpoint_write)
        with pytest.raises(RuntimeError, match="endpoint write failed"):
            await _mutation_request(client, "generic", next_secret)
        await asyncio.wait_for(lock.releases.get(), timeout=5.0)
        monkeypatch.setattr(SettingsStore, "set", real_set)

    entered_drain = asyncio.Event()
    release_drain = asyncio.Event()
    drain = asyncio.create_task(_run_one_drain(app, monkeypatch, entered_drain, release_drain))
    await _wait_for_event(entered_drain)
    release_drain.set()
    await assert_task_raises(drain, _StopDrainLoop)

    response = await asyncio.wait_for(
        _mutation_request(client, "generic", next_secret), timeout=5.0
    )
    assert response.status_code == 200


@pytest.mark.parametrize("kind", ["generic", "rotate", "revoke"])
async def test_mutation_reopens_transaction_under_lock_before_rewrite(
    kind: Literal["generic", "rotate", "revoke"],
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every mutation path ends its pre-lock transaction AFTER acquiring the
    rotation lock and BEFORE the rewrite (codex #382): a drain that held the
    lock first may have committed rows carrying the retiring secret, and a
    retained pre-lock snapshot would let the rewrite miss them and the final
    ``secret_values()`` read regress the handler snapshot."""
    from plex_manager.web.routers import settings as settings_router

    old_value = f"old-{kind}-fresh-snapshot-secret"
    new_secret = "new-generic-fresh-snapshot-secret"  # noqa: S105 -- fixture credential
    await _seed_rotation_source(kind, seed, sessionmaker_, old_value)
    app.state.log_handler = log_capture_service.LogCaptureHandler()
    events: list[str] = []

    class _RecordingLock(asyncio.Lock):
        async def acquire(self) -> Literal[True]:
            result = await super().acquire()
            events.append("locked")
            return result

    lock = _RecordingLock()
    monkeypatch.setattr(deps.secret_rotation_lock, "value", lock)

    real_rollback = AsyncSession.rollback

    async def recording_rollback(self: AsyncSession) -> None:
        events.append("rollback")
        await real_rollback(self)

    monkeypatch.setattr(AsyncSession, "rollback", recording_rollback)
    real_rewrite = settings_router._rewrite_before_secret_replacement  # pyright: ignore[reportPrivateUsage]

    async def recording_rewrite(session: AsyncSession, values: frozenset[str]) -> int:
        events.append("rewrite")
        return await real_rewrite(session, values)

    monkeypatch.setattr(settings_router, "_rewrite_before_secret_replacement", recording_rewrite)

    app_key = old_value if kind != "generic" else _API_KEY
    response = await _mutation_request(client, kind, new_secret, app_key=app_key)

    assert response.status_code == (204 if kind == "revoke" else 200)
    locked_at = events.index("locked")
    assert "rewrite" not in events[:locked_at]
    assert events[locked_at + 1 : locked_at + 3] == ["rollback", "rewrite"]


async def test_non_secret_put_never_decrypts_stored_credentials(
    client: httpx.AsyncClient,
    seed: SeedFn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PUT touching only non-secret fields must succeed without ever calling
    ``SettingsStore.secret_values()`` (codex #382): an unrelated corrupt or
    undecryptable stored credential must not 500 a save of log retention or
    eviction tuning -- the rotation boundary decrypts only when a secret
    actually changes."""
    await seed(initialized=True, app_api_key=_API_KEY)

    async def poisoned(self: SettingsStore) -> frozenset[str]:
        raise RuntimeError("corrupt encrypted credential")

    monkeypatch.setattr(SettingsStore, "secret_values", poisoned)
    response = await client.put(
        "/api/v1/settings",
        json={"log_retention_days": 12},
        headers={"X-Api-Key": _API_KEY},
    )
    assert response.status_code == 200
    assert response.json()["log_retention_days"] == 12


async def test_short_retired_secret_is_masked_despite_read_floor(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    """Rotating a secret SHORTER than the 8-char value-redaction floor (e.g. a
    5-char qBittorrent password) still erases bare occurrences from durable
    rows, the live ring, and the queue (codex #382): the rewrite's exact-match
    pass has no floor -- the floor guards live reads against over-redaction,
    which does not apply to a specific retiring value."""
    old_value = "abc12"  # 5 chars: below MIN_SECRET_VALUE_LENGTH
    await seed(initialized=True, app_api_key=_API_KEY)
    async with sessionmaker_() as session:
        await SettingsStore(session).set("qbittorrent_password", old_value)
        session.add(
            LogEvent(
                level="INFO",
                logger="test",
                message=f"durable bare {old_value} occurrence",
                context_json={old_value: [f"nested {old_value}"]},
            )
        )
        await session.commit()
    handler = log_capture_service.LogCaptureHandler()
    live_record = log_capture_service.CapturedLogRecord(
        created_at=datetime.now(UTC),
        level="INFO",
        logger="test",
        message=f"live bare {old_value} occurrence",
        context={"retiring": old_value},
    )
    handler.queue.put_nowait(live_record)
    handler.ring_buffer.append(live_record)
    app.state.log_handler = handler

    response = await client.put(
        "/api/v1/settings",
        json={"qbittorrent_password": "new-longer-password-123"},
        headers={"X-Api-Key": _API_KEY},
    )
    assert response.status_code == 200

    async with sessionmaker_() as session:
        row = (await session.execute(select(LogEvent))).scalar_one()
    assert old_value not in row.message
    assert old_value not in json.dumps(row.context_json)
    for record in (*tuple(handler.queue._queue), *handler.snapshot_tail(10)):  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]
        assert old_value not in record.message
        assert old_value not in json.dumps(record.context)
