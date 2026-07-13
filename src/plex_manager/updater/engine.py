"""Minimal async Docker Engine API client over a Unix-domain socket."""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast
from urllib.parse import quote

import httpx

JsonObject = dict[str, Any]


class DockerError(RuntimeError):
    """A bounded Docker failure safe to surface as a detail code."""

    def __init__(self, code: str, *, status_code: int | None = None) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)


class DockerNotFound(DockerError):
    def __init__(self) -> None:
        super().__init__("docker_object_not_found", status_code=404)


def _json_object(response: httpx.Response) -> JsonObject:
    try:
        value: object = response.json()
    except ValueError as exc:
        raise DockerError("docker_invalid_json", status_code=response.status_code) from exc
    if not isinstance(value, dict):
        raise DockerError("docker_invalid_response", status_code=response.status_code)
    return cast(JsonObject, value)


class DockerEngine:
    """Only the Engine calls required to replace one fixed container."""

    def __init__(
        self,
        socket_path: str,
        *,
        client: httpx.AsyncClient | None = None,
        sleep: Any = asyncio.sleep,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=socket_path),
            base_url="http://docker",
            timeout=httpx.Timeout(30.0),
            trust_env=False,
        )
        self._api_prefix: str | None = None
        self._api_version: tuple[int, int] | None = None
        self._sleep = sleep

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _prefix(self) -> str:
        if self._api_prefix is not None:
            return self._api_prefix
        response = await self._request("GET", "/version", versioned=False)
        data = _json_object(response)
        version = data.get("ApiVersion")
        minimum = data.get("MinAPIVersion", "1.24")
        if not isinstance(version, str) or not isinstance(minimum, str):
            raise DockerError("docker_invalid_version")
        try:
            server_parts = tuple(int(part) for part in version.split("."))
            minimum_parts = tuple(int(part) for part in minimum.split("."))
        except ValueError as exc:
            raise DockerError("docker_invalid_version") from exc
        # API 1.41 (Docker 20.10) is the oldest contract supported by this
        # executor. Negotiate down from the version it was implemented against.
        chosen = min(server_parts, (1, 47))
        if chosen < (1, 41) or minimum_parts > (1, 47):
            raise DockerError("docker_api_unsupported")
        self._api_prefix = f"/v{chosen[0]}.{chosen[1]}"
        self._api_version = cast(tuple[int, int], chosen)
        return self._api_prefix

    async def api_version(self) -> tuple[int, int]:
        """Return the negotiated Engine API version for portable payload choices."""
        await self._prefix()
        if self._api_version is None:  # pragma: no cover - _prefix sets both caches
            raise DockerError("docker_invalid_version")
        return self._api_version

    async def _request(
        self,
        method: str,
        path: str,
        *,
        versioned: bool = True,
        expected: tuple[int, ...] = (200, 201, 204, 304),
        **kwargs: Any,
    ) -> httpx.Response:
        if versioned:
            path = f"{await self._prefix()}{path}"
        try:
            response = await self._client.request(method, path, **kwargs)
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            raise DockerError("docker_unavailable") from exc
        if response.status_code == 404:
            raise DockerNotFound
        if response.status_code not in expected:
            raise DockerError("docker_api_error", status_code=response.status_code)
        return response

    async def inspect_container(self, identifier: str) -> JsonObject:
        response = await self._request(
            "GET", f"/containers/{quote(identifier, safe='')}/json", expected=(200,)
        )
        return _json_object(response)

    async def inspect_image(self, identifier: str) -> JsonObject:
        response = await self._request(
            "GET", f"/images/{quote(identifier, safe='')}/json", expected=(200,)
        )
        return _json_object(response)

    async def containers_by_labels(self, labels: dict[str, str]) -> list[JsonObject]:
        filters = {"label": [f"{key}={value}" for key, value in labels.items()]}
        response = await self._request(
            "GET",
            "/containers/json",
            params={"all": "true", "filters": json.dumps(filters, separators=(",", ":"))},
            expected=(200,),
        )
        try:
            value: object = response.json()
        except ValueError as exc:
            raise DockerError("docker_invalid_json") from exc
        if not isinstance(value, list):
            raise DockerError("docker_invalid_response")
        items = cast(list[object], value)
        if not all(isinstance(item, dict) for item in items):
            raise DockerError("docker_invalid_response")
        return [cast(JsonObject, item) for item in items]

    async def pull(self, image_ref: str) -> JsonObject:
        repository, tag = split_tag(image_ref)
        prefix = await self._prefix()
        pull_timeout = httpx.Timeout(30.0, read=None)
        try:
            async with self._client.stream(
                "POST",
                f"{prefix}/images/create",
                params={"fromImage": repository, "tag": tag},
                timeout=pull_timeout,
            ) as response:
                if response.status_code != 200:
                    raise DockerError("docker_api_error", status_code=response.status_code)
                # Pull responses are newline-delimited progress objects. Never log
                # them: registries may include sensitive authentication detail.
                async for line in response.aiter_lines():
                    try:
                        item: object = json.loads(line)
                    except ValueError as exc:
                        raise DockerError("docker_pull_invalid_response") from exc
                    if isinstance(item, dict) and ("error" in item or "errorDetail" in item):
                        raise DockerError("docker_pull_failed")
        except DockerError:
            raise
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            raise DockerError("docker_unavailable") from exc
        return await self.inspect_image(image_ref)

    async def create_container(self, name: str, spec: JsonObject) -> str:
        response = await self._request(
            "POST", "/containers/create", params={"name": name}, json=spec, expected=(201,)
        )
        identifier = _json_object(response).get("Id")
        if not isinstance(identifier, str) or not identifier:
            raise DockerError("docker_create_invalid_response")
        return identifier

    async def stop_container(
        self,
        identifier: str,
        *,
        grace_override: int | None = None,
        request_timeout: float | None = None,
    ) -> None:
        params = {"t": grace_override} if grace_override is not None else None
        request_options: dict[str, Any] = {}
        if request_timeout is not None:
            request_options["timeout"] = httpx.Timeout(request_timeout)
        await self._request(
            "POST",
            f"/containers/{quote(identifier, safe='')}/stop",
            params=params,
            expected=(204, 304),
            **request_options,
        )

    async def start_container(self, identifier: str) -> None:
        await self._request(
            "POST", f"/containers/{quote(identifier, safe='')}/start", expected=(204, 304)
        )

    async def remove_container(self, identifier: str, *, force: bool = False) -> None:
        await self._request(
            "DELETE",
            f"/containers/{quote(identifier, safe='')}",
            params={"force": str(force).lower(), "v": "false"},
            expected=(204,),
        )

    async def rename_container(self, identifier: str, name: str) -> None:
        await self._request(
            "POST",
            f"/containers/{quote(identifier, safe='')}/rename",
            params={"name": name},
            expected=(204,),
        )

    async def disconnect_network(self, network: str, container: str) -> None:
        await self._request(
            "POST",
            f"/networks/{quote(network, safe='')}/disconnect",
            json={"Container": container, "Force": True},
            expected=(200,),
        )

    async def connect_network(
        self, network: str, container: str, endpoint_config: JsonObject
    ) -> None:
        await self._request(
            "POST",
            f"/networks/{quote(network, safe='')}/connect",
            json={"Container": container, "EndpointConfig": endpoint_config},
            expected=(200,),
        )

    async def exists(self, identifier: str | None) -> bool:
        if identifier is None:
            return False
        try:
            await self.inspect_container(identifier)
        except DockerNotFound:
            return False
        return True

    async def health_status(self, identifier: str) -> str:
        container = await self.inspect_container(identifier)
        state = container.get("State")
        if not isinstance(state, dict):
            raise DockerError("docker_invalid_container_state")
        state_data = cast(JsonObject, state)
        health = state_data.get("Health")
        if not isinstance(health, dict):
            raise DockerError("target_healthcheck_missing")
        status = cast(JsonObject, health).get("Status")
        if not isinstance(status, str):
            raise DockerError("target_healthcheck_missing")
        return status

    async def wait_healthy(self, identifier: str, *, timeout: float) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise DockerError("replacement_health_timeout")
            try:
                async with asyncio.timeout(remaining):
                    container = await self.inspect_container(identifier)
            except TimeoutError as exc:
                raise DockerError("replacement_health_timeout") from exc
            state = container.get("State")
            if not isinstance(state, dict):
                raise DockerError("docker_invalid_container_state")
            state_data = cast(JsonObject, state)
            status = state_data.get("Status")
            health = state_data.get("Health")
            if not isinstance(health, dict):
                raise DockerError("target_healthcheck_missing")
            health_status = cast(JsonObject, health).get("Status")
            if health_status == "healthy":
                return
            if status in {"dead", "exited", "removing"} or health_status == "unhealthy":
                raise DockerError("replacement_unhealthy")
            if loop.time() >= deadline:
                raise DockerError("replacement_health_timeout")
            await self._sleep(1.0)


def split_tag(image_ref: str) -> tuple[str, str]:
    """Split the already-validated repository:tag without confusing registry ports."""
    slash = image_ref.rfind("/")
    colon = image_ref.rfind(":")
    if colon <= slash:
        raise DockerError("configured_image_requires_tag")
    return image_ref[:colon], image_ref[colon + 1 :]


def image_id(image: JsonObject) -> str:
    value = image.get("Id")
    if not isinstance(value, str) or not value.startswith("sha256:"):
        raise DockerError("docker_image_id_missing")
    return value


def image_digest(image: JsonObject, image_ref: str) -> str:
    repository, _tag = split_tag(image_ref)
    values = image.get("RepoDigests")
    if isinstance(values, list):
        for value in cast(list[object], values):
            if isinstance(value, str) and value.startswith(f"{repository}@sha256:"):
                return value
    return image_id(image)


def image_build(image: JsonObject) -> str | None:
    config = image.get("Config")
    if not isinstance(config, dict):
        return None
    environment = cast(JsonObject, config).get("Env")
    if not isinstance(environment, list):
        return None
    for item in cast(list[object], environment):
        if isinstance(item, str) and item.startswith("PLEX_MANAGER_BUILD_ID="):
            value = item.partition("=")[2]
            return value[:255] if value else None
    return None
