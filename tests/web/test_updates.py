"""Admin update controls and private sidecar coordination API."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from plex_manager.config import get_settings
from plex_manager.ports.metadata import MovieMetadata
from plex_manager.services.update_coordination_service import UpdateCoordinationService
from tests.web.fakes import FakeTmdb, override_adapters

SeedFn = Callable[..., Awaitable[None]]

_API_KEY = "updates-key"
_ADMIN = {"X-Api-Key": _API_KEY}
_UPDATER_TOKEN = "updater-test-token-with-at-least-thirty-two-bytes"  # noqa: S105


@pytest.fixture
def updater_headers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    secret = tmp_path / "updater-token"
    secret.write_text(_UPDATER_TOKEN, encoding="utf-8")
    monkeypatch.setenv("PLEX_MANAGER_UPDATER_SECRET_FILE", str(secret))
    get_settings.cache_clear()
    return {"Authorization": f"Bearer {_UPDATER_TOKEN}"}


async def test_status_is_honestly_unavailable_without_a_sidecar(
    client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    response = await client.get("/api/v1/updates/status", headers=_ADMIN)
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "unavailable"
    assert body["updater_available"] is False
    assert body["channel"] == "stable"
    assert body["blocker"] == "updater_unavailable"


async def test_public_update_routes_require_admin(client: httpx.AsyncClient, seed: SeedFn) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    assert (await client.get("/api/v1/updates/status")).status_code == 401
    assert (await client.post("/api/v1/updates/check-now")).status_code == 401


async def test_internal_api_accepts_only_the_compose_secret(
    client: httpx.AsyncClient,
    seed: SeedFn,
    updater_headers: dict[str, str],
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    public_credential = await client.post("/api/v1/internal/updates/eligibility", headers=_ADMIN)
    assert public_credential.status_code == 401
    valid = await client.post("/api/v1/internal/updates/eligibility", headers=updater_headers)
    assert valid.status_code == 200
    assert valid.json()["action"] == "none"


async def test_check_and_claim_flow_is_targetless_and_concurrency_safe(
    client: httpx.AsyncClient,
    seed: SeedFn,
    updater_headers: dict[str, str],
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    await client.post("/api/v1/internal/updates/eligibility", headers=updater_headers)

    arbitrary_target = await client.post(
        "/api/v1/updates/check-now",
        json={"container": "some-other-container"},
        headers=_ADMIN,
    )
    assert arbitrary_target.status_code == 422

    requested = await client.post("/api/v1/updates/check-now", headers=_ADMIN)
    assert requested.status_code == 200
    assert requested.json()["state"] == "checking"
    eligible = await client.post("/api/v1/internal/updates/eligibility", headers=updater_headers)
    assert eligible.json()["action"] == "check"

    checked = await client.post(
        "/api/v1/internal/updates/outcome",
        headers=updater_headers,
        json={
            "operation": "check",
            "outcome": "update_available",
            "current_build": "old-build",
            "current_digest": "sha256:old",
            "available_build": "new-build",
            "available_digest": "sha256:new",
        },
    )
    assert checked.status_code == 200
    assert checked.json()["state"] == "update_available"

    queued = await client.post("/api/v1/updates/update-when-ready", headers=_ADMIN)
    assert queued.status_code == 200
    claim = await client.post("/api/v1/internal/updates/claim", headers=updater_headers)
    assert claim.status_code == 200
    token = claim.json()["lease_token"]
    assert isinstance(token, str)
    assert claim.json()["ready"] is True

    concurrent = await client.post("/api/v1/internal/updates/claim", headers=updater_headers)
    assert concurrent.status_code == 200
    assert concurrent.json()["lease_token"] is None
    assert concurrent.json()["blocker"] == "concurrent_update_claim"

    renewed = await client.post(
        "/api/v1/internal/updates/renew",
        headers=updater_headers,
        json={"lease_token": token},
    )
    assert renewed.status_code == 200
    assert renewed.json()["ready"] is True

    released = await client.post(
        "/api/v1/internal/updates/release",
        headers=updater_headers,
        json={"lease_token": token},
    )
    assert released.status_code == 200


async def test_drain_blocks_admin_mutations_but_accepts_new_requests(
    app: FastAPI,
    client: httpx.AsyncClient,
    seed: SeedFn,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    coordinator = UpdateCoordinationService(app.state.sessionmaker)
    await coordinator.initialize()
    app.state.update_coordinator = coordinator
    claim = await coordinator.claim_drain(ttl=timedelta(minutes=1))
    assert claim is not None

    blocked = await client.put(
        "/api/v1/settings",
        json={"automatic_updates_enabled": False},
        headers=_ADMIN,
    )
    assert blocked.status_code == 503
    assert blocked.json()["detail"] == "maintenance_in_progress"

    override_adapters(
        app,
        tmdb=FakeTmdb(
            movies={1: MovieMetadata(tmdb_id=1, title="Queued During Update", year=2026)}
        ),
    )
    accepted = await client.post(
        "/api/v1/requests",
        json={"tmdb_id": 1, "media_type": "movie"},
        headers=_ADMIN,
    )
    assert accepted.status_code == 201
    await coordinator.release(claim.lease.token)
