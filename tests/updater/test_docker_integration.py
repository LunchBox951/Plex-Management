"""Opt-in disposable-Docker recreation and rollback coverage."""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest

from plex_manager.updater.config import IMAGE_REF_LABEL, TARGET_LABEL, UpdaterConfig
from plex_manager.updater.coordinator import Eligibility, LeaseStatus
from plex_manager.updater.engine import DockerEngine, DockerError, image_id
from plex_manager.updater.recreation import (
    build_candidate_spec,
    build_rollback_spec,
    capture_networks,
    capture_port_bindings,
    remaining_networks,
)
from plex_manager.updater.runner import UpdaterRunner
from plex_manager.updater.state import StateStore

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


class _LocalPullEngine(DockerEngine):
    async def pull(self, image_ref: str) -> dict[str, Any]:
        return await self.inspect_image(image_ref)


class _RecordingCoordinator:
    def __init__(self, engine: DockerEngine, previous_id: str) -> None:
        self.engine = engine
        self.previous_id = previous_id
        self.outcomes: list[dict[str, object]] = []
        self.previous_existed_at_ack = False
        self.renewals = 0

    async def eligibility(self) -> Eligibility:
        return Eligibility(
            action="install",
            action_generation=1,
            automatic_enabled=True,
            window_open=True,
            idle_only=True,
            blocker=None,
        )

    async def claim(
        self, *, recovery: bool = False, expected_generation: int | None = None
    ) -> LeaseStatus:
        del recovery
        assert expected_generation == 1
        return LeaseStatus(
            lease_token="l" * 32,
            ready=True,
            lease_seconds=600,
            blocker=None,
            action_generation=1,
        )

    async def renew(self, lease_token: str, *, phase: str | None = None) -> LeaseStatus:
        del phase
        self.renewals += 1
        return LeaseStatus(
            lease_token=lease_token,
            ready=True,
            lease_seconds=600,
            blocker=None,
        )

    async def outcome(self, **values: object) -> None:
        self.previous_existed_at_ack = await self.engine.exists(self.previous_id)
        self.outcomes.append(values)

    async def heartbeat(self, *, action_generation: int) -> None:
        assert action_generation == 1


@pytest.mark.asyncio
async def test_real_runner_healthy_cutover_preserves_runtime_and_cleans_state(
    tmp_path: Path,
) -> None:
    suffix = uuid.uuid4().hex[:12]
    moving_tag = f"plex-manager-updater-fixture:{suffix}-moving"
    new_tag = f"plex-manager-updater-fixture:{suffix}-new"
    primary = f"pm-updater-primary-{suffix}"
    secondary = f"pm-updater-secondary-{suffix}"
    volume = f"pm-updater-data-{suffix}"
    target_name = f"pm-updater-target-{suffix}"
    bind = tmp_path / "bind"
    bind.mkdir()
    engine = _LocalPullEngine("/var/run/docker.sock")
    anonymous_volume: str | None = None

    try:
        _docker(
            "build",
            "--quiet",
            "--tag",
            moving_tag,
            "--build-arg",
            "BUILD_ID=fixture-old",
            "--build-arg",
            "HEALTHY=1",
            str(_FIXTURE),
        )
        _docker("network", "create", primary)
        _docker("network", "create", secondary)
        _docker("volume", "create", volume)
        old_image = await engine.inspect_image(moving_tag)
        original_id = await engine.create_container(
            target_name,
            {
                "Image": image_id(old_image),
                "StopTimeout": 90,
                "Env": ["RUNTIME_SETTING=preserved"],
                "Labels": {
                    TARGET_LABEL: "true",
                    IMAGE_REF_LABEL: moving_tag,
                    "integration.fixture": suffix,
                },
                "ExposedPorts": {"8000/tcp": {}},
                "Volumes": {"/anonymous": {}},
                "HostConfig": {
                    "AutoRemove": False,
                    "Binds": [f"{volume}:/data:rw", f"{bind}:/bind:rw"],
                    "NetworkMode": primary,
                    "PortBindings": {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "0"}]},
                    "RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
                },
                "NetworkingConfig": {
                    "EndpointsConfig": {
                        primary: {
                            "Aliases": [target_name, "plex-manager"],
                            "MacAddress": "02:42:ac:10:00:0a",
                        },
                        secondary: {
                            "Aliases": [target_name],
                            "MacAddress": "02:42:ac:11:00:0a",
                        },
                    }
                },
            },
        )
        await engine.start_container(original_id)
        await engine.wait_healthy(original_id, timeout=20)
        original = await engine.inspect_container(original_id)
        original_ports = original["NetworkSettings"]["Ports"]
        original_macs = {
            name: endpoint["MacAddress"]
            for name, endpoint in original["NetworkSettings"]["Networks"].items()
        }
        anonymous = next(
            mount for mount in original["Mounts"] if mount["Destination"] == "/anonymous"
        )
        anonymous_volume = str(anonymous["Name"])
        _docker("exec", target_name, "sh", "-c", "printf preserved > /anonymous/value")

        _docker(
            "build",
            "--quiet",
            "--tag",
            new_tag,
            "--build-arg",
            "BUILD_ID=fixture-new",
            "--build-arg",
            "HEALTHY=1",
            str(_FIXTURE),
        )
        _docker("image", "tag", new_tag, moving_tag)

        state = StateStore(tmp_path / "updater-state.json")
        coordinator = _RecordingCoordinator(engine, original_id)
        runner = UpdaterRunner(
            UpdaterConfig(
                image_ref=moving_tag,
                container_name=target_name,
                docker_socket="/var/run/docker.sock",
                coordinator_url="http://unused/internal/updates",
                secret_file=tmp_path / "secret",
                state_file=state.path,
                poll_seconds=30,
                request_timeout_seconds=10,
                health_timeout_seconds=20,
                drain_timeout_seconds=20,
            ),
            engine,
            coordinator,  # type: ignore[arg-type]
            state,
        )
        await runner.run_once()

        current = await engine.inspect_container(target_name)
        assert current["Image"] != original["Image"]
        assert "RUNTIME_SETTING=preserved" in current["Config"]["Env"]
        assert current["HostConfig"]["Binds"] == original["HostConfig"]["Binds"]
        assert current["NetworkSettings"]["Ports"] == original_ports
        assert set(current["NetworkSettings"]["Networks"]) == {primary, secondary}
        assert {
            name: endpoint["MacAddress"]
            for name, endpoint in current["NetworkSettings"]["Networks"].items()
        } == original_macs
        assert current["Config"]["StopTimeout"] == 90
        assert _docker("exec", target_name, "cat", "/anonymous/value").stdout == "preserved"
        assert coordinator.renewals >= 1
        assert coordinator.previous_existed_at_ack is True
        assert coordinator.outcomes[-1]["outcome"] == "succeeded"
        assert not await engine.exists(original_id)
        assert state.load() is None
    finally:
        _docker("rm", "--force", target_name, check=False)
        leftovers = _docker(
            "ps", "-aq", "--filter", f"label=integration.fixture={suffix}", check=False
        ).stdout.split()
        for identifier in leftovers:
            _docker("rm", "--force", identifier, check=False)
        await engine.close()
        _docker("network", "rm", secondary, check=False)
        _docker("network", "rm", primary, check=False)
        _docker("volume", "rm", "--force", volume, check=False)
        if anonymous_volume is not None:
            _docker("volume", "rm", "--force", anonymous_volume, check=False)
        _docker("image", "rm", "--force", moving_tag, new_tag, check=False)


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
        port_bindings = capture_port_bindings(original)
        original_ports = original["NetworkSettings"]["Ports"]

        await engine.stop_container(original_id, request_timeout=20)
        await engine.disconnect_network(network, original_id)
        candidate_spec, primary = build_candidate_spec(
            original,
            old_image,
            bad_image,
            image_ref=old_tag,
            operation_id=suffix,
            networks=networks,
            port_bindings=port_bindings,
            multi_network_create=await engine.api_version() >= (1, 44),
        )
        # Runtime-environment fidelity intentionally preserves the effective
        # old FIXTURE_HEALTHY=1 value when its provenance is ambiguous. This low-level
        # rollback fixture explicitly injects a failing runtime health mode so
        # it continues to exercise the unhealthy-container path.
        candidate_env = candidate_spec["Env"]
        assert isinstance(candidate_env, list)
        candidate_spec["Env"] = [
            "FIXTURE_HEALTHY=0" if item == "FIXTURE_HEALTHY=1" else item for item in candidate_env
        ]
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
            port_bindings=port_bindings,
            multi_network_create=await engine.api_version() >= (1, 44),
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
        assert rollback["HostConfig"]["PortBindings"] == port_bindings
        assert rollback["NetworkSettings"]["Ports"] == original_ports
        assert set(rollback["NetworkSettings"]["Networks"]) == set(networks)
    finally:
        for identifier in created_ids:
            with suppress(subprocess.SubprocessError):
                _docker("rm", "--force", identifier)
        await engine.close()
        _docker("network", "rm", network, check=False)
        _docker("volume", "rm", "--force", volume, check=False)
        _docker("image", "rm", "--force", old_tag, bad_tag, check=False)
