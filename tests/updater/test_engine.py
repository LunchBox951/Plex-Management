"""Docker Engine HTTP primitives, image identity, and digest handling."""

from __future__ import annotations

import json

import httpx
import pytest

from plex_manager.updater.engine import (
    DockerEngine,
    DockerError,
    image_build,
    image_digest,
    image_id,
    split_tag,
)

IMAGE_REF = "registry.example.test:5443/media/plex-manager:stable"
IMAGE_ID = "sha256:" + "a" * 64
DIGEST = "registry.example.test:5443/media/plex-manager@sha256:" + "b" * 64


async def test_pull_negotiates_api_and_inspects_the_immutable_result() -> None:
    seen: list[httpx.Request] = []
    image = {
        "Id": IMAGE_ID,
        "RepoDigests": [DIGEST],
        "Config": {"Env": ["PLEX_MANAGER_BUILD_ID=build-200", "OTHER=value"]},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "GET" and request.url.path == "/version":
            return httpx.Response(200, json={"ApiVersion": "1.51", "MinAPIVersion": "1.24"})
        if request.method == "POST" and request.url.path == "/v1.47/images/create":
            return httpx.Response(
                200,
                text='{"status":"Pull complete"}\n{"status":"Digest: sha256:test"}\n',
            )
        if request.method == "GET" and request.url.path.endswith("/json"):
            return httpx.Response(200, json=image)
        raise AssertionError(f"unexpected Docker request {request.method} {request.url}")

    async with httpx.AsyncClient(
        base_url="http://docker", transport=httpx.MockTransport(handler)
    ) as http:
        engine = DockerEngine("/unused/in-mock.sock", client=http)
        pulled = await engine.pull(IMAGE_REF)

    assert pulled == image
    assert [request.method for request in seen] == ["GET", "POST", "GET"]
    assert dict(seen[1].url.params) == {
        "fromImage": "registry.example.test:5443/media/plex-manager",
        "tag": "stable",
    }
    assert seen[2].url.path == f"/v1.47/images/{IMAGE_REF}/json"
    assert image_id(pulled) == IMAGE_ID
    assert image_digest(pulled, IMAGE_REF) == DIGEST
    assert image_build(pulled) == "build-200"


def test_image_identity_helpers_support_digest_no_op_comparison() -> None:
    first = {"Id": IMAGE_ID, "RepoDigests": [DIGEST], "Config": {"Env": []}}
    second = {"Id": IMAGE_ID, "RepoDigests": [DIGEST], "Config": {"Env": []}}

    assert image_id(first) == image_id(second)
    assert image_digest(first, IMAGE_REF) == image_digest(second, IMAGE_REF)
    assert image_build(first) is None
    assert split_tag(IMAGE_REF) == (
        "registry.example.test:5443/media/plex-manager",
        "stable",
    )


async def test_pull_error_body_becomes_a_bounded_code_without_body_disclosure() -> None:
    private_registry_message = "credential=must-not-escape"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/version":
            return httpx.Response(200, json={"ApiVersion": "1.47"})
        return httpx.Response(
            200,
            text=json.dumps(
                {
                    "error": private_registry_message,
                    "errorDetail": {"message": private_registry_message},
                }
            )
            + "\n",
        )

    async with httpx.AsyncClient(
        base_url="http://docker", transport=httpx.MockTransport(handler)
    ) as http:
        engine = DockerEngine("/unused/in-mock.sock", client=http)
        with pytest.raises(DockerError) as caught:
            await engine.pull(IMAGE_REF)

    assert caught.value.code == "docker_pull_failed"
    assert private_registry_message not in str(caught.value)
