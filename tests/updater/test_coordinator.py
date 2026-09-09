"""Authenticated, fail-closed updater coordinator client contract."""

from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from plex_manager.updater.coordinator import (
    _MAX_JSON_BODY_BYTES,  # pyright: ignore[reportPrivateUsage]
    CoordinatorClient,
    CoordinatorError,
)

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


async def test_non_2xx_app_error_envelope_logs_detail_code_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """issue #539: a non-2xx coordinator response whose body carries a
    ``detail`` field equal to one of the exact codes the coordinator's
    internal endpoints can actually send (``updater_coordinator_unavailable``
    -- ``web/updater_auth.py``/``web/routers/updates.py``'s ``_coordinator()``
    helper, status 503, the same code family behind the real 2026-07-28
    canary recurrence) must log that code -- while keeping the existing
    ``coordinator_unavailable`` classification.

    ``message`` is no longer read or logged at all (see
    ``test_non_2xx_look_alike_envelope_never_logs_message_secret`` below for
    why), so this only pins the ``detail``-only echo.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            json={
                "detail": "updater_coordinator_unavailable",
                "message": "The update coordinator is not available.",
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
        and "status=503" in message
        and "detail=updater_coordinator_unavailable" in message
        for message in messages
    )
    # message is never read, let alone logged -- not even redacted.
    assert all("message=" not in message for message in messages)
    # The bearer token must never reach the log.
    assert all(_TOKEN not in message for message in messages)


async def test_non_2xx_look_alike_envelope_never_logs_message_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """issue #539 review round 4: a body with string ``detail``/``message``
    fields does not authenticate origin -- round 3's shape-only predicate let
    a look-alike proxy/relay body pass with a secret-bearing URL riding
    ``message`` (neither ``safe_text`` nor ``redact_secrets`` is guaranteed
    to catch an UNLABELED secret in free text). ``message`` is now never
    read at all, so the secret cannot reach the log regardless of whether
    the surrounding body is a genuine ``AppError`` envelope or a look-alike
    -- while the ``detail`` code (the actual diagnostic payload) still
    echoes normally."""
    secret_marker = "SECRETTOKEN9999"  # noqa: S105 - synthetic test marker, not a credential

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            json={
                "detail": "updater_coordinator_unavailable",
                "message": f"See https://host/download/{secret_marker} for details",
            },
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
    message = messages[0]
    assert "detail=updater_coordinator_unavailable" in message
    # The secret (and the whole message field) must be absent from EVERY
    # captured record, not just this one.
    assert all(secret_marker not in record.getMessage() for record in caplog.records)
    assert "message=" not in message


async def test_non_2xx_non_conforming_detail_is_treated_as_opaque(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A ``detail`` field that IS a string but is not one of the exact known
    codes (here it embeds a secret-bearing URL itself) must fall through to
    the opaque branch exactly like a missing/wrong-typed ``detail`` -- the
    allowlist is exact-match, not a best-effort filter (issue #539 review
    round 5)."""
    secret_marker = "DETAILSECRET42"  # noqa: S105 - synthetic test marker, not a credential

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            json={
                "detail": f"see https://host/download/{secret_marker}",
                "message": "irrelevant",
            },
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
    assert all(secret_marker not in record.getMessage() for record in caplog.records)
    assert "(opaque body)" in messages[0]
    assert "status=500" in messages[0]


async def test_non_2xx_hex_credential_shaped_detail_is_treated_as_opaque(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """issue #539 review round 5: a bare lowercase-hex credential
    (``{"detail": "deadbeef..."}``) fullmatches round 4's
    ``[a-z0-9_]{1,64}`` charset check just as well as a real machine code --
    the charset check alone could not tell them apart. The round-5 EXACT
    allowlist closes this: a hex-shaped string is not a MEMBER of
    ``_KNOWN_COORDINATOR_DETAIL_CODES`` regardless of how code-like it
    looks, so it falls to the opaque branch and never reaches the log by
    name."""
    hex_credential = "deadbeefcafe1234567890abcdef1234567890abcdef12"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": hex_credential, "message": "irrelevant"})

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
    assert all(hex_credential not in record.getMessage() for record in caplog.records)
    assert "(opaque body)" in messages[0]
    assert "detail=" not in messages[0]


async def test_non_2xx_content_type_parameter_is_stripped_before_logging(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """issue #539 review round 4: a ``Content-Type`` parameter can smuggle a
    secret (``application/json; source="https://host/...secret..."``) past a
    naive whole-header log. Only the media type before the first ``;`` is
    ever logged, and only when it is a MEMBER of the exact known-media-type
    allowlist (round 5)."""
    secret_marker = "CTSECRET777"  # noqa: S105 - synthetic test marker, not a credential

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            502,
            text="upstream proxy error",
            headers={
                "content-type": f'application/json; source="https://internal.example/{secret_marker}"'
            },
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
    message = messages[0]
    assert all(secret_marker not in record.getMessage() for record in caplog.records)
    # The parameter is stripped entirely -- only the bare media type remains.
    assert "content_type=application/json" in message
    assert "source=" not in message


async def test_non_2xx_credential_shaped_media_type_logs_other(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """issue #539 review round 5: round 4's RFC 6838 token-charset check
    still accepted a credential-shaped subtype
    (``application/SECRETTOKEN9999`` is valid token syntax, so it fullmatched
    the regex and would have been logged verbatim). The exact
    ``_KNOWN_MEDIA_TYPES`` allowlist closes this: only literally
    ``application/json``/``application/problem+json``/``text/plain``/
    ``text/html`` are ever echoed; anything else -- including this
    syntactically-valid-but-unknown subtype -- logs the fixed marker
    ``content_type=other``."""
    secret_marker = "SECRETTOKEN9999"  # noqa: S105 - synthetic test marker, not a credential

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            502,
            text="upstream proxy error",
            headers={"content-type": f"application/{secret_marker}"},
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
    assert all(secret_marker not in record.getMessage() for record in caplog.records)
    assert "content_type=other" in messages[0]


async def test_non_2xx_unknown_but_innocent_content_type_logs_other(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A ``Content-Type`` that is well-formed and entirely innocuous but
    simply isn't in the exact known-media-type set (e.g. an XML error body
    some proxies emit) still logs the fixed marker ``content_type=other`` --
    the allowlist is exact-match, not a best-effort filter (issue #539
    review round 5)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            502, text="<error>bad gateway</error>", headers={"content-type": "application/xml"}
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
    assert "content_type=other" in messages[0]


async def test_non_2xx_deeply_nested_json_body_still_classifies_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """issue #539 review round 4: pathologically deep JSON nesting makes
    ``response.json()`` raise ``RecursionError`` -- not a ``ValueError`` --
    which a ``ValueError``-only catch would let escape ``_log_non_2xx``
    uncaught, aborting ``_post`` before it ever raises ``CoordinatorError``.
    Classification must complete regardless, falling through to the opaque
    branch (a JSON body this deeply nested is certainly not a conforming
    ``{"detail": "<code>"}`` envelope)."""
    nested_body = ("[" * 3000) + ("]" * 3000)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500, content=nested_body.encode("ascii"), headers={"content-type": "application/json"}
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
    assert len(messages) == 1
    assert "(opaque body)" in messages[0]
    assert "status=500" in messages[0]
    assert f"content_length={len(nested_body)}" in messages[0]


def _padded_envelope(total_bytes: int) -> bytes:
    """A syntactically valid app envelope padded with an ignored key so the
    encoded body is exactly ``total_bytes`` long."""
    prefix = b'{"detail": "updater_coordinator_unavailable", "pad": "'
    suffix = b'"}'
    padding = total_bytes - len(prefix) - len(suffix)
    assert padding >= 0
    body = prefix + (b"x" * padding) + suffix
    assert len(body) == total_bytes
    return body


def _forbid_json_decoding(monkeypatch: pytest.MonkeyPatch) -> None:
    def _never(*args: object, **kwargs: object) -> object:
        pytest.fail("response.json() must not run on an oversized body")

    monkeypatch.setattr(httpx.Response, "json", _never)


async def test_non_2xx_oversized_valid_envelope_is_opaque_without_decoding(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """issue #573: a non-2xx body larger than ``_MAX_JSON_BODY_BYTES`` must
    take the opaque branch without ever being handed to ``response.json()``,
    even when it is a well-formed envelope with a known ``detail`` -- the
    real envelope is tiny, and decoding an oversized one would allocate a
    second full copy inside the long-running sidecar."""
    body = _padded_envelope(_MAX_JSON_BODY_BYTES + 1)
    _forbid_json_decoding(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=body, headers={"content-type": "application/json"})

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
    assert len(messages) == 1
    assert "(opaque body)" in messages[0]
    assert "detail=" not in messages[0]
    assert "status=503" in messages[0]
    assert f"content_length={len(body)}" in messages[0]


async def test_non_2xx_envelope_at_exact_cap_still_logs_detail_code(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """issue #573 boundary: a body of exactly ``_MAX_JSON_BODY_BYTES`` is
    still decoded, so a known ``detail`` at the cap logs by name."""
    body = _padded_envelope(_MAX_JSON_BODY_BYTES)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=body, headers={"content-type": "application/json"})

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
    assert len(messages) == 1
    assert "(app envelope)" in messages[0]
    assert "detail=updater_coordinator_unavailable" in messages[0]


async def test_2xx_oversized_body_is_invalid_response_without_decoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """issue #573: the same cap guards the 2xx contract path. An oversized
    success body is rejected as ``coordinator_invalid_response`` (the
    classification ``_optional_string`` already uses for an over-length
    field) before ``response.json()`` runs."""
    body = (
        b'{"action": "none", "action_generation": 1, "pad": "'
        + (b"x" * _MAX_JSON_BODY_BYTES)
        + b'"}'
    )
    _forbid_json_decoding(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    async with httpx.AsyncClient(
        base_url="http://coordinator/api/v1/internal/updates/",
        transport=httpx.MockTransport(handler),
    ) as http:
        client = CoordinatorClient(
            "http://coordinator/api/v1/internal/updates", _TOKEN, timeout=1, client=http
        )
        with pytest.raises(CoordinatorError) as exc_info:
            await client.eligibility()

    assert exc_info.value.code == "coordinator_invalid_response"


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
