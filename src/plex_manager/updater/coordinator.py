"""Authenticated client for the app-owned update coordinator."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Final, Literal, cast

import httpx

Action = Literal["none", "check", "install"]
Outcome = Literal["no_update", "update_available", "succeeded", "failed", "rolled_back"]

_logger = logging.getLogger(__name__)

#: EXACT enumeration of every ``detail`` code the coordinator's internal
#: endpoints (``/eligibility``, ``/heartbeat``, ``/claim``, ``/renew``,
#: ``/release``, ``/outcome`` -- the only paths this client ever calls) can
#: put in a non-2xx response body (issue #539 review round 5). A charset
#: allowlist (round 4) still passed a bare credential-shaped string
#: (``{"detail": "deadbeef..."}`` fullmatches ``[a-z0-9_]{1,64}`` just as
#: well as a real code) -- an exact set of the finite, ACTUAL codes closes
#: that permanently: nothing outside this enumeration can ever be logged by
#: name, full stop, regardless of what charset it happens to use.
#:
#: Sourced by reading every ``AppError(code=...)`` reachable from these six
#: paths, 2026-08-10 (issue #539 review round 5):
#:   * ``web/trusted_host.py`` (``TrustedHostMiddleware``, wraps every
#:     request): ``invalid_host``.
#:   * ``web/updater_auth.py`` (``require_updater``, the internal router's
#:     own auth dependency): ``updater_coordinator_unavailable``,
#:     ``invalid_updater_credential``.
#:   * ``web/routers/updates.py``'s ``_coordinator()`` helper (called by
#:     every one of the six endpoints): ``updater_coordinator_unavailable``.
#:   * ``web/routers/updates.py``'s ``_guard_unknown_phase()`` helper plus
#:     each endpoint's own pre-call snapshot check: ``coordinator_state_
#:     unknown``.
#:   * ``heartbeat_endpoint``/``claim_endpoint``/``outcome_endpoint``:
#:     ``update_action_generation_mismatch``.
#:   * ``claim_endpoint``: ``update_recovery_generation_mismatch``,
#:     ``update_not_eligible``.
#:   * ``renew_endpoint``/``outcome_endpoint``: ``update_lease_expired``.
#:   * ``outcome_endpoint``: ``missing_update_lease``.
#:   * ``web/middleware.py``'s ``SetupGuardMiddleware`` (wraps every
#:     ``/api/`` path except the setup/auth sub-APIs, and
#:     ``/api/v1/internal/updates`` is NOT in that exemption list):
#:     ``setup_required``. ``CriticalMutationMiddleware`` is EXCLUDED for
#:     this prefix (``_MAINTENANCE_EXCLUDED_PREFIXES``), so its
#:     ``maintenance_*`` codes can never reach this client and are
#:     deliberately NOT in this set.
#:
#: Every code above whose status is 409 (``coordinator_state_unknown``, both
#: generation-mismatch codes, ``update_not_eligible``, ``update_lease_
#: expired``, ``setup_required``) is currently intercepted by ``_post``'s
#: own ``status_code == 409`` special case BEFORE ``_log_non_2xx`` ever
#: runs, so none of them are reachable through THIS path today -- they are
#: listed anyway for completeness, so this set stays a complete audit of
#: "codes these endpoints can send" rather than a snapshot of today's
#: control flow (which the next refactor could silently change).
#:
#: A NEW app code these endpoints can return must be added here to ever be
#: logged by name; until then it -- like anything else not in this set --
#: falls to the opaque branch below, still diagnosable via status +
#: fingerprint.
_KNOWN_COORDINATOR_DETAIL_CODES: Final[frozenset[str]] = frozenset(
    {
        "invalid_host",
        "updater_coordinator_unavailable",
        "invalid_updater_credential",
        "coordinator_state_unknown",
        "update_action_generation_mismatch",
        "update_recovery_generation_mismatch",
        "update_not_eligible",
        "update_lease_expired",
        "missing_update_lease",
        "setup_required",
    }
)

#: EXACT enumeration of the media types ever logged for a non-app-shape body
#: (issue #539 review round 5): round 4's RFC 6838 token-charset check still
#: passed a credential-shaped subtype (``application/SECRETTOKEN9999`` is
#: valid token syntax). ``application/json`` is the only media type the app
#: itself ever emits (every response is a Starlette ``JSONResponse``);
#: ``text/plain``/``text/html`` are what a reverse proxy or load balancer
#: typically emits for its OWN error page (a 502/504/413 the app never saw);
#: ``application/problem+json`` is the RFC 7807 convention some API
#: gateways/ingress controllers use for a structured error body. Anything
#: else logs the fixed marker ``content_type=other`` rather than any part of
#: the raw header.
_KNOWN_MEDIA_TYPES: Final[frozenset[str]] = frozenset(
    {"application/json", "application/problem+json", "text/plain", "text/html"}
)

#: Hex digits of the SHA-256 fingerprint logged for a non-app-shape body
#: (issue #539 review round 3) -- long enough to correlate repeated
#: occurrences of the SAME opaque body across log lines, short enough to stay
#: unmistakably a fingerprint rather than a hash an operator might try to
#: reverse. Mirrors :func:`~plex_manager.logsafe.safe_guid`'s own redaction
#: hash length.
_FINGERPRINT_HEX_CHARS: Final = 12


class CoordinatorError(RuntimeError):
    """The coordinator was unavailable or returned an invalid contract."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Eligibility:
    action: Action
    action_generation: int
    blocker: str | None


@dataclass(frozen=True)
class LeaseStatus:
    lease_token: str | None
    ready: bool
    lease_seconds: int
    blocker: str | None
    action_generation: int | None = None


def _object(response: httpx.Response) -> dict[str, object]:
    try:
        value: object = response.json()
    # httpx.Response.json() delegates to the stdlib json module: a malformed
    # body raises json.JSONDecodeError (a ValueError subclass), and CPython's
    # JSON scanner raises RecursionError -- not a ValueError -- on
    # pathologically deep nesting (issue #539 review round 4). Catching only
    # ValueError would let that escape uncaught here instead of the intended
    # coordinator_invalid_json classification.
    except (ValueError, RecursionError) as exc:
        raise CoordinatorError("coordinator_invalid_json") from exc
    if not isinstance(value, dict):
        raise CoordinatorError("coordinator_invalid_response")
    return cast(dict[str, object], value)


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 128:
        raise CoordinatorError("coordinator_invalid_response")
    return value


def _extract_safe_detail_code(response: httpx.Response) -> str | None:
    """Extract a non-2xx coordinator body's ``detail`` field, but ONLY when
    it is a string that is a MEMBER of :data:`_KNOWN_COORDINATOR_DETAIL_CODES`
    -- the exact, finite set of codes these endpoints can actually send
    (issue #539 review round 5). ``None`` for anything else: not JSON, not a
    dict, a missing/non-string ``detail``, or a ``detail`` that IS a string
    but is not one of the known codes.

    Round 3 recognized the app's ``AppError`` envelope by SHAPE (a dict with
    string ``detail``/``message`` fields) and echoed both -- exploitable by a
    look-alike body. Round 4 tightened to a CHARSET check on ``detail``
    alone (``[a-z0-9_]{1,64}``) -- still exploitable: a bare credential that
    happens to be lowercase hex (``{"detail": "deadbeef1234..."}``)
    fullmatches that charset just as well as a real code. Round 5 closes
    this permanently with an EXACT allowlist: ``detail`` is logged ONLY when
    it equals one of the finite codes these six endpoints are actually
    capable of returning, so nothing outside that enumeration -- of any
    shape, charset, or length -- can ever be logged by name. A brand new app
    code these endpoints start returning must be added to
    :data:`_KNOWN_COORDINATOR_DETAIL_CODES` to be logged this way; until
    then it falls to the opaque branch like anything else unrecognized.
    ``message`` remains unread: free-text prose has no finite vocabulary the
    way a code does.
    """
    try:
        parsed: object = response.json()
    # See _object's identical comment: json.JSONDecodeError (a ValueError
    # subclass) covers malformed JSON, but CPython's scanner raises
    # RecursionError -- not a ValueError -- on pathologically deep nesting
    # (issue #539 review round 4), and that must still resolve to "not the
    # app envelope" rather than escape this method uncaught.
    except (ValueError, RecursionError):
        return None
    if not isinstance(parsed, dict):
        return None
    detail = cast("dict[str, object]", parsed).get("detail")
    if not isinstance(detail, str) or detail not in _KNOWN_COORDINATOR_DETAIL_CODES:
        return None
    return detail


def _safe_media_type(content_type_header: str) -> str:
    """Reduce a ``Content-Type`` header to a loggable media type (issue #539
    review round 5): only the portion before the first ``;`` is even
    considered (a parameter such as ``application/json; source="https://
    host/...secret..."`` could otherwise smuggle a secret past the log), and
    that portion is returned ONLY when it is a MEMBER of
    :data:`_KNOWN_MEDIA_TYPES` -- the exact, finite set of media types the
    app and its typical intermediaries actually emit. Round 4's RFC 6838
    token-charset check still passed a credential-shaped subtype
    (``application/SECRETTOKEN9999`` is valid token syntax); an exact
    allowlist closes that permanently. Anything else (missing, empty, or any
    media type outside the known set) logs the fixed marker ``"other"``
    rather than any part of the raw header value.
    """
    media_type = content_type_header.split(";", 1)[0].strip().lower()
    if media_type not in _KNOWN_MEDIA_TYPES:
        return "other"
    return media_type


class CoordinatorClient:
    """Small fail-closed client; no Docker identifier crosses this boundary."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            timeout=timeout,
            trust_env=False,
        )
        # The service DNS name need not become a publicly trusted Host. The bearer
        # credential remains mandatory; this only lets the request through the
        # app's existing trusted-host middleware.
        self._headers = {"Authorization": f"Bearer {token}", "Host": "127.0.0.1"}

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _post(self, path: str, body: dict[str, object] | None = None) -> dict[str, object]:
        try:
            response = await self._client.post(path, headers=self._headers, json=body)
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            # Transport-level failure (connection refused/reset, timeout, no
            # response at all): there is no status code or body to report, so
            # this branch stays a bare classification. Logged distinctly from
            # the HTTP-status branch below by message wording -- if this ever
            # grows its own logging, the two must stay tellable apart.
            raise CoordinatorError("coordinator_unavailable") from exc
        if response.status_code == 409:
            raise CoordinatorError("coordinator_conflict")
        try:
            response.raise_for_status()
        except httpx.HTTPError as exc:
            # A response DID arrive, so unlike the transport-error branch
            # above there is real diagnostic evidence (issue #539 -- the
            # 2026-07-28 canary 500s on /eligibility left no evidence because
            # this classification was previously silent): log it, but never
            # echo arbitrary response text (review rounds 3-5 -- see
            # _log_non_2xx/_extract_safe_detail_code for why).
            self._log_non_2xx(path, response)
            raise CoordinatorError("coordinator_unavailable") from exc
        return _object(response)

    def _log_non_2xx(self, path: str, response: httpx.Response) -> None:
        """Log a non-2xx coordinator response without ever echoing arbitrary
        response text (issue #539 review rounds 3-5).

        Neither body SHAPE (round 3: a look-alike body can carry its own
        string ``detail``/``message`` fields) nor a CHARSET check on
        ``detail`` alone (round 4: a bare credential can happen to be
        lowercase hex) authenticates origin. Only :func:`_extract_safe_detail_
        code`'s EXACT allowlist match -- ``detail`` equal to one of the
        finite, actually-possible codes these six endpoints can send -- is
        ever echoed: nothing outside that enumeration reaches the log by
        name, regardless of shape or charset. ``message`` free text is never
        logged at all. Any other body (the actual 2026-07-28 canary
        recurrence was app-origin JSON with a known ``detail``, so this is
        the exceptional path) logs status, an allowlisted media type
        (:func:`_safe_media_type` -- a ``Content-Type`` parameter, or an
        out-of-allowlist subtype, could otherwise smuggle a secret), byte
        length, and an IRREVERSIBLE fingerprint instead: still enough to
        correlate repeated occurrences of the same opaque body across log
        lines, never a byte of its content. No request header (the bearer
        token) is ever included either way.
        """
        detail = _extract_safe_detail_code(response)
        if detail is not None:
            _logger.warning(
                "coordinator request returned HTTP status error (app envelope): "
                "path=%s status=%d detail=%s",
                path,
                response.status_code,
                detail,
            )
            return
        body_bytes = response.content
        fingerprint = hashlib.sha256(body_bytes).hexdigest()[:_FINGERPRINT_HEX_CHARS]
        media_type = _safe_media_type(response.headers.get("content-type", ""))
        _logger.warning(
            "coordinator request returned HTTP status error (opaque body): "
            "path=%s status=%d content_type=%s content_length=%d fingerprint=%s",
            path,
            response.status_code,
            media_type,
            len(body_bytes),
            fingerprint,
        )

    async def eligibility(self) -> Eligibility:
        data = await self._post("eligibility")
        action = data.get("action")
        if action not in {"none", "check", "install"}:
            raise CoordinatorError("coordinator_invalid_response")
        generation = data.get("action_generation")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise CoordinatorError("coordinator_invalid_response")
        return Eligibility(
            action=cast(Action, action),
            action_generation=generation,
            blocker=_optional_string(data.get("blocker")),
        )

    async def claim(
        self,
        *,
        recovery: bool = False,
        expected_generation: int | None = None,
    ) -> LeaseStatus:
        body: dict[str, object] | None = None
        if recovery:
            if expected_generation is None:
                raise CoordinatorError("coordinator_invalid_recovery_generation")
            body = {"recovery": True, "expected_generation": expected_generation}
        elif expected_generation is not None:
            body = {"expected_generation": expected_generation}
        data = await self._post("claim", body)
        ready, seconds, blocker = self._lease_values(data)
        token = data.get("lease_token")
        if token is not None and (not isinstance(token, str) or not 32 <= len(token) <= 256):
            raise CoordinatorError("coordinator_invalid_response")
        if token is None and ready:
            raise CoordinatorError("coordinator_invalid_response")
        generation = data.get("action_generation")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise CoordinatorError("coordinator_invalid_response")
        return LeaseStatus(
            lease_token=token,
            ready=ready,
            lease_seconds=seconds,
            blocker=blocker,
            action_generation=generation,
        )

    async def renew(
        self,
        lease_token: str,
        *,
        phase: Literal["installing", "rollback"] | None = None,
    ) -> LeaseStatus:
        body: dict[str, object] = {"lease_token": lease_token}
        if phase is not None:
            body["phase"] = phase
        data = await self._post("renew", body)
        ready, seconds, blocker = self._lease_values(data)
        return LeaseStatus(
            lease_token=lease_token,
            ready=ready,
            lease_seconds=seconds,
            blocker=blocker,
        )

    async def heartbeat(self, *, action_generation: int) -> None:
        await self._post(
            "heartbeat",
            {"phase": "checking", "action_generation": action_generation},
        )

    def _lease_values(self, data: dict[str, object]) -> tuple[bool, int, str | None]:
        ready = data.get("ready")
        seconds = data.get("lease_seconds")
        if (
            not isinstance(ready, bool)
            or isinstance(seconds, bool)
            or not isinstance(seconds, int)
            or not 1 <= seconds <= 3600
        ):
            raise CoordinatorError("coordinator_invalid_response")
        return ready, seconds, _optional_string(data.get("blocker"))

    async def release(self, lease_token: str) -> None:
        await self._post("release", {"lease_token": lease_token})

    async def outcome(
        self,
        *,
        operation: Literal["check", "install"],
        outcome: Outcome,
        action_generation: int,
        lease_token: str | None = None,
        current_digest: str | None = None,
        available_digest: str | None = None,
        current_build: str | None = None,
        available_build: str | None = None,
        from_build: str | None = None,
        to_build: str | None = None,
        detail_code: str | None = None,
    ) -> None:
        body: dict[str, object] = {
            "operation": operation,
            "outcome": outcome,
            "action_generation": action_generation,
        }
        optional = {
            "lease_token": lease_token,
            "current_digest": current_digest,
            "available_digest": available_digest,
            "current_build": current_build,
            "available_build": available_build,
            "from_build": from_build,
            "to_build": to_build,
            "detail_code": detail_code,
        }
        body.update({key: value for key, value in optional.items() if value is not None})
        await self._post("outcome", body)
