"""Plex hosted sign-in helpers.

This adapter owns the plex.tv PIN flow used for human login. It is separate from
``PlexLibrary`` because the library adapter talks to one configured Plex server,
while this module talks to plex.tv and then verifies that the signed-in account
owns the configured server.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, cast
from urllib.parse import urlencode

import httpx

__all__ = [
    "PlexAccount",
    "PlexOAuthClient",
    "PlexOAuthError",
    "PlexOAuthPending",
    "PlexPin",
    "PlexResource",
    "owner_has_server",
]

_PLEX_TV_BASE_URL: Final = "https://plex.tv"
_PLEX_AUTH_BASE_URL: Final = "https://app.plex.tv/auth"
_HTTP_UNAUTHORIZED: Final = 401
_HTTP_FORBIDDEN: Final = 403


class PlexOAuthError(RuntimeError):
    """Raised when plex.tv or the configured server returns an unusable response."""


class PlexOAuthPending(PlexOAuthError):
    """Raised while a PIN exists but the user has not approved it yet."""


@dataclass(frozen=True)
class PlexPin:
    """One plex.tv PIN login challenge."""

    pin_id: int
    code: str
    auth_url: str
    expires_at: datetime
    expires_in: int | None = None


@dataclass(frozen=True)
class PlexAccount:
    """Minimal signed-in Plex account identity persisted to ``users``."""

    plex_id: int
    username: str
    email: str | None
    avatar_url: str | None


@dataclass(frozen=True)
class PlexResource:
    """One resource from ``/api/resources`` relevant to owner checks."""

    name: str | None
    client_identifier: str | None
    owned: bool


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
    value = fields.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() == "true"
    return False


def _media_container(payload: Mapping[str, object]) -> Mapping[str, object]:
    return _as_mapping(payload.get("MediaContainer"))


def owner_has_server(resources: Sequence[PlexResource], machine_identifier: str) -> bool:
    """Whether the signed-in account owns the configured Plex server."""

    return any(
        resource.owned and resource.client_identifier == machine_identifier
        for resource in resources
    )


class PlexOAuthClient:
    """Small async client for Plex hosted login and owner verification."""

    def __init__(self, client: httpx.AsyncClient, *, client_identifier: str) -> None:
        self._client = client
        self._client_identifier = client_identifier

    def __repr__(self) -> str:  # pragma: no cover - trivial redaction guard
        return f"PlexOAuthClient(client_identifier={self._client_identifier!r})"

    async def create_pin(self, *, return_url: str) -> PlexPin:
        payload = await self._request_json(
            "POST",
            f"{_PLEX_TV_BASE_URL}/api/v2/pins",
            params={"strong": "true"},
            headers=self._client_headers(),
        )
        return self.parse_pin(
            payload,
            client_identifier=self._client_identifier,
            return_url=return_url,
        )

    async def poll_pin(self, pin_id: int) -> str:
        payload = await self._request_json(
            "GET",
            f"{_PLEX_TV_BASE_URL}/api/v2/pins/{pin_id}",
            headers=self._client_headers(),
        )
        token = _get_str(payload, "authToken")
        if token is None:
            raise PlexOAuthPending("plex pin authorization is still pending")
        return token

    async def fetch_account(self, auth_token: str) -> PlexAccount:
        payload = await self._request_json(
            "GET",
            f"{_PLEX_TV_BASE_URL}/users/account.json",
            headers=self._token_headers(auth_token),
        )
        return self.parse_account(payload)

    async def fetch_resources(self, auth_token: str) -> list[PlexResource]:
        payload = await self._request_json(
            "GET",
            f"{_PLEX_TV_BASE_URL}/api/resources",
            params={"includeHttps": "1"},
            headers=self._token_headers(auth_token),
        )
        return self.parse_resources(payload)

    async def fetch_server_identity(self, base_url: str, service_token: str) -> str:
        payload = await self._request_json(
            "GET",
            f"{base_url.rstrip('/')}/identity",
            headers={"X-Plex-Token": service_token, "Accept": "application/json"},
        )
        identity = _get_str(_media_container(payload), "machineIdentifier")
        if identity is None:
            raise PlexOAuthError("Plex server identity response did not include machineIdentifier")
        return identity

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str],
    ) -> Mapping[str, object]:
        try:
            response = await self._client.request(method, url, params=params, headers=headers)
        except httpx.RequestError as exc:
            raise PlexOAuthError(f"Plex OAuth request failed: {method} {_safe_path(url)}") from exc
        status = response.status_code
        if status in (_HTTP_UNAUTHORIZED, _HTTP_FORBIDDEN):
            raise PlexOAuthError(
                f"Plex OAuth request rejected: {method} {_safe_path(url)} ({status})"
            )
        if response.is_error:
            raise PlexOAuthError(
                f"Plex OAuth request failed: {method} {_safe_path(url)} ({status})"
            )
        try:
            payload = cast("object", response.json())
        except (json.JSONDecodeError, ValueError) as exc:
            raise PlexOAuthError(
                f"Plex OAuth returned a non-JSON response: {method} {_safe_path(url)}"
            ) from exc
        if isinstance(payload, list):
            return {"items": cast("list[object]", payload)}
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
    def parse_pin(
        cls,
        payload: Mapping[str, object],
        *,
        client_identifier: str = "",
        now: datetime | None = None,
        return_url: str | None = None,
    ) -> PlexPin:
        pin_id = _get_int(payload, "id")
        code = _get_str(payload, "code")
        if pin_id is None or code is None:
            raise PlexOAuthError("Plex PIN response did not include id and code")
        expires_in = _get_int(payload, "expiresIn")
        issued_at = now or datetime.now(UTC)
        expires_at = issued_at + timedelta(seconds=expires_in or 600)
        return PlexPin(
            pin_id=pin_id,
            code=code,
            auth_url=_build_auth_url(
                client_identifier=client_identifier,
                code=code,
                return_url=return_url,
            ),
            expires_at=expires_at,
            expires_in=expires_in,
        )

    @classmethod
    def parse_account(cls, payload: Mapping[str, object]) -> PlexAccount:
        account = _as_mapping(payload.get("user")) or payload
        plex_id = _get_int(account, "id")
        username = _get_str(account, "username") or _get_str(account, "title")
        if plex_id is None or username is None:
            raise PlexOAuthError("Plex account response did not include id and username")
        return PlexAccount(
            plex_id=plex_id,
            username=username,
            email=_get_str(account, "email"),
            avatar_url=_get_str(account, "thumb"),
        )

    @classmethod
    def parse_resources(cls, payload: object) -> list[PlexResource]:
        if isinstance(payload, Mapping):
            typed_payload = cast("Mapping[str, object]", payload)
            container = _media_container(typed_payload)
            raw_items = _as_sequence(container.get("Device"))
            if not raw_items:
                raw_items = _as_sequence(typed_payload.get("items"))
        else:
            raw_items = _as_sequence(payload)
        resources: list[PlexResource] = []
        for item in raw_items:
            fields = _as_mapping(item)
            resources.append(
                PlexResource(
                    name=_get_str(fields, "name"),
                    client_identifier=_get_str(fields, "clientIdentifier"),
                    owned=_get_bool(fields, "owned"),
                )
            )
        return resources


def _safe_path(url: str) -> str:
    parsed = httpx.URL(url)
    return parsed.path


def _build_auth_url(
    *,
    client_identifier: str,
    code: str,
    return_url: str | None,
) -> str:
    params = {
        "clientID": client_identifier,
        "code": code,
        "context[device][product]": "Plex Manager",
    }
    if return_url:
        params["forwardUrl"] = return_url
    return f"{_PLEX_AUTH_BASE_URL}#?{urlencode(params)}"
