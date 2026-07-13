"""Authenticated client for the app-owned update coordinator."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast

import httpx

Action = Literal["none", "check", "install"]
Outcome = Literal["no_update", "update_available", "succeeded", "failed", "rolled_back"]


class CoordinatorError(RuntimeError):
    """The coordinator was unavailable or returned an invalid contract."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Eligibility:
    action: Action
    automatic_enabled: bool
    window_open: bool
    idle_only: bool
    blocker: str | None


@dataclass(frozen=True)
class LeaseStatus:
    lease_token: str | None
    ready: bool
    lease_seconds: int
    blocker: str | None


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
            response.raise_for_status()
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            raise CoordinatorError("coordinator_unavailable") from exc
        return _object(response)

    async def eligibility(self) -> Eligibility:
        data = await self._post("eligibility")
        action = data.get("action")
        if action not in {"none", "check", "install"}:
            raise CoordinatorError("coordinator_invalid_response")
        values = tuple(data.get(key) for key in ("automatic_enabled", "window_open", "idle_only"))
        if not all(isinstance(value, bool) for value in values):
            raise CoordinatorError("coordinator_invalid_response")
        return Eligibility(
            action=cast(Action, action),
            automatic_enabled=cast(bool, values[0]),
            window_open=cast(bool, values[1]),
            idle_only=cast(bool, values[2]),
            blocker=_optional_string(data.get("blocker")),
        )

    async def claim(self) -> LeaseStatus:
        data = await self._post("claim")
        ready, seconds, blocker = self._lease_values(data)
        token = data.get("lease_token")
        if token is not None and (not isinstance(token, str) or not 32 <= len(token) <= 256):
            raise CoordinatorError("coordinator_invalid_response")
        if token is None and ready:
            raise CoordinatorError("coordinator_invalid_response")
        return LeaseStatus(
            lease_token=token,
            ready=ready,
            lease_seconds=seconds,
            blocker=blocker,
        )

    async def renew(self, lease_token: str) -> LeaseStatus:
        data = await self._post("renew", {"lease_token": lease_token})
        ready, seconds, blocker = self._lease_values(data)
        return LeaseStatus(
            lease_token=lease_token,
            ready=ready,
            lease_seconds=seconds,
            blocker=blocker,
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
        lease_token: str | None = None,
        current_digest: str | None = None,
        available_digest: str | None = None,
        current_build: str | None = None,
        available_build: str | None = None,
        from_build: str | None = None,
        to_build: str | None = None,
        detail_code: str | None = None,
    ) -> None:
        body: dict[str, object] = {"operation": operation, "outcome": outcome}
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
