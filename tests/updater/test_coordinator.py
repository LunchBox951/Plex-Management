"""Authenticated, fail-closed updater coordinator client contract."""

from __future__ import annotations

import json

import httpx
import pytest

from plex_manager.logsafe import redact_secrets, safe_text
from plex_manager.updater.coordinator import CoordinatorClient, CoordinatorError

_TOKEN = "coordinator-test-token-0123456789"  # noqa: S105 - synthetic test credential
_LEASE_TOKEN = "lease-token-1234567890"  # noqa: S105 - synthetic test credential


async def test_eligibility_and_heartbeat_are_generation_bound() -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/eligibility"):
            return httpx.Response(
                200,
                json={
                    "action": "check",
                    "action_generation": 9,
                    "automatic_enabled": True,
                    "window_open": True,
                    "idle_only": True,
                    "blocker": None,
                },
            )
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"ready": False, "lease_seconds": 600})

    async with httpx.AsyncClient(
        base_url="http://coordinator/api/v1/internal/updates/",
        transport=httpx.MockTransport(handler),
    ) as http:
        client = CoordinatorClient(
            "http://coordinator/api/v1/internal/updates", _TOKEN, timeout=1, client=http
        )
        eligibility = await client.eligibility()
        await client.heartbeat(action_generation=eligibility.action_generation)

    assert eligibility.action_generation == 9
    assert seen == [{"phase": "checking", "action_generation": 9}]


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
            action_generation=7,
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
        "action_generation": 7,
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
        await client.outcome(operation="check", outcome="no_update", action_generation=3)

    assert seen_body == {
        "operation": "check",
        "outcome": "no_update",
        "action_generation": 3,
    }


async def test_non_2xx_response_logs_status_and_truncated_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """issue #539: a non-2xx coordinator response must log enough to diagnose
    a future recurrence -- the status code and a bounded slice of the body --
    while keeping the existing ``coordinator_unavailable`` classification."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal coordinator failure: lease store unreachable")

    async with httpx.AsyncClient(
        base_url="http://coordinator/api/v1/internal/updates/",
        transport=httpx.MockTransport(handler),
    ) as http:
        client = CoordinatorClient(
            "http://coordinator/api/v1/internal/updates", _TOKEN, timeout=1, client=http
        )
        with caplog.at_level("WARNING"), pytest.raises(CoordinatorError) as exc_info:
            await client.eligibility()

    assert exc_info.value.code == "coordinator_unavailable"
    messages = [record.message for record in caplog.records if record.levelname == "WARNING"]
    assert any(
        "status=500" in message and "lease store unreachable" in message for message in messages
    )
    # The bearer token must never reach the log.
    assert all(_TOKEN not in message for message in messages)


async def test_non_2xx_response_body_is_truncated_and_line_boundary_safe(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A pathological body must be capped BEFORE the line-boundary/redaction
    barriers run (no log-flooding), and a CR/LF *within* the cap must never be
    able to forge a second log record (issue #539).

    The CR/LF sits at offset 100 -- well inside the 500-char cap -- so cap and
    barrier are both exercised by this one body: a prior version of this test
    put the CR/LF past the cap, where truncation alone satisfied every
    assertion (a ``safe_text`` -> identity mutation left it green). The
    expectation is built from the SAME real ``safe_text``/``redact_secrets``
    pipeline the call site runs, so mutating either one out of ``_post``
    breaks the equality below.
    """
    oversized_body = ("x" * 100) + "\r\nFAKE LOG LINE INJECTED" + ("x" * 600)
    expected_logged_body = redact_secrets(safe_text(oversized_body[:500]))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text=oversized_body)

    async with httpx.AsyncClient(
        base_url="http://coordinator/api/v1/internal/updates/",
        transport=httpx.MockTransport(handler),
    ) as http:
        client = CoordinatorClient(
            "http://coordinator/api/v1/internal/updates", _TOKEN, timeout=1, client=http
        )
        with caplog.at_level("WARNING"), pytest.raises(CoordinatorError):
            await client.eligibility()

    messages = [record.message for record in caplog.records if record.levelname == "WARNING"]
    assert len(messages) == 1
    logged_body = messages[0].rsplit("body=", 1)[1]
    assert logged_body == expected_logged_body
    # Sanity checks spelling out what the equality above proves: the barrier
    # neutralized the CR/LF (rather than the cap coincidentally slicing it
    # away -- it didn't, the phrase is still present, just on one line), and
    # only the first 500 raw chars were ever considered.
    assert "\r" not in logged_body
    assert "\n" not in logged_body
    assert "FAKE LOG LINE INJECTED" in logged_body
    assert logged_body.count("x") == 476


async def test_transport_error_does_not_log_http_status_details(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A transport-level failure (no response at all) has no status/body to
    report and must not emit the HTTP-status warning path (issue #539)."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with httpx.AsyncClient(
        base_url="http://coordinator/api/v1/internal/updates/",
        transport=httpx.MockTransport(handler),
    ) as http:
        client = CoordinatorClient(
            "http://coordinator/api/v1/internal/updates", _TOKEN, timeout=1, client=http
        )
        with caplog.at_level("WARNING"), pytest.raises(CoordinatorError) as exc_info:
            await client.eligibility()

    assert exc_info.value.code == "coordinator_unavailable"
    assert caplog.records == []
