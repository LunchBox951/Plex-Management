"""Authenticated, fail-closed updater coordinator client contract."""

from __future__ import annotations

import hashlib
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


async def test_non_2xx_app_error_envelope_logs_detail_and_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """issue #539: a non-2xx coordinator response whose body is the app's own
    ``AppError`` JSON envelope (``web/errors.py``'s ``{"detail": ..., ...
    "message": ...}``) must log the recognized ``detail``/``message`` fields
    -- e.g. the actual 2026-07-28 canary recurrence, which was app-origin
    JSON -- while keeping the existing ``coordinator_unavailable``
    classification."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            json={
                "detail": "coordinator_lease_store_unreachable",
                "message": "The lease store connection pool is exhausted.",
            },
        )

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
        "(app envelope)" in message
        and "status=500" in message
        and "detail=coordinator_lease_store_unreachable" in message
        and "message=The lease store connection pool is exhausted." in message
        for message in messages
    )
    # The bearer token must never reach the log.
    assert all(_TOKEN not in message for message in messages)


async def test_non_2xx_app_error_envelope_message_is_capped_and_line_boundary_safe(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A pathological ``message`` field must be capped BEFORE the line-
    boundary/redaction barriers run (no log-flooding), and a CR/LF *within*
    the cap must never be able to forge a second log record (issue #539).

    The CR/LF sits at offset 100 -- well inside the 500-char field cap -- so
    cap and barrier are both exercised. A JSON string value can only ever
    carry a CR/LF as the ``\\r\\n`` escape sequence (valid JSON has no raw
    control byte in a string), so ``response.json()`` decodes it back to a
    real CR/LF before this body ever reaches ``_log_non_2xx`` -- exactly the
    shape a hostile/malfunctioning coordinator could still send even though
    it is JSON. The expectation is built from the SAME real ``safe_text``/
    ``redact_secrets`` pipeline the call site runs, so mutating either one
    out of ``_log_non_2xx`` breaks the equality below.
    """
    oversized_message = ("x" * 100) + "\r\nFAKE LOG LINE INJECTED" + ("x" * 600)
    expected_logged_message = redact_secrets(safe_text(oversized_message[:500]))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503, json={"detail": "coordinator_busy", "message": oversized_message}
        )

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
    logged_message = messages[0].rsplit("message=", 1)[1]
    assert logged_message == expected_logged_message
    # Sanity checks spelling out what the equality above proves: the barrier
    # neutralized the CR/LF (rather than the cap coincidentally slicing it
    # away -- it didn't, the phrase is still present, just on one line), and
    # only the first 500 raw chars were ever considered.
    assert "\r" not in logged_message
    assert "\n" not in logged_message
    assert "FAKE LOG LINE INJECTED" in logged_message
    assert logged_message.count("x") == 476


async def test_non_2xx_non_app_shape_body_never_logs_body_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """issue #539 review round 3: an intermediary/proxy debug page (or any
    body that is not the app's own AppError envelope) could carry an
    unlabeled token or a secret-bearing URL that neither ``safe_text``
    (line-boundary only) nor ``redact_secrets`` (key/shape-based) is
    guaranteed to catch -- truncating it would still leak a shorter prefix.
    So NO body text is ever logged for this shape: only status, content
    type, byte length, and an irreversible fingerprint."""
    secret_marker = "SECRETTOKEN1234567890"  # noqa: S105 - synthetic test marker, not a credential
    body = (
        "<html><body>Bad Gateway: see "
        f"https://internal-proxy.example/download/{secret_marker} for details</body></html>"
    )
    body_bytes = body.encode("utf-8")
    expected_fingerprint = hashlib.sha256(body_bytes).hexdigest()[:12]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, content=body_bytes, headers={"content-type": "text/html"})

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
    message = messages[0]
    # The raw marker (and thus the whole secret-bearing URL/body) must be
    # absent from every captured record, not just this one -- a defense-in-
    # depth check that no other handler/formatter re-introduced it either.
    assert all(secret_marker not in record.getMessage() for record in caplog.records)
    assert "(opaque body)" in message
    assert "status=502" in message
    assert "content_type=text/html" in message
    assert f"content_length={len(body_bytes)}" in message
    assert f"fingerprint={expected_fingerprint}" in message


async def test_non_2xx_json_with_wrong_field_shape_is_treated_as_opaque(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A body that is valid JSON but does not match the AppError envelope --
    e.g. FastAPI's OWN ``{"detail": [...]}`` validation-error envelope, whose
    ``detail`` is a list, not the app's machine-code string -- must be
    treated exactly as opaque as non-JSON text, never partially trusted."""
    marker = "should never leak into the coordinator log"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422, json={"detail": [{"msg": marker, "type": "value_error", "loc": ["body"]}]}
        )

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
    assert marker not in messages[0]
    assert "(opaque body)" in messages[0]
    assert "status=422" in messages[0]


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
