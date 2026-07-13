"""Opt-in disposable-Docker recreation and rollback coverage."""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from contextlib import suppress
from pathlib import Path

import pytest

from plex_manager.updater.config import IMAGE_REF_LABEL, TARGET_LABEL
from plex_manager.updater.engine import DockerEngine, DockerError, image_id
from plex_manager.updater.recreation import (
    build_candidate_spec,
    build_rollback_spec,
    capture_networks,
    remaining_networks,
)

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(
        os.environ.get("PLEX_MANAGER_RUN_DOCKER_TESTS") != "1",
        reason="set PLEX_MANAGER_RUN_DOCKER_TESTS=1 to permit disposable Docker resources",
    ),
    pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI is unavailable"),
]

_FIXTURE = Path(__file__).with_name("fixtures")
_DOCKER_PATH = shutil.which("docker")


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    if _DOCKER_PATH is None:  # pragma: no cover - module-level marker skips first
        raise RuntimeError("Docker CLI is unavailable")
    return subprocess.run(  # noqa: S603 -- fixed executable and test-generated arguments only
        [_DOCKER_PATH, *args],
        check=check,
        capture_output=True,
        text=True,
    )


@pytest.mark.asyncio
async def test_real_engine_unhealthy_replacement_rolls_back_preserved_runtime(
    tmp_path: Path,
) -> None:
    suffix = uuid.uuid4().hex[:12]
    old_tag = f"plex-manager-updater-fixture:{suffix}-old"
    bad_tag = f"plex-manager-updater-fixture:{suffix}-bad"
    network = f"pm-updater-net-{suffix}"
    volume = f"pm-updater-data-{suffix}"
    target_name = f"pm-updater-target-{suffix}"
    candidate_name = f"{target_name}-candidate"
    previous_name = f"{target_name}-previous"
    bind = tmp_path / "bind"
    bind.mkdir()
    engine = DockerEngine("/var/run/docker.sock")
    created_ids: set[str] = set()

    try:
        _docker(
            "build",
            "--quiet",
            "--tag",
            old_tag,
            "--build-arg",
            "BUILD_ID=fixture-old",
            "--build-arg",
            "HEALTHY=1",
            str(_FIXTURE),
        )
        _docker(
            "build",
            "--quiet",
            "--tag",
            bad_tag,
            "--build-arg",
            "BUILD_ID=fixture-bad",
            "--build-arg",
            "HEALTHY=0",
            str(_FIXTURE),
        )
        _docker("network", "create", network)
        _docker("volume", "create", volume)
        old_image = await engine.inspect_image(old_tag)
        bad_image = await engine.inspect_image(bad_tag)
        original_id = await engine.create_container(
            target_name,
            {
                "Image": image_id(old_image),
                "Env": ["RUNTIME_SETTING=preserved"],
                "Labels": {
                    TARGET_LABEL: "true",
                    IMAGE_REF_LABEL: old_tag,
                    "integration.fixture": suffix,
                },
                "ExposedPorts": {"8000/tcp": {}},
                "HostConfig": {
                    "AutoRemove": False,
                    "Binds": [f"{volume}:/data:rw", f"{bind}:/bind:rw"],
                    "NetworkMode": network,
                    "PortBindings": {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": ""}]},
                    "RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
                },
                "NetworkingConfig": {
                    "EndpointsConfig": {network: {"Aliases": [target_name, "plex-manager"]}}
                },
            },
        )
        created_ids.add(original_id)
        await engine.start_container(original_id)
        await engine.wait_healthy(original_id, timeout=20)
        original = await engine.inspect_container(original_id)
        networks = capture_networks(original)

        await engine.stop_container(original_id, timeout=5)
        await engine.disconnect_network(network, original_id)
        candidate_spec, primary = build_candidate_spec(
            original,
            old_image,
            bad_image,
            image_ref=old_tag,
            operation_id=suffix,
            networks=networks,
        )
        candidate_id = await engine.create_container(candidate_name, candidate_spec)
        created_ids.add(candidate_id)
        for name, endpoint in remaining_networks(networks, primary):
            await engine.connect_network(name, candidate_id, endpoint)
        await engine.start_container(candidate_id)
        with pytest.raises(DockerError, match="replacement_unhealthy"):
            await engine.wait_healthy(candidate_id, timeout=20)
        await engine.remove_container(candidate_id, force=True)
        created_ids.discard(candidate_id)

        await engine.rename_container(original_id, previous_name)
        rollback_spec, primary = build_rollback_spec(
            original,
            old_image,
            image_ref=old_tag,
            operation_id=suffix,
            networks=networks,
        )
        rollback_id = await engine.create_container(target_name, rollback_spec)
        created_ids.add(rollback_id)
        for name, endpoint in remaining_networks(networks, primary):
            await engine.connect_network(name, rollback_id, endpoint)
        await engine.start_container(rollback_id)
        await engine.wait_healthy(rollback_id, timeout=20)
        rollback = await engine.inspect_container(rollback_id)

        assert rollback["Image"] == original["Image"]
        assert rollback["Config"]["Entrypoint"] == ["python", "-m", "plex_manager"]
        assert "RUNTIME_SETTING=preserved" in rollback["Config"]["Env"]
        assert rollback["HostConfig"]["Binds"] == original["HostConfig"]["Binds"]
        assert rollback["HostConfig"]["PortBindings"] == original["HostConfig"]["PortBindings"]
        assert set(rollback["NetworkSettings"]["Networks"]) == set(networks)
    finally:
        for identifier in created_ids:
            with suppress(subprocess.SubprocessError):
                _docker("rm", "--force", identifier)
        await engine.close()
        _docker("network", "rm", network, check=False)
        _docker("volume", "rm", "--force", volume, check=False)
        _docker("image", "rm", "--force", old_tag, bad_tag, check=False)
