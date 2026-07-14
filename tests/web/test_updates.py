"""Admin update controls and private sidecar coordination API."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.config import get_settings
from plex_manager.models import MaintenanceLease, UpdateCoordinatorState
from plex_manager.ports.metadata import MovieMetadata
from plex_manager.repositories.update_coordination import CoordinatorSnapshot
from plex_manager.services.update_coordination_service import (
    UpdateAction,
    UpdateCoordinationService,
    UpdatePhase,
    UpdateResult,
)
from tests.web.fakes import FakeTmdb, override_adapters

SeedFn = Callable[..., Awaitable[None]]

_API_KEY = "updates-key"
_ADMIN = {"X-Api-Key": _API_KEY}
_UPDATER_TOKEN = "updater-test-token-with-at-least-thirty-two-bytes"  # noqa: S105


async def _enable_automatic_updates(client: httpx.AsyncClient) -> None:
    response = await client.put(
        "/api/v1/settings",
        headers=_ADMIN,
        json={
            "automatic_updates_enabled": True,
            "automatic_update_timezone": "UTC",
            "automatic_update_weekdays": [
                "monday",
                "tuesday",
                "wednesday",
                "thursday",
                "friday",
                "saturday",
                "sunday",
            ],
            "automatic_update_window_start": "00:00",
            "automatic_update_window_end": "23:59",
            "automatic_update_idle_only": True,
        },
    )
    assert response.status_code == 200


def _freeze_router_clock(monkeypatch: pytest.MonkeyPatch, moment: list[datetime]) -> None:
    """Pin the ``datetime.now(UTC)`` seen by the updates router to ``moment[0]``.

    The window-open checks in ``web/routers/updates.py`` call ``datetime.now(UTC)``
    directly rather than threading a fake clock, so a real "always open" window
    (00:00-23:59) genuinely closes for the last minute of the UTC day
    (``UpdateSchedule.is_open`` is a half-open ``[start, end)`` check). Tests that
    assume the window is open would then flake once a day. Pin it here instead so
    window-dependent assertions are deterministic regardless of when the suite
    runs. Pass a one-element list so callers can advance ``moment[0]`` in lockstep
    with a coordinator fake clock built the same way.
    """
    from plex_manager.web.routers import updates as updates_module

    class _FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            return moment[0]

    monkeypatch.setattr(updates_module, "datetime", _FrozenDateTime)


@pytest.fixture
def updater_headers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    secret = tmp_path / "updater-token"
    secret.write_text(_UPDATER_TOKEN, encoding="utf-8")
    monkeypatch.setenv("PLEX_MANAGER_UPDATER_SECRET_FILE", str(secret))
    get_settings.cache_clear()
    return {"Authorization": f"Bearer {_UPDATER_TOKEN}"}


async def test_status_is_honestly_unavailable_without_a_sidecar(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    response = await client.get("/api/v1/updates/status", headers=_ADMIN)
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "unavailable"
    assert body["updater_available"] is False
    assert body["channel"] == "stable"
    assert body["blocker"] == "updater_unavailable"
    assert isinstance(app.state.update_coordinator, UpdateCoordinationService)


async def test_update_route_preserves_coordinator_failure_response(
    client: httpx.AsyncClient,
    seed: SeedFn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plex_manager.web.routers import updates as updates_module

    await seed(initialized=True, app_api_key=_API_KEY)

    async def fail_coordinator(_app: FastAPI) -> UpdateCoordinationService:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(updates_module, "ensure_update_coordinator", fail_coordinator)
    response = await client.get("/api/v1/updates/status", headers=_ADMIN)
    assert response.status_code == 503
    assert response.json()["detail"] == "updater_coordinator_unavailable"


async def test_mutation_middleware_preserves_coordinator_failure_response(
    client: httpx.AsyncClient,
    seed: SeedFn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plex_manager.web import middleware as middleware_module

    await seed(initialized=True, app_api_key=_API_KEY)

    async def fail_coordinator(_app: FastAPI) -> UpdateCoordinationService:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(middleware_module, "ensure_update_coordinator", fail_coordinator)
    response = await client.put(
        "/api/v1/settings",
        json={"automatic_updates_enabled": False},
        headers=_ADMIN,
    )
    assert response.status_code == 503
    assert response.json() == {
        "detail": "maintenance_coordinator_unavailable",
        "message": "A safe mutation lease could not be established.",
    }


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


async def test_eligibility_touch_reuses_its_returned_snapshot(
    app: FastAPI,
    client: httpx.AsyncClient,
    seed: SeedFn,
    updater_headers: dict[str, str],
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)

    class CountingCoordinator(UpdateCoordinationService):
        snapshot_calls = 0
        touch_calls = 0

        async def snapshot(self) -> CoordinatorSnapshot:
            self.snapshot_calls += 1
            return await super().snapshot()

        async def touch_updater(
            self,
            *,
            phase: UpdatePhase | None = None,
            expected_generation: int | None = None,
        ) -> CoordinatorSnapshot | None:
            self.touch_calls += 1
            return await super().touch_updater(
                phase=phase,
                expected_generation=expected_generation,
            )

    coordinator = CountingCoordinator(app.state.sessionmaker)
    await coordinator.initialize()
    app.state.update_coordinator = coordinator

    response = await client.post(
        "/api/v1/internal/updates/eligibility",
        headers=updater_headers,
    )

    assert response.status_code == 200
    assert coordinator.touch_calls == 1
    # touch_updater() returns the one snapshot used by eligibility; the router
    # must not perform a second full snapshot before touching.
    assert coordinator.snapshot_calls == 1


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
    check_generation = eligible.json()["action_generation"]

    checked = await client.post(
        "/api/v1/internal/updates/outcome",
        headers=updater_headers,
        json={
            "operation": "check",
            "outcome": "update_available",
            "action_generation": check_generation,
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


async def test_manual_update_without_cached_digest_reports_preflight_checking(
    client: httpx.AsyncClient,
    seed: SeedFn,
    updater_headers: dict[str, str],
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    await client.post("/api/v1/internal/updates/eligibility", headers=updater_headers)
    queued = await client.post("/api/v1/updates/update-when-ready", headers=_ADMIN)
    assert queued.status_code == 200
    assert queued.json()["state"] == "checking"
    assert queued.json()["blocker"] == "checking_for_update"
    eligibility = await client.post("/api/v1/internal/updates/eligibility", headers=updater_headers)
    assert eligibility.status_code == 200
    assert eligibility.json()["action"] == "check"


async def test_stale_check_outcome_cannot_consume_newer_install_action(
    app: FastAPI,
    client: httpx.AsyncClient,
    seed: SeedFn,
    updater_headers: dict[str, str],
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    await client.post("/api/v1/internal/updates/eligibility", headers=updater_headers)
    await client.post("/api/v1/updates/check-now", headers=_ADMIN)
    eligibility = await client.post("/api/v1/internal/updates/eligibility", headers=updater_headers)
    stale_generation = eligibility.json()["action_generation"]
    # Finish the check (freeing the action slot) before queuing the install; a new
    # public action is refused while the check is still pending. The sidecar may
    # still redeliver the completed check's outcome at least once, so that late,
    # stale-generation receipt must not consume the newer install action.
    first = await client.post(
        "/api/v1/internal/updates/outcome",
        headers=updater_headers,
        json={
            "operation": "check",
            "outcome": "no_update",
            "action_generation": stale_generation,
        },
    )
    assert first.status_code == 200
    await client.post("/api/v1/updates/update-when-ready", headers=_ADMIN)

    stale = await client.post(
        "/api/v1/internal/updates/outcome",
        headers=updater_headers,
        json={
            "operation": "check",
            "outcome": "no_update",
            "action_generation": stale_generation,
            "current_digest": "sha256:stale",
        },
    )
    assert stale.status_code == 409
    snapshot = await app.state.update_coordinator.snapshot()
    assert snapshot.requested_action == "install"
    assert snapshot.action_generation == stale_generation + 1
    assert snapshot.current_digest is None


async def test_check_now_cannot_downgrade_a_queued_install(
    app: FastAPI,
    client: httpx.AsyncClient,
    seed: SeedFn,
    updater_headers: dict[str, str],
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    coordinator = UpdateCoordinationService(app.state.sessionmaker)
    await coordinator.initialize()
    app.state.update_coordinator = coordinator
    # A prefetch check leaves an update available, then the operator queues an
    # install that the sidecar has not yet claimed (phase still "available").
    assert await coordinator.acknowledge_action(
        expected_generation=0,
        result=UpdateResult.update_available,
        current_digest="sha256:old",
        available_digest="sha256:new",
    )
    await client.post("/api/v1/internal/updates/eligibility", headers=updater_headers)
    queued = await client.post("/api/v1/updates/update-when-ready", headers=_ADMIN)
    assert queued.status_code == 200
    before = await coordinator.snapshot()
    assert before.requested_action == "install"

    # check-now arriving before the claim must be refused, not silently downgrade
    # the queued install to another check and strand its generation.
    blocked = await client.post("/api/v1/updates/check-now", headers=_ADMIN)
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == "update_operation_in_progress"
    after = await coordinator.snapshot()
    assert after.requested_action == "install"
    assert after.action_generation == before.action_generation

    # The sidecar still claims and drives the original queued generation.
    claim = await client.post(
        "/api/v1/internal/updates/claim",
        headers=updater_headers,
        json={"expected_generation": after.action_generation},
    )
    assert claim.status_code == 200
    assert claim.json()["lease_token"] is not None
    assert claim.json()["action_generation"] == after.action_generation


async def test_no_update_defensively_clears_false_availability(
    client: httpx.AsyncClient,
    seed: SeedFn,
    updater_headers: dict[str, str],
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    await client.post("/api/v1/internal/updates/eligibility", headers=updater_headers)
    await client.post("/api/v1/updates/check-now", headers=_ADMIN)
    eligibility = await client.post("/api/v1/internal/updates/eligibility", headers=updater_headers)
    response = await client.post(
        "/api/v1/internal/updates/outcome",
        headers=updater_headers,
        json={
            "operation": "check",
            "outcome": "no_update",
            "action_generation": eligibility.json()["action_generation"],
            "current_build": "same",
            "current_digest": "sha256:same",
            "available_build": "same",
            "available_digest": "sha256:same",
        },
    )
    assert response.status_code == 200
    assert response.json()["available_build"] is None
    assert response.json()["available_digest"] is None
    assert response.json()["state"] == "disabled"
    next_eligibility = await client.post(
        "/api/v1/internal/updates/eligibility", headers=updater_headers
    )
    assert next_eligibility.json()["action"] == "none"


async def test_check_after_install_reports_check_without_stale_build_transition(
    client: httpx.AsyncClient,
    seed: SeedFn,
    updater_headers: dict[str, str],
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    await client.post("/api/v1/internal/updates/eligibility", headers=updater_headers)
    await client.post("/api/v1/updates/check-now", headers=_ADMIN)
    available = await client.post(
        "/api/v1/internal/updates/outcome",
        headers=updater_headers,
        json={
            "operation": "check",
            "outcome": "update_available",
            "action_generation": 1,
            "current_build": "old",
            "available_build": "new",
            "available_digest": "sha256:new",
        },
    )
    assert available.status_code == 200
    await client.post("/api/v1/updates/update-when-ready", headers=_ADMIN)
    claim = await client.post("/api/v1/internal/updates/claim", headers=updater_headers)
    token = claim.json()["lease_token"]
    installed = await client.post(
        "/api/v1/internal/updates/outcome",
        headers=updater_headers,
        json={
            "operation": "install",
            "outcome": "succeeded",
            "action_generation": claim.json()["action_generation"],
            "lease_token": token,
            "current_build": "new",
            "from_build": "old",
            "to_build": "new",
        },
    )
    assert installed.status_code == 200

    await client.post("/api/v1/updates/check-now", headers=_ADMIN)
    checked = await client.post(
        "/api/v1/internal/updates/outcome",
        headers=updater_headers,
        json={
            "operation": "check",
            "outcome": "no_update",
            "action_generation": 3,
            "current_build": "new",
        },
    )
    assert checked.status_code == 200
    last = checked.json()["last_result"]
    assert last["operation"] == "check"
    assert last["outcome"] == "no_update"
    assert last["from_build"] is None
    assert last["to_build"] is None


@pytest.mark.parametrize(
    ("operation", "outcome", "with_lease"),
    [
        ("check", "succeeded", False),
        ("check", "rolled_back", False),
        ("install", "no_update", True),
        ("install", "update_available", True),
    ],
)
async def test_internal_outcome_rejects_impossible_operation_result_pairs(
    client: httpx.AsyncClient,
    seed: SeedFn,
    updater_headers: dict[str, str],
    operation: str,
    outcome: str,
    with_lease: bool,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    body: dict[str, object] = {
        "operation": operation,
        "outcome": outcome,
        "action_generation": 0,
    }
    if with_lease:
        body["lease_token"] = "l" * 32
    response = await client.post(
        "/api/v1/internal/updates/outcome", headers=updater_headers, json=body
    )
    assert response.status_code == 422
    if with_lease:
        assert "l" * 32 not in response.text
        redacted = response.json()["detail"][0]["input"]["lease_token"]
        assert len(redacted) == 3 and set(redacted) == {"*"}


async def test_invalid_lease_field_is_redacted_from_validation_response(
    client: httpx.AsyncClient,
    seed: SeedFn,
    updater_headers: dict[str, str],
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    sentinel = "lease-secret-sentinel-" * 20
    response = await client.post(
        "/api/v1/internal/updates/renew",
        headers=updater_headers,
        json={"lease_token": sentinel},
    )
    assert response.status_code == 422
    assert sentinel not in response.text
    assert response.json()["detail"][0]["input"] == "***"


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

    rotated = await client.post("/api/v1/settings/app-key/rotate", headers=_ADMIN)
    assert rotated.status_code == 503
    revoked = await client.delete("/api/v1/settings/app-key", headers=_ADMIN)
    assert revoked.status_code == 503

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


async def test_automatic_claim_survives_expiry_for_exact_recovery(
    app: FastAPI,
    client: httpx.AsyncClient,
    seed: SeedFn,
    updater_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    await _enable_automatic_updates(client)
    now = [datetime(2026, 7, 12, 12, 0, tzinfo=UTC)]
    _freeze_router_clock(monkeypatch, now)
    coordinator = UpdateCoordinationService(
        app.state.sessionmaker,
        clock=lambda: now[0],
    )
    await coordinator.initialize()
    app.state.update_coordinator = coordinator
    assert await coordinator.acknowledge_action(
        expected_generation=0,
        result=UpdateResult.update_available,
        current_digest="sha256:old",
        available_digest="sha256:new",
    )

    claim = await client.post(
        "/api/v1/internal/updates/claim",
        headers=updater_headers,
        json={"expected_generation": 0},
    )
    assert claim.status_code == 200
    assert claim.json()["action_generation"] == 1
    assert claim.json()["lease_token"] is not None
    snapshot = await coordinator.snapshot()
    assert snapshot.requested_action == "install"

    now[0] += timedelta(minutes=11)
    recovered = await client.post(
        "/api/v1/internal/updates/claim",
        headers=updater_headers,
        json={"recovery": True, "expected_generation": 1},
    )
    assert recovered.status_code == 200
    assert recovered.json()["action_generation"] == 1
    assert recovered.json()["lease_token"] is not None
    stale = await client.post(
        "/api/v1/internal/updates/claim",
        headers=updater_headers,
        json={"recovery": True, "expected_generation": 0},
    )
    assert stale.status_code == 409


async def test_unknown_coordinator_phase_fails_closed_without_rewrite(
    app: FastAPI,
    client: httpx.AsyncClient,
    seed: SeedFn,
    updater_headers: dict[str, str],
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    coordinator = UpdateCoordinationService(app.state.sessionmaker)
    await coordinator.initialize()
    app.state.update_coordinator = coordinator
    async with app.state.sessionmaker() as session:
        await session.execute(
            update(UpdateCoordinatorState)
            .where(UpdateCoordinatorState.id == 1)
            .values(
                phase="future_installing",
                requested_action="install",
                action_generation=7,
                available_digest="sha256:new",
            )
        )
        await session.commit()

    eligibility = await client.post("/api/v1/internal/updates/eligibility", headers=updater_headers)
    assert eligibility.status_code == 200
    assert eligibility.json()["action"] == "none"
    assert eligibility.json()["blocker"] == "coordinator_state_unknown"
    assert (await coordinator.snapshot()).phase == "future_installing"
    assert (await client.get("/api/v1/updates/status", headers=_ADMIN)).json()["state"] == (
        "unavailable"
    )
    assert (await client.post("/api/v1/updates/check-now", headers=_ADMIN)).status_code == 409
    assert (
        await client.post(
            "/api/v1/internal/updates/claim",
            headers=updater_headers,
            json={"recovery": True, "expected_generation": 7},
        )
    ).status_code == 409
    assert (await coordinator.snapshot()).action_generation == 7

    before = await coordinator.snapshot()
    check_outcome = await client.post(
        "/api/v1/internal/updates/outcome",
        headers=updater_headers,
        json={
            "operation": "check",
            "outcome": "no_update",
            "action_generation": 7,
        },
    )
    assert check_outcome.status_code == 409
    assert check_outcome.json()["detail"] == "coordinator_state_unknown"
    install_outcome = await client.post(
        "/api/v1/internal/updates/outcome",
        headers=updater_headers,
        json={
            "lease_token": "irrelevant-because-the-phase-guard-fires-first",
            "operation": "install",
            "outcome": "succeeded",
            "action_generation": 7,
        },
    )
    assert install_outcome.status_code == 409
    assert install_outcome.json()["detail"] == "coordinator_state_unknown"
    after = await coordinator.snapshot()
    assert after.phase == before.phase == "future_installing"
    assert after.action_generation == before.action_generation == 7
    assert after.requested_action == before.requested_action == "install"
    assert after.last_result == before.last_result
    assert after.last_operation == before.last_operation


async def test_unknown_coordinator_phase_blocks_outcome_even_with_matching_generation(
    app: FastAPI,
    client: httpx.AsyncClient,
    seed: SeedFn,
    updater_headers: dict[str, str],
) -> None:
    """The phase guard alone must block the rewrite, independent of any CAS.

    Both `acknowledge_action` and `acknowledge_outcome` are generation/lease
    CAS'd, so most unknown-phase deliveries already 409 on that mismatch
    alone. This seeds a lease that *matches* the outcome request so the only
    thing standing between the unknown-phase row and a rewrite is the phase
    guard itself.
    """
    await seed(initialized=True, app_api_key=_API_KEY)
    coordinator = UpdateCoordinationService(app.state.sessionmaker)
    await coordinator.initialize()
    app.state.update_coordinator = coordinator
    assert await coordinator.acknowledge_action(
        expected_generation=0,
        result=UpdateResult.update_available,
        current_digest="sha256:old",
        available_digest="sha256:new",
    )
    await client.post("/api/v1/updates/update-when-ready", headers=_ADMIN)
    claim = await client.post(
        "/api/v1/internal/updates/claim",
        headers=updater_headers,
        json={"expected_generation": 1},
    )
    assert claim.status_code == 200
    token = claim.json()["lease_token"]
    generation = claim.json()["action_generation"]

    async with app.state.sessionmaker() as session:
        await session.execute(
            update(UpdateCoordinatorState)
            .where(UpdateCoordinatorState.id == 1)
            .values(phase="future_installing")
        )
        await session.commit()

    before = await coordinator.snapshot()
    outcome = await client.post(
        "/api/v1/internal/updates/outcome",
        headers=updater_headers,
        json={
            "lease_token": token,
            "operation": "install",
            "outcome": "succeeded",
            "action_generation": generation,
        },
    )
    assert outcome.status_code == 409
    assert outcome.json()["detail"] == "coordinator_state_unknown"
    after = await coordinator.snapshot()
    assert after.phase == before.phase == "future_installing"
    assert after.requested_action == before.requested_action
    assert after.last_result == before.last_result
    assert after.drain_owner == before.drain_owner


async def test_unknown_coordinator_phase_blocks_release_even_with_valid_lease(
    app: FastAPI,
    client: httpx.AsyncClient,
    seed: SeedFn,
    updater_headers: dict[str, str],
) -> None:
    """The phase guard alone must block the release, independent of lease validity.

    A currently-valid drain lease token is deliberately paired with an unknown
    row phase (the post-rollback version-skew window #308 addressed) so the
    only thing standing between the row and a silent phase-rewrite-to-idle is
    the phase guard itself.
    """
    await seed(initialized=True, app_api_key=_API_KEY)
    coordinator = UpdateCoordinationService(app.state.sessionmaker)
    await coordinator.initialize()
    app.state.update_coordinator = coordinator
    claim = await coordinator.claim_drain(ttl=timedelta(minutes=5))
    assert claim is not None
    token = claim.lease.token

    async with app.state.sessionmaker() as session:
        await session.execute(
            update(UpdateCoordinatorState)
            .where(UpdateCoordinatorState.id == 1)
            .values(phase="future_installing")
        )
        await session.commit()

    before = await coordinator.snapshot()
    blocked = await client.post(
        "/api/v1/internal/updates/release",
        headers=updater_headers,
        json={"lease_token": token},
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == "coordinator_state_unknown"
    after = await coordinator.snapshot()
    assert after.phase == before.phase == "future_installing"

    # The lease itself must survive the blocked attempt: once the row phase is
    # known again, releasing the same token still works, proving the guard
    # rejected the request before any row or lease mutation occurred.
    async with app.state.sessionmaker() as session:
        await session.execute(
            update(UpdateCoordinatorState)
            .where(UpdateCoordinatorState.id == 1)
            .values(phase="draining")
        )
        await session.commit()
    recovered = await client.post(
        "/api/v1/internal/updates/release",
        headers=updater_headers,
        json={"lease_token": token},
    )
    assert recovered.status_code == 200
    assert recovered.json()["blocker"] is None
    assert (await coordinator.snapshot()).phase == "idle"


async def test_known_phase_release_still_succeeds(
    app: FastAPI,
    client: httpx.AsyncClient,
    seed: SeedFn,
    updater_headers: dict[str, str],
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    coordinator = UpdateCoordinationService(app.state.sessionmaker)
    await coordinator.initialize()
    app.state.update_coordinator = coordinator
    claim = await coordinator.claim_drain(ttl=timedelta(minutes=5))
    assert claim is not None
    token = claim.lease.token
    assert (await coordinator.snapshot()).phase == "draining"

    released = await client.post(
        "/api/v1/internal/updates/release",
        headers=updater_headers,
        json={"lease_token": token},
    )
    assert released.status_code == 200
    assert released.json()["blocker"] is None
    assert (await coordinator.snapshot()).phase == "idle"


def _flip_phase_after_next_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    coordinator: UpdateCoordinationService,
    sessionmaker: async_sessionmaker[AsyncSession],
    new_phase: str,
) -> None:
    """Make the coordinator's very next ``snapshot()`` call read a real, KNOWN
    phase while a "concurrent" writer flips the underlying row to an
    unrecognized ``new_phase`` immediately afterward -- the exact TOCTOU window
    issue #322 closes. Every endpoint below calls ``snapshot()`` (directly, or
    via ``_eligibility``) exactly once as its own fast-path pre-check before
    reaching its locked write, so this reproduces "the row was known-phase when
    the endpoint snapshotted it, but had already moved to an unrecognized phase
    by the time the locked repository method re-read it" without needing real
    concurrent requests. Only the LOCKED write's own guard -- not the fast
    path, which already saw and approved a known phase -- can catch this.
    """
    original_snapshot = coordinator.snapshot
    flipped = {"done": False}

    async def snapshot_then_flip() -> CoordinatorSnapshot:
        result = await original_snapshot()
        if not flipped["done"]:
            flipped["done"] = True
            async with sessionmaker() as session:
                await session.execute(
                    update(UpdateCoordinatorState)
                    .where(UpdateCoordinatorState.id == 1)
                    .values(phase=new_phase)
                )
                await session.commit()
        return result

    monkeypatch.setattr(coordinator, "snapshot", snapshot_then_flip)


async def test_heartbeat_locked_touch_rejects_phase_that_turned_unknown_after_snapshot(
    app: FastAPI,
    client: httpx.AsyncClient,
    seed: SeedFn,
    updater_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    coordinator = UpdateCoordinationService(app.state.sessionmaker)
    await coordinator.initialize()
    app.state.update_coordinator = coordinator
    generation = await coordinator.request_action(UpdateAction.check)

    _flip_phase_after_next_snapshot(
        monkeypatch, coordinator, app.state.sessionmaker, "future_checking"
    )

    response = await client.post(
        "/api/v1/internal/updates/heartbeat",
        headers=updater_headers,
        json={"phase": "checking", "action_generation": generation},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "coordinator_state_unknown"

    async with app.state.sessionmaker() as session:
        row = (await session.execute(select(UpdateCoordinatorState))).scalar_one()
    assert row.phase == "future_checking"
    assert row.updater_last_seen_at is None


async def test_claim_recovery_locked_op_rejects_phase_that_turned_unknown_after_snapshot(
    app: FastAPI,
    client: httpx.AsyncClient,
    seed: SeedFn,
    updater_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    coordinator = UpdateCoordinationService(app.state.sessionmaker)
    await coordinator.initialize()
    app.state.update_coordinator = coordinator
    claim = await coordinator.claim_drain(
        ttl=timedelta(minutes=5),
        action_generation=0,
        materialize_install=True,
    )
    assert claim is not None
    assert claim.lease.action_generation == 1

    _flip_phase_after_next_snapshot(
        monkeypatch, coordinator, app.state.sessionmaker, "future_installing"
    )

    response = await client.post(
        "/api/v1/internal/updates/claim",
        headers=updater_headers,
        json={"recovery": True, "expected_generation": 1},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "coordinator_state_unknown"

    async with app.state.sessionmaker() as session:
        row = (await session.execute(select(UpdateCoordinatorState))).scalar_one()
        leases = (await session.execute(select(MaintenanceLease))).scalars().all()
    assert row.phase == "future_installing"
    # The original (still-live) drain lease must survive the blocked recovery
    # claim untouched -- no new lease, no released/mutated existing one.
    assert len(leases) == 1
    assert leases[0].token_hash == hashlib.sha256(claim.lease.token.encode()).hexdigest()


async def test_renew_locked_op_rejects_phase_that_turned_unknown_after_snapshot(
    app: FastAPI,
    client: httpx.AsyncClient,
    seed: SeedFn,
    updater_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    coordinator = UpdateCoordinationService(app.state.sessionmaker)
    await coordinator.initialize()
    app.state.update_coordinator = coordinator
    claim = await coordinator.claim_drain(ttl=timedelta(minutes=5))
    assert claim is not None
    token = claim.lease.token

    _flip_phase_after_next_snapshot(
        monkeypatch, coordinator, app.state.sessionmaker, "future_installing"
    )

    response = await client.post(
        "/api/v1/internal/updates/renew",
        headers=updater_headers,
        json={"lease_token": token, "phase": "installing"},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "coordinator_state_unknown"

    async with app.state.sessionmaker() as session:
        row = (await session.execute(select(UpdateCoordinatorState))).scalar_one()
        lease = (await session.execute(select(MaintenanceLease))).scalar_one()
    assert row.phase == "future_installing"
    assert lease.token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert lease.expires_at.replace(tzinfo=UTC) == claim.lease.expires_at


async def test_release_locked_op_rejects_phase_that_turned_unknown_after_snapshot(
    app: FastAPI,
    client: httpx.AsyncClient,
    seed: SeedFn,
    updater_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    coordinator = UpdateCoordinationService(app.state.sessionmaker)
    await coordinator.initialize()
    app.state.update_coordinator = coordinator
    claim = await coordinator.claim_drain(ttl=timedelta(minutes=5))
    assert claim is not None
    token = claim.lease.token

    _flip_phase_after_next_snapshot(
        monkeypatch, coordinator, app.state.sessionmaker, "future_installing"
    )

    response = await client.post(
        "/api/v1/internal/updates/release",
        headers=updater_headers,
        json={"lease_token": token},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "coordinator_state_unknown"

    async with app.state.sessionmaker() as session:
        row = (await session.execute(select(UpdateCoordinatorState))).scalar_one()
        lease = (await session.execute(select(MaintenanceLease))).scalar_one()
    assert row.phase == "future_installing"
    assert lease.token_hash == hashlib.sha256(token.encode()).hexdigest()


async def test_outcome_check_locked_op_rejects_phase_that_turned_unknown_after_snapshot(
    app: FastAPI,
    client: httpx.AsyncClient,
    seed: SeedFn,
    updater_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    coordinator = UpdateCoordinationService(app.state.sessionmaker)
    await coordinator.initialize()
    app.state.update_coordinator = coordinator
    generation = await coordinator.request_action(UpdateAction.check)

    _flip_phase_after_next_snapshot(
        monkeypatch, coordinator, app.state.sessionmaker, "future_checking"
    )

    response = await client.post(
        "/api/v1/internal/updates/outcome",
        headers=updater_headers,
        json={
            "operation": "check",
            "outcome": "no_update",
            "action_generation": generation,
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "coordinator_state_unknown"

    async with app.state.sessionmaker() as session:
        row = (await session.execute(select(UpdateCoordinatorState))).scalar_one()
    assert row.phase == "future_checking"
    assert row.requested_action == "check"
    assert row.last_result is None


async def test_outcome_install_locked_op_rejects_phase_that_turned_unknown_after_snapshot(
    app: FastAPI,
    client: httpx.AsyncClient,
    seed: SeedFn,
    updater_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    coordinator = UpdateCoordinationService(app.state.sessionmaker)
    await coordinator.initialize()
    app.state.update_coordinator = coordinator
    generation = await coordinator.request_action(UpdateAction.install)
    claim = await coordinator.claim_drain(ttl=timedelta(minutes=5), action_generation=generation)
    assert claim is not None
    token = claim.lease.token

    _flip_phase_after_next_snapshot(
        monkeypatch, coordinator, app.state.sessionmaker, "future_installing"
    )

    response = await client.post(
        "/api/v1/internal/updates/outcome",
        headers=updater_headers,
        json={
            "lease_token": token,
            "operation": "install",
            "outcome": "succeeded",
            "action_generation": generation,
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "coordinator_state_unknown"

    async with app.state.sessionmaker() as session:
        row = (await session.execute(select(UpdateCoordinatorState))).scalar_one()
        lease = (await session.execute(select(MaintenanceLease))).scalar_one()
    assert row.phase == "future_installing"
    assert row.requested_action == "install"
    assert lease.token_hash == hashlib.sha256(token.encode()).hexdigest()


async def test_token_bound_progress_reports_install_and_rollback(
    app: FastAPI,
    client: httpx.AsyncClient,
    seed: SeedFn,
    updater_headers: dict[str, str],
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    coordinator = UpdateCoordinationService(app.state.sessionmaker)
    await coordinator.initialize()
    app.state.update_coordinator = coordinator
    assert await coordinator.acknowledge_action(
        expected_generation=0,
        result=UpdateResult.update_available,
        current_digest="sha256:old",
        available_digest="sha256:new",
    )
    await client.post("/api/v1/updates/update-when-ready", headers=_ADMIN)
    claim = await client.post(
        "/api/v1/internal/updates/claim",
        headers=updater_headers,
        json={"expected_generation": 1},
    )
    token = claim.json()["lease_token"]

    installing = await client.post(
        "/api/v1/internal/updates/renew",
        headers=updater_headers,
        json={"lease_token": token, "phase": "installing"},
    )
    assert installing.status_code == 200
    status = await client.get("/api/v1/updates/status", headers=_ADMIN)
    assert status.json()["state"] == "installing"
    assert status.json()["updater_available"] is True
    for endpoint in ("check-now", "update-when-ready"):
        blocked = await client.post(f"/api/v1/updates/{endpoint}", headers=_ADMIN)
        assert blocked.status_code == 409
        assert blocked.json()["detail"] == "update_operation_in_progress"
    assert (await coordinator.snapshot()).action_generation == claim.json()["action_generation"]

    rollback = await client.post(
        "/api/v1/internal/updates/renew",
        headers=updater_headers,
        json={"lease_token": token, "phase": "rollback"},
    )
    assert rollback.status_code == 200
    assert (await client.get("/api/v1/updates/status", headers=_ADMIN)).json()[
        "state"
    ] == "rollback"
    outcome = await client.post(
        "/api/v1/internal/updates/outcome",
        headers=updater_headers,
        json={
            "lease_token": token,
            "operation": "install",
            "outcome": "rolled_back",
            "action_generation": claim.json()["action_generation"],
        },
    )
    assert outcome.status_code == 200
    assert (await coordinator.snapshot()).drain_owner is None


async def test_automatic_idle_only_status_matches_claim_blocker(
    app: FastAPI,
    client: httpx.AsyncClient,
    seed: SeedFn,
    updater_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    await _enable_automatic_updates(client)
    _freeze_router_clock(monkeypatch, [datetime(2026, 7, 12, 12, 0, tzinfo=UTC)])
    coordinator = UpdateCoordinationService(app.state.sessionmaker)
    await coordinator.initialize()
    app.state.update_coordinator = coordinator
    assert await coordinator.acknowledge_action(
        expected_generation=0,
        result=UpdateResult.update_available,
        current_digest="sha256:old",
        available_digest="sha256:new",
    )
    critical = await coordinator.acquire_critical("import")
    assert critical is not None

    status = await client.get("/api/v1/updates/status", headers=_ADMIN)
    assert status.json()["state"] == "waiting_for_idle"
    assert status.json()["blocker"] == "active_critical_work"
    eligibility = await client.post("/api/v1/internal/updates/eligibility", headers=updater_headers)
    assert eligibility.json()["blocker"] == "active_critical_work"
    claim = await client.post(
        "/api/v1/internal/updates/claim",
        headers=updater_headers,
        json={"expected_generation": eligibility.json()["action_generation"]},
    )
    assert claim.json()["lease_token"] is None
    assert claim.json()["blocker"] == "active_critical_work"
    await coordinator.release(critical.token)
    assert (await client.get("/api/v1/updates/status", headers=_ADMIN)).json()[
        "state"
    ] == "update_available"
