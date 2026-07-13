"""Plex account verification against plex.tv's JSON v2 API.

The browser runs the plex.tv PIN flow itself and hands the resulting token to
the backend; this adapter re-derives identity and server ownership server-side
before any user or session is written. It talks to plex.tv's ``api/v2`` JSON
endpoints (``/user`` returns a flat object, ``/resources`` returns an array) and
to one Plex server's ``/identity`` probe.

Every failure raises :class:`PlexVerifyError` with a stable ``code`` (a
user-facing error identifier) and non-secret ``diagnostics`` only — Plex tokens
never appear in a message, a diagnostics value, or a log line.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, cast

import httpx

from plex_manager.adapters.service_url import InvalidServiceUrl, ServiceUrl
from plex_manager.headersafe import is_header_safe

__all__ = [
    "CODE_TOKEN_INVALID",
    "PlexAccount",
    "PlexConnection",
    "PlexResource",
    "PlexTvClient",
    "PlexVerifyError",
    "account_server_resource",
    "find_owned_server",
    "owned_servers",
]

_PLEX_TV_BASE_URL: Final = "https://plex.tv"
_PLEX_TV_HOST: Final = "plex.tv"
_HTTP_OK: Final = 200
_HTTP_MULTIPLE_CHOICES: Final = 300
_HTTP_UNAUTHORIZED: Final = 401
_HTTP_FORBIDDEN: Final = 403

# Stable, user-facing error identifiers (see the web error taxonomy).
_CODE_PLEX_TV_UNREACHABLE: Final = "plex_tv_unreachable_server"
_CODE_PLEX_TV_BAD_RESPONSE: Final = "plex_tv_bad_response"

# Private wrapper key ``_request_json`` uses to carry a top-level JSON ARRAY body.
# Deliberately not a plausible public wire key: ``parse_resources`` keys off it to
# distinguish "the body was a real array" from an object body that merely contains
# an array under a public name like "items" (#296 — a malformed object must never
# read as an empty resource list, which callers treat as an authorization signal).
_ARRAY_BODY_KEY: Final = "__plex_manager_array_body__"
# Public: consumers (e.g. watchlist revalidation) key STALE-vs-UNKNOWN off this
# exact code, so it must be shared -- not hand-copied -- to stay coupled at
# import time.
CODE_TOKEN_INVALID: Final = "plex_token_invalid"  # noqa: S105 - error code, not a secret
_CODE_IDENTITY_FAILED: Final = "server_identity_failed"
_CODE_SERVER_UNREACHABLE: Final = "server_unreachable_from_backend"


class PlexVerifyError(RuntimeError):
    """plex.tv or the Plex server gave an unusable answer during verification.

    ``code`` is a stable, user-facing error identifier (see the web error
    taxonomy); ``diagnostics`` carries only non-secret context (host, status).
    """

    def __init__(
        self, code: str, message: str, *, diagnostics: dict[str, str] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.diagnostics = diagnostics or {}


@dataclass(frozen=True)
class PlexAccount:
    """Minimal signed-in Plex account identity persisted to ``users``."""

    plex_id: int
    username: str
    email: str | None
    avatar_url: str | None


@dataclass(frozen=True)
class PlexConnection:
    """One reachable address advertised for a Plex resource."""

    uri: str
    address: str | None
    port: int | None
    local: bool
    relay: bool
    protocol: str | None


@dataclass(frozen=True)
class PlexResource:
    """One device from ``/api/v2/resources`` relevant to owner/access checks."""

    name: str | None
    client_identifier: str | None
    owned: bool
    provides: tuple[str, ...]
    connections: tuple[PlexConnection, ...]


def _require_header_safe_token(token: str, host: str) -> None:
    """Reject a token that cannot be sent as an ``X-Plex-Token`` header value
    BEFORE any request.

    httpx raises on such a value: CR/LF/NUL echo the RAW token in ``str(exc)``
    (a credential leak if that ever reached a message/log via the chained
    cause); non-ASCII makes httpx's ASCII header encoder raise an uncaught
    ``UnicodeEncodeError`` (a 500). Fail fast with a credential-free
    :class:`PlexVerifyError` instead of ever reaching either failure mode.
    """
    if not is_header_safe(token):
        raise PlexVerifyError(
            CODE_TOKEN_INVALID,
            "Plex token is not a valid credential value",
            diagnostics={"host": host},
        )


def _as_mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return cast("Mapping[str, object]", value)
    return {}


def _as_sequence(value: object) -> Sequence[object]:
    if isinstance(value, (list, tuple)):
        return cast("Sequence[object]", value)
    return ()


def _get_str(fields: Mapping[str, object], key: str) -> str | None:
    value = fields.get(key)
    return value if isinstance(value, str) and value else None


def _get_int(fields: Mapping[str, object], key: str) -> int | None:
    value = fields.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return None


def _get_bool(fields: Mapping[str, object], key: str) -> bool:
    """Parse a Plex boolean, tolerating every shape plex.tv encodes them in.

    v2 JSON uses real booleans, but resource payloads have historically been
    XML-derived, so a boolean field (e.g. ``owned``) can arrive as a real
    ``bool``, an ``int`` (``1`` / ``0``), or a string (``"1"`` / ``"0"`` /
    ``"true"`` / ``"false"``, case-insensitive). Recognizing only ``True`` /
    ``"true"`` would read the REAL server owner's ``owned=1`` as ``False`` and
    lock them out of every ``require_admin`` route.

    Fails CLOSED for ownership: any value outside the known truthy encodings maps
    to ``False``, so an unexpected shape (an int other than ``1``, an unknown
    string, ``None``) never mis-grants ownership.
    """
    value = fields.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 1  # 1 -> True; 0 and any other int -> False (fail closed)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true"}
    return False


def _media_container(payload: Mapping[str, object]) -> Mapping[str, object]:
    return _as_mapping(payload.get("MediaContainer"))


def _parse_provides(value: object) -> tuple[str, ...]:
    """Split plex.tv's comma-joined ``provides`` string, dropping empty entries."""
    if not isinstance(value, str):
        return ()
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _parse_connections(value: object) -> tuple[PlexConnection, ...]:
    connections: list[PlexConnection] = []
    for item in _as_sequence(value):
        fields = _as_mapping(item)
        uri = _get_str(fields, "uri")
        if uri is None:
            continue  # a connection without a uri is unusable
        connections.append(
            PlexConnection(
                uri=uri,
                address=_get_str(fields, "address"),
                port=_get_int(fields, "port"),
                local=_get_bool(fields, "local"),
                relay=_get_bool(fields, "relay"),
                protocol=_get_str(fields, "protocol"),
            )
        )
    return tuple(connections)


def owned_servers(resources: Sequence[PlexResource]) -> list[PlexResource]:
    """The account's OWN Plex Media Servers (owned and provides 'server')."""
    return [r for r in resources if r.owned and "server" in r.provides and r.client_identifier]


def find_owned_server(
    resources: Sequence[PlexResource], machine_identifier: str
) -> PlexResource | None:
    return next(
        (r for r in owned_servers(resources) if r.client_identifier == machine_identifier), None
    )


def account_server_resource(
    resources: Sequence[PlexResource], machine_identifier: str
) -> PlexResource | None:
    """Any resource (owned or shared) matching the configured server."""
    return next(
        (
            r
            for r in resources
            if "server" in r.provides and r.client_identifier == machine_identifier
        ),
        None,
    )


class PlexTvClient:
    """Small async client for plex.tv v2 verification and server identity."""

    def __init__(self, client: httpx.AsyncClient, *, client_identifier: str) -> None:
        self._client = client
        self._client_identifier = client_identifier

    def __repr__(self) -> str:  # pragma: no cover - trivial redaction guard
        return f"PlexTvClient(client_identifier={self._client_identifier!r})"

    async def fetch_account(self, auth_token: str) -> PlexAccount:
        _require_header_safe_token(auth_token, _PLEX_TV_HOST)
        payload = await self._request_json(
            "GET",
            f"{_PLEX_TV_BASE_URL}/api/v2/user",
            headers=self._token_headers(auth_token),
            unreachable_code=_CODE_PLEX_TV_UNREACHABLE,
            bad_response_code=_CODE_PLEX_TV_BAD_RESPONSE,
            invalid_token_code=CODE_TOKEN_INVALID,
        )
        return self.parse_account(payload)

    async def fetch_resources(self, auth_token: str) -> list[PlexResource]:
        _require_header_safe_token(auth_token, _PLEX_TV_HOST)
        payload = await self._request_json(
            "GET",
            f"{_PLEX_TV_BASE_URL}/api/v2/resources",
            params={"includeHttps": "1"},
            headers=self._token_headers(auth_token),
            unreachable_code=_CODE_PLEX_TV_UNREACHABLE,
            bad_response_code=_CODE_PLEX_TV_BAD_RESPONSE,
            invalid_token_code=CODE_TOKEN_INVALID,
        )
        return self.parse_resources(payload)

    async def fetch_server_identity(self, base_url: str, service_token: str) -> str:
        try:
            service_url = ServiceUrl.parse(base_url)
            url = service_url.endpoint("/identity")
        except InvalidServiceUrl as exc:
            raise PlexVerifyError(
                _CODE_SERVER_UNREACHABLE,
                "Plex server URL is invalid",
            ) from exc
        _require_header_safe_token(service_token, service_url.host)
        payload = await self._request_json(
            "GET",
            url,
            headers=self._token_headers(service_token),
            unreachable_code=_CODE_SERVER_UNREACHABLE,
            bad_response_code=_CODE_IDENTITY_FAILED,
        )
        identity = _get_str(_media_container(payload), "machineIdentifier")
        if identity is None:
            raise PlexVerifyError(
                _CODE_IDENTITY_FAILED,
                "Plex server identity response did not include machineIdentifier",
                diagnostics={"host": service_url.host},
            )
        return identity

    async def _request_json(
        self,
        method: str,
        url: str | httpx.URL,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str],
        unreachable_code: str,
        bad_response_code: str,
        invalid_token_code: str | None = None,
    ) -> Mapping[str, object]:
        host = httpx.URL(url).host
        try:
            response = await self._client.request(
                method,
                url,
                params=params,
                headers=headers,
                follow_redirects=False,
            )
        except httpx.RequestError as exc:
            raise PlexVerifyError(
                unreachable_code,
                f"Plex request failed: {method} {_safe_path(url)}",
                diagnostics={"host": host},
            ) from exc
        status = response.status_code
        if invalid_token_code is not None and status in (_HTTP_UNAUTHORIZED, _HTTP_FORBIDDEN):
            raise PlexVerifyError(
                invalid_token_code,
                f"Plex request rejected: {method} {_safe_path(url)} ({status})",
                diagnostics={"host": host, "status": str(status)},
            )
        # Checks the full 2xx range explicitly rather than ``httpx.Response.is_error``
        # (issue #87): ``is_error`` is only true for >=400, so a 3xx (the shared
        # client never follows redirects) would read as a successful verification
        # even though it never reached plex.tv/the server. #122 fixed the other
        # four adapter wrappers but deferred this fifth site because oauth.py lived
        # only on the then-unmerged PR #45 branch.
        if not (_HTTP_OK <= status < _HTTP_MULTIPLE_CHOICES):
            raise PlexVerifyError(
                bad_response_code,
                f"Plex request failed: {method} {_safe_path(url)} ({status})",
                diagnostics={"host": host, "status": str(status)},
            )
        try:
            payload = cast("object", response.json())
        except (json.JSONDecodeError, ValueError) as exc:
            raise PlexVerifyError(
                bad_response_code,
                f"Plex returned a non-JSON response: {method} {_safe_path(url)}",
                diagnostics={"host": host},
            ) from exc
        if isinstance(payload, list):
            return {_ARRAY_BODY_KEY: cast("list[object]", payload)}
        return _as_mapping(payload)

    def _client_headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "X-Plex-Product": "Plex Manager",
            "X-Plex-Client-Identifier": self._client_identifier,
        }

    def _token_headers(self, token: str) -> dict[str, str]:
        headers = self._client_headers()
        headers["X-Plex-Token"] = token
        return headers

    @classmethod
    def parse_account(cls, payload: Mapping[str, object]) -> PlexAccount:
        # v2 /user is a FLAT object: id/username/title/email/thumb at top level.
        plex_id = _get_int(payload, "id")
        username = _get_str(payload, "username") or _get_str(payload, "title")
        if plex_id is None or username is None:
            raise PlexVerifyError(
                _CODE_PLEX_TV_BAD_RESPONSE,
                "plex.tv answered in an unexpected way: account response missing id/username",
                diagnostics={"host": _PLEX_TV_HOST},
            )
        return PlexAccount(
            plex_id=plex_id,
            username=username,
            email=_get_str(payload, "email"),
            avatar_url=_get_str(payload, "thumb"),
        )

    @classmethod
    def parse_resources(cls, payload: Mapping[str, object]) -> list[PlexResource]:
        # v2 /resources is a JSON array; ``_request_json`` wraps it under the PRIVATE
        # ``_ARRAY_BODY_KEY`` sentinel. A 2xx body that is NOT that array shape (an
        # error object, an HTML page that still parsed as JSON, a truncated payload)
        # is a MALFORMED response, not "an account with zero resources". The
        # distinction is load-bearing: callers treat a genuinely-empty resource list
        # as an authorization signal (revalidation maps it to STALE and DELETES the
        # user's eviction-protection snapshot), so a malformed shape silently
        # collapsing to ``[]`` would destroy state on a transient plex.tv hiccup
        # (#296). The sentinel is deliberately not a public wire key: only
        # ``_request_json`` can synthesize it (exclusively for array bodies), so an
        # object body that happens to carry a public "items" list can never
        # impersonate the array wrapper -- fail fatal instead of guessing.
        raw_items = payload.get(_ARRAY_BODY_KEY)
        if not isinstance(raw_items, list):
            raise PlexVerifyError(
                _CODE_PLEX_TV_BAD_RESPONSE,
                "plex.tv answered in an unexpected way: resources response was not a JSON array",
                diagnostics={"host": _PLEX_TV_HOST},
            )
        resources: list[PlexResource] = []
        for item in cast("list[object]", raw_items):
            fields = _as_mapping(item)
            resources.append(
                PlexResource(
                    name=_get_str(fields, "name"),
                    client_identifier=_get_str(fields, "clientIdentifier"),
                    owned=_get_bool(fields, "owned"),
                    provides=_parse_provides(fields.get("provides")),
                    connections=_parse_connections(fields.get("connections")),
                )
            )
        return resources


def _safe_path(url: str | httpx.URL) -> str:
    return httpx.URL(url).path
