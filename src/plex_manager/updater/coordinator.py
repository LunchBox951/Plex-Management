"""Authenticated client for the app-owned update coordinator."""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from typing import Final, Literal, cast

import httpx

Action = Literal["none", "check", "install"]
Outcome = Literal["no_update", "update_available", "succeeded", "failed", "rolled_back"]

_logger = logging.getLogger(__name__)

#: The app's own machine-code charset (issue #539 review round 4): every
#: ``AppError``/``PlexVerifyError``/``CoordinatorError`` code raised anywhere
#: in this codebase (``web/errors.py``, ``web/routers/updates.py``,
#: ``web/updater_auth.py``, ``adapters/plex/oauth.py``, this module's own
#: ``coordinator_*`` codes) is lowercase ASCII letters/digits/underscores;
#: the longest observed (``update_recovery_generation_mismatch``) is 35
#: chars, so 64 is a generous, still-tight bound. This is an ALLOWLIST, not a
#: sanitizer, mirroring :func:`~plex_manager.logsafe.safe_guid`'s own
#: plain-id passthrough: a string that fullmatches this charset cannot
#: contain ``/ : ? & % @`` or whitespace, so it structurally cannot carry a
#: URL, a query string, or a CR/LF -- there is no separate
#: safe_text/redact_secrets step needed after a fullmatch, the same way
#: ``safe_guid``'s own allowlisted ids pass through unprocessed. Round 3's
#: ``message`` field is deliberately dropped rather than similarly
#: constrained: free-text prose has no charset that stays both bounded and
#: useful, and shape (parses-as-envelope) proved NOT to authenticate origin
#: (round 4's finding -- a look-alike proxy body with string ``detail``/
#: ``message`` fields passed the round-3 predicate). The detail CODE alone
#: (e.g. ``coordinator_lease_store_unreachable``) remains the diagnostic
#: payload.
_DETAIL_CODE_RE: Final = re.compile(r"[a-z0-9_]{1,64}")

#: RFC 6838 media-type token charset (``type/subtype``, no parameters) for
#: the ``Content-Type`` logged on a non-app-shape body (issue #539 review
#: round 4): a parameter such as ``application/json; source="https://host/
#: ...secret..."`` could smuggle a secret past a naive whole-header log, so
#: only the portion before the first ``;`` is even considered, and it is
#: logged ONLY when it fullmatches this allowlist -- otherwise
#: ``content_type=invalid``. Each side is capped at 127 chars (every real
#: IANA-registered media type is far shorter; RFC 4288 itself caps a
#: type/subtype token at 127) so a pathological-but-charset-valid header
#: cannot flood the log either.
_MEDIA_TYPE_RE: Final = re.compile(r"[a-zA-Z0-9!#$&^_.+-]{1,127}/[a-zA-Z0-9!#$&^_.+-]{1,127}")

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
    it is a string that fullmatches :data:`_DETAIL_CODE_RE` -- the app's own
    machine-code charset (issue #539 review round 4). ``None`` for anything
    else: not JSON, not a dict, a missing/non-string ``detail``, or a
    ``detail`` that IS a string but does not fullmatch the charset.

    Round 3 tried to recognize the app's ``AppError`` JSON envelope
    (``web/errors.py``'s ``{"detail": <code>, "message": <text>, ...}``) by
    SHAPE -- a dict with string ``detail``/``message`` fields -- and echoed
    both. That shape does not authenticate origin: a look-alike proxy/relay
    body with its own string ``detail``/``message`` fields (a secret-bearing
    URL in ``message``, say) passes the identical predicate, and neither
    ``safe_text`` (line-boundary only) nor ``redact_secrets`` (key-name/
    shape-based) is guaranteed to catch an unlabeled secret riding free-text
    prose. Round 4 instead authenticates by CONTENT: ``detail`` is logged
    ONLY when it fullmatches the tight ``[a-z0-9_]{1,64}`` allowlist every
    real machine code in this codebase uses -- a string in that charset
    structurally cannot contain ``/ : ? & % @`` or whitespace, so it cannot
    carry a URL or a CR/LF regardless of where the response actually came
    from. ``message`` is no longer read or logged at all: free-text prose has
    no charset that stays both bounded and useful the way a code does.
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
    if not isinstance(detail, str) or _DETAIL_CODE_RE.fullmatch(detail) is None:
        return None
    return detail


def _safe_media_type(content_type_header: str) -> str:
    """Reduce a ``Content-Type`` header to a loggable media type (issue #539
    review round 4): only the portion before the first ``;`` is even
    considered (a parameter such as ``application/json; source="https://
    host/...secret..."`` could otherwise smuggle a secret past the log), and
    that portion is returned ONLY when it fullmatches :data:`_MEDIA_TYPE_RE`
    -- the RFC 6838 ``type/subtype`` token charset. Anything else (missing,
    empty, malformed, or carrying stray characters the charset excludes)
    logs as the fixed string ``"invalid"`` rather than any part of the raw
    header value.
    """
    media_type = content_type_header.split(";", 1)[0].strip()
    if _MEDIA_TYPE_RE.fullmatch(media_type) is None:
        return "invalid"
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
            # echo arbitrary response text (review rounds 3-4 -- see
            # _log_non_2xx/_extract_safe_detail_code for why).
            self._log_non_2xx(path, response)
            raise CoordinatorError("coordinator_unavailable") from exc
        return _object(response)

    def _log_non_2xx(self, path: str, response: httpx.Response) -> None:
        """Log a non-2xx coordinator response without ever echoing arbitrary
        response text (issue #539 review rounds 3-4).

        Body shape does not authenticate origin: a look-alike intermediary/
        proxy body can carry its own string ``detail``/``message`` fields, so
        parsing-as-the-envelope is not enough (round 4's finding on round
        3's approach). Only :func:`_extract_safe_detail_code`'s CONTENT
        check -- a ``detail`` field that fullmatches the app's tight
        machine-code charset -- is ever echoed; that charset structurally
        excludes ``/ : ? & % @`` and whitespace, so a matching string cannot
        carry a URL or CR/LF no matter where the response actually
        originated. ``message`` free text is never logged at all any more.
        Any other body (the actual 2026-07-28 canary recurrence was
        app-origin JSON with a conforming ``detail``, so this is the
        exceptional path) logs status, a validated media type
        (:func:`_safe_media_type` -- a ``Content-Type`` parameter could
        otherwise smuggle a secret), byte length, and an IRREVERSIBLE
        fingerprint instead: still enough to correlate repeated occurrences
        of the same opaque body across log lines, never a byte of its
        content. No request header (the bearer token) is ever included
        either way.
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
