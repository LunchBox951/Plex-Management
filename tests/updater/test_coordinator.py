"""Authenticated, fail-closed updater coordinator client contract."""

from __future__ import annotations

import json

import httpx

from plex_manager.updater.coordinator import CoordinatorClient

_TOKEN = "coordinator-test-token-0123456789"  # noqa: S105 - synthetic test credential
_LEASE_TOKEN = "lease-token-1234567890"  # noqa: S105 - synthetic test credential


async def test_busy_claim_with_null_token_is_a_normal_deferral() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "lease_token": None,
                "action_generation": 4,
                "ready": False,
                "lease_seconds": 30,
                "blocker": "critical_work_active",
            },
        )

    async with httpx.AsyncClient(
        base_url="http://coordinator/api/v1/internal/updates/",
        transport=httpx.MockTransport(handler),
    ) as http:
        client = CoordinatorClient(
            "http://coordinator/api/v1/internal/updates", _TOKEN, timeout=1, client=http
        )
        claim = await client.claim()

    assert claim.lease_token is None
    assert claim.action_generation == 4
    assert claim.ready is False
    assert claim.lease_seconds == 30
    assert claim.blocker == "critical_work_active"
    assert seen[0].url.path == "/api/v1/internal/updates/claim"
    assert seen[0].headers["Authorization"] == f"Bearer {_TOKEN}"
    assert seen[0].headers["Host"] == "127.0.0.1"


async def test_renew_response_reuses_request_token_without_expect_response_token() -> None:
    lease_token = "l" * 32

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {"lease_token": lease_token}
        return httpx.Response(
            200,
            json={"ready": True, "lease_seconds": 120, "blocker": None},
        )

    async with httpx.AsyncClient(
        base_url="http://coordinator/api/v1/internal/updates/",
        transport=httpx.MockTransport(handler),
    ) as http:
        client = CoordinatorClient(
            "http://coordinator/api/v1/internal/updates", _TOKEN, timeout=1, client=http
        )
        renewed = await client.renew(lease_token)

    assert renewed.lease_token == lease_token
    assert renewed.ready is True


async def test_outcome_sends_all_observation_and_transition_fields() -> None:
    seen_body: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_body
        seen_body = json.loads(request.content)
        return httpx.Response(200, json={"acknowledged": True})

    async with httpx.AsyncClient(
        base_url="http://coordinator/api/v1/internal/updates/",
        transport=httpx.MockTransport(handler),
    ) as http:
        client = CoordinatorClient(
            "http://coordinator/api/v1/internal/updates", _TOKEN, timeout=1, client=http
        )
        await client.outcome(
            operation="install",
            outcome="rolled_back",
            lease_token=_LEASE_TOKEN,
            current_digest="repo@sha256:old",
            available_digest="repo@sha256:new",
            current_build="build-old",
            available_build="build-new",
            from_build="build-old",
            to_build="build-new",
            detail_code="replacement_unhealthy",
        )

    assert seen_body == {
        "operation": "install",
        "outcome": "rolled_back",
        "lease_token": _LEASE_TOKEN,
        "current_digest": "repo@sha256:old",
        "available_digest": "repo@sha256:new",
        "current_build": "build-old",
        "available_build": "build-new",
        "from_build": "build-old",
        "to_build": "build-new",
        "detail_code": "replacement_unhealthy",
    }


async def test_outcome_omits_unknown_optional_fields() -> None:
    seen_body: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_body
        seen_body = json.loads(request.content)
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(
        base_url="http://coordinator/api/v1/internal/updates/",
        transport=httpx.MockTransport(handler),
    ) as http:
        client = CoordinatorClient(
            "http://coordinator/api/v1/internal/updates", _TOKEN, timeout=1, client=http
        )
        await client.outcome(operation="check", outcome="no_update")

    assert seen_body == {"operation": "check", "outcome": "no_update"}
