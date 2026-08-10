"""Authenticated client for the app-owned update coordinator."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Final, Literal, cast

import httpx

from plex_manager.logsafe import redact_secrets, safe_text

Action = Literal["none", "check", "install"]
Outcome = Literal["no_update", "update_available", "succeeded", "failed", "rolled_back"]

_logger = logging.getLogger(__name__)

#: Cap on the ``detail``/``message`` field chars logged on a non-2xx
#: coordinator response whose body is the app's own AppError envelope (issue
#: #539, review round 3) -- bounds a pathological field without needing one
#: (both are normally short operator-facing prose/codes; see
#: :func:`_parse_app_error_envelope`).
_MAX_LOGGED_FIELD_CHARS: Final = 500

#: Cap on the logged ``Content-Type`` header for a non-app-shape body. Also
#: server-controlled input; bounded and safe_text'd for the same reason as
#: every other value on this path even though a compliant header is short.
_MAX_LOGGED_CONTENT_TYPE_CHARS: Final = 128

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
    except ValueError as exc:
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


def _parse_app_error_envelope(response: httpx.Response) -> tuple[str, str] | None:
    """Recognize the app's own ``AppError`` JSON envelope on a non-2xx body
    (``web/errors.py``'s ``{"detail": <code>, "message": <text>, "hint"?:
    ..., "diagnostics"?: ...}``, see ``_envelope``/``install_error_handlers``)
    -- the ONLY body shape this client ever echoes any text from.

    Returns ``(detail, message)`` when the parsed JSON is a dict carrying
    both as strings; ``None`` for anything else (not JSON, not a dict,
    missing/non-string fields) -- including a body that is valid JSON but
    merely LOOKS similar, which is treated exactly as opaque as one that
    isn't JSON at all (see :meth:`CoordinatorClient._log_non_2xx`).

    Why only these two fields, and why this is the only recognized shape:
    ``AppError.message``/``hint`` are operator-facing prose the APP ITSELF
    constructs and ``diagnostics`` is explicitly documented as NON-secret
    context (``AppError``'s own docstring) -- the envelope is safe to echo
    BY THE APP'S OWN CONTRACT, unlike arbitrary response text. An
    intermediary/proxy debug page, raw HTML, or any other non-envelope body
    could carry an unlabeled token or a secret-bearing URL
    (``https://host/download/<token>``) that neither ``safe_text`` (line-
    boundary only) nor ``redact_secrets`` (key-name/shape-based) is
    guaranteed to catch -- truncating that text would still leave it
    logged, just shorter. ``hint``/``diagnostics`` are recognized as part of
    the shape check (a genuine envelope may carry them) but deliberately NOT
    returned/logged: the review scope for issue #539 is ``detail``/
    ``message`` only.
    """
    try:
        parsed: object = response.json()
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    parsed_dict = cast("dict[str, object]", parsed)
    detail = parsed_dict.get("detail")
    message = parsed_dict.get("message")
    if not isinstance(detail, str) or not isinstance(message, str):
        return None
    return detail, message


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
            # echo arbitrary response text (review round 3 -- see
            # _log_non_2xx/_parse_app_error_envelope for why).
            self._log_non_2xx(path, response)
            raise CoordinatorError("coordinator_unavailable") from exc
        return _object(response)

    def _log_non_2xx(self, path: str, response: httpx.Response) -> None:
        """Log a non-2xx coordinator response without ever echoing arbitrary
        response text (issue #539 review round 3).

        Truncation alone does not make an unrecognized body non-secret: an
        intermediary/proxy debug page returned instead of the app's own
        response could carry an unlabeled token or a secret-bearing URL
        (``https://host/download/<token>``) that neither ``safe_text``
        (line-boundary only) nor ``redact_secrets`` (key-name/shape-based) is
        guaranteed to catch within the first N characters. So body TEXT is
        only ever logged when it parses as the app's own AppError envelope
        (:func:`_parse_app_error_envelope`) -- content the app itself
        constructed and guarantees non-secret by contract -- and even then
        only the recognized ``detail``/``message`` fields, bounded and run
        through ``safe_text``/``redact_secrets``. Any other body (the actual
        #539 recurrence case was app-origin JSON, so this is the exceptional
        path) logs status/content-type/length plus an IRREVERSIBLE
        fingerprint instead: still enough to correlate repeated occurrences
        of the same opaque body across log lines, never a byte of its
        content. No request header (the bearer token) is ever included
        either way. The updater sidecar's stdout is read straight off
        ``docker logs`` -- it never passes through the app's
        ``log_capture_service`` capture pipeline, so this call site cannot
        lean on that pipeline's own ``redact_secrets`` pass as a second line
        of defense and composes it directly instead.
        """
        envelope = _parse_app_error_envelope(response)
        if envelope is not None:
            detail, message = envelope
            _logger.warning(
                "coordinator request returned HTTP status error (app envelope): "
                "path=%s status=%d detail=%s message=%s",
                path,
                response.status_code,
                redact_secrets(safe_text(detail[:_MAX_LOGGED_FIELD_CHARS])),
                redact_secrets(safe_text(message[:_MAX_LOGGED_FIELD_CHARS])),
            )
            return
        body_bytes = response.content
        fingerprint = hashlib.sha256(body_bytes).hexdigest()[:_FINGERPRINT_HEX_CHARS]
        content_type = response.headers.get("content-type", "")
        _logger.warning(
            "coordinator request returned HTTP status error (opaque body): "
            "path=%s status=%d content_type=%s content_length=%d fingerprint=%s",
            path,
            response.status_code,
            safe_text(content_type[:_MAX_LOGGED_CONTENT_TYPE_CHARS]),
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
