"""Updater orchestration, rollback, and interrupted-stage recovery."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import pytest

from plex_manager.updater.config import (
    IMAGE_REF_LABEL,
    ROLE_LABEL,
    TARGET_LABEL,
    UpdaterConfig,
)
from plex_manager.updater.coordinator import (
    CoordinatorError,
    Eligibility,
    LeaseStatus,
)
from plex_manager.updater.engine import DockerError
from plex_manager.updater.runner import UpdaterError, UpdaterRunner
from plex_manager.updater.state import UpdateStage, UpdateState

IMAGE_REF = "ghcr.io/lunchbox951/plex-manager:stable"
IMAGE_ID = "sha256:" + "a" * 64
DIGEST = "ghcr.io/lunchbox951/plex-manager@sha256:" + "b" * 64
NEW_IMAGE_ID = "sha256:" + "c" * 64
NEW_DIGEST = "ghcr.io/lunchbox951/plex-manager@sha256:" + "d" * 64


class _EmptyState:
    def load(self) -> None:
        return None


class _MemoryState:
    def __init__(self, value: UpdateState | None = None) -> None:
        self.value = value
        self.saved_stages: list[str] = []

    def load(self) -> UpdateState | None:
        return self.value

    def save(self, value: UpdateState) -> None:
        self.value = value
        self.saved_stages.append(value.stage)

    def clear(self) -> None:
        self.value = None


class _NoOpEngine:
    def __init__(self) -> None:
        self.image = {
            "Id": IMAGE_ID,
            "RepoDigests": [DIGEST],
            "Config": {"Env": ["PLEX_MANAGER_BUILD_ID=build-current"]},
        }
        self.target = {
            "Id": "container-id",
            "Name": "/plex-manager",
            "Image": IMAGE_ID,
            "Config": {
                "Labels": {TARGET_LABEL: "true", IMAGE_REF_LABEL: IMAGE_REF},
                "Healthcheck": {"Test": ["CMD", "healthcheck"]},
            },
        }
        self.pull_calls = 0

    async def inspect_container(self, _identifier: str) -> dict[str, Any]:
        return self.target

    async def inspect_image(self, _identifier: str) -> dict[str, Any]:
        return self.image

    async def pull(self, _image_ref: str) -> dict[str, Any]:
        self.pull_calls += 1
        return self.image


class _InstallCoordinator:
    def __init__(self) -> None:
        self.claim_calls = 0
        self.outcomes: list[dict[str, object]] = []

    async def eligibility(self) -> Eligibility:
        return Eligibility(
            action="install",
            automatic_enabled=True,
            window_open=True,
            idle_only=True,
            blocker=None,
        )

    async def claim(self) -> None:
        self.claim_calls += 1

    async def outcome(self, **values: object) -> None:
        self.outcomes.append(values)


class _Coordinator:
    def __init__(
        self,
        *,
        claim: LeaseStatus | None = None,
        fail_success_outcomes: int = 0,
        renew_conflicts: int = 0,
    ) -> None:
        self.claim_result = claim or LeaseStatus(
            lease_token="l" * 32,
            ready=True,
            lease_seconds=120,
            blocker=None,
            action_generation=1,
        )
        self.fail_success_outcomes = fail_success_outcomes
        self.renew_conflicts = renew_conflicts
        self.outcomes: list[dict[str, object]] = []
        self.claim_calls = 0
        self.renew_calls = 0
        self.release_calls = 0

    async def eligibility(self) -> Eligibility:
        return Eligibility(
            action="install",
            automatic_enabled=True,
            window_open=True,
            idle_only=True,
            blocker=None,
        )

    async def claim(
        self, *, recovery: bool = False, expected_generation: int | None = None
    ) -> LeaseStatus:
        del recovery, expected_generation
        self.claim_calls += 1
        return self.claim_result

    async def renew(self, lease_token: str) -> LeaseStatus:
        self.renew_calls += 1
        if self.renew_conflicts:
            self.renew_conflicts -= 1
            raise CoordinatorError("coordinator_conflict")
        return LeaseStatus(
            lease_token=lease_token,
            ready=True,
            lease_seconds=self.claim_result.lease_seconds,
            blocker=None,
        )

    async def release(self, _lease_token: str) -> None:
        self.release_calls += 1

    async def outcome(self, **values: object) -> None:
        if values.get("outcome") == "succeeded" and self.fail_success_outcomes:
            self.fail_success_outcomes -= 1
            raise CoordinatorError("coordinator_unavailable")
        self.outcomes.append(values)


def _image(identifier: str, digest: str, build: str) -> dict[str, Any]:
    health = {"Test": ["CMD", "healthcheck"], "Interval": 1_000_000_000}
    return {
        "Id": identifier,
        "RepoDigests": [digest],
        "Config": {
            "Env": [f"PLEX_MANAGER_BUILD_ID={build}", "IMAGE_DEFAULT=1"],
            "Entrypoint": ["/usr/local/bin/entrypoint.sh"],
            "Cmd": None,
            "User": "appuser",
            "WorkingDir": "/app",
            "Healthcheck": health,
            "Labels": {"org.opencontainers.image.revision": build},
        },
    }


def _target() -> dict[str, Any]:
    return {
        "Id": "old-container",
        "Name": "/plex-manager",
        "Image": IMAGE_ID,
        "Config": {
            "Hostname": "old-containe",
            "Env": [
                "PLEX_MANAGER_BUILD_ID=build-old",
                "IMAGE_DEFAULT=1",
                "RUNTIME_SETTING=kept",
            ],
            "Entrypoint": ["/usr/local/bin/entrypoint.sh"],
            "Cmd": None,
            "User": "appuser",
            "WorkingDir": "/app",
            "Healthcheck": {"Test": ["CMD", "healthcheck"], "Interval": 1_000_000_000},
            "Labels": {
                TARGET_LABEL: "true",
                IMAGE_REF_LABEL: IMAGE_REF,
                "com.docker.compose.image": IMAGE_ID,
                "com.docker.compose.service": "plex-manager",
                "org.opencontainers.image.revision": "build-old",
            },
        },
        "HostConfig": {
            "AutoRemove": False,
            "Binds": ["pm-data:/app/data:rw", "/srv/media:/media:rw"],
            "NetworkMode": "pm_default",
            "PortBindings": {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8000"}]},
            "RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
        },
        "NetworkSettings": {
            "Networks": {
                "pm_default": {
                    "Aliases": ["plex-manager"],
                    "IPAMConfig": None,
                    "IPAddress": "172.20.0.2",
                    "EndpointID": "allocated",
                }
            }
        },
        "State": {"Status": "running", "Health": {"Status": "healthy"}},
    }


class _Engine:
    def __init__(self, *, unhealthy_candidate: bool = False) -> None:
        self.images = {
            IMAGE_ID: _image(IMAGE_ID, DIGEST, "build-old"),
            NEW_IMAGE_ID: _image(NEW_IMAGE_ID, NEW_DIGEST, "build-new"),
        }
        self.containers: dict[str, dict[str, Any]] = {"old-container": _target()}
        self.unhealthy_candidate = unhealthy_candidate
        self.calls: list[tuple[object, ...]] = []
        self.next_id = 1

    def container(self, identifier: str) -> dict[str, Any]:
        if identifier in self.containers:
            return self.containers[identifier]
        for container in self.containers.values():
            if container["Name"] == f"/{identifier}":
                return container
        raise DockerError("docker_object_not_found", status_code=404)

    async def inspect_container(self, identifier: str) -> dict[str, Any]:
        return self.container(identifier)

    async def inspect_image(self, identifier: str) -> dict[str, Any]:
        return self.images[identifier]

    async def pull(self, _image_ref: str) -> dict[str, Any]:
        self.calls.append(("pull",))
        return self.images[NEW_IMAGE_ID]

    async def exists(self, identifier: str | None) -> bool:
        return identifier is not None and identifier in self.containers

    async def stop_container(self, identifier: str, *, timeout: int) -> None:
        self.calls.append(("stop", identifier, timeout))
        self.container(identifier)["State"]["Status"] = "exited"

    async def disconnect_network(self, network: str, identifier: str) -> None:
        self.calls.append(("disconnect", network, identifier))
        self.container(identifier)["NetworkSettings"]["Networks"].pop(network, None)

    async def connect_network(
        self, network: str, identifier: str, endpoint: dict[str, Any]
    ) -> None:
        self.calls.append(("connect", network, identifier))
        self.container(identifier)["NetworkSettings"]["Networks"][network] = endpoint

    async def create_container(self, name: str, spec: dict[str, Any]) -> str:
        identifier = f"created-{self.next_id}"
        self.next_id += 1
        role = spec["Labels"][ROLE_LABEL]
        primary = spec.get("NetworkingConfig", {}).get("EndpointsConfig", {})
        self.containers[identifier] = {
            "Id": identifier,
            "Name": f"/{name}",
            "Image": spec["Image"],
            "Config": {
                key: value
                for key, value in spec.items()
                if key not in {"HostConfig", "NetworkingConfig"}
            },
            "HostConfig": spec["HostConfig"],
            "NetworkSettings": {"Networks": dict(primary)},
            "State": {"Status": "created", "Health": {"Status": "starting"}},
        }
        self.calls.append(("create", role, identifier))
        return identifier

    async def start_container(self, identifier: str) -> None:
        container = self.container(identifier)
        role = container["Config"]["Labels"][ROLE_LABEL]
        container["State"] = {
            "Status": "running",
            "Health": {
                "Status": "unhealthy"
                if role == "candidate" and self.unhealthy_candidate
                else "healthy"
            },
        }
        self.calls.append(("start", role, identifier))

    async def wait_healthy(self, identifier: str, *, timeout: float) -> None:
        del timeout
        status = self.container(identifier)["State"]["Health"]["Status"]
        self.calls.append(("health", identifier, status))
        if status != "healthy":
            raise DockerError("replacement_unhealthy")

    async def rename_container(self, identifier: str, name: str) -> None:
        self.container(identifier)["Name"] = f"/{name}"
        self.calls.append(("rename", identifier, name))

    async def remove_container(self, identifier: str, *, force: bool = False) -> None:
        del force
        self.calls.append(("remove", identifier))
        self.containers.pop(identifier)

    async def containers_by_labels(self, labels: dict[str, str]) -> list[dict[str, Any]]:
        return [
            container
            for container in self.containers.values()
            if all(container["Config"]["Labels"].get(key) == value for key, value in labels.items())
        ]


class _SlowHealthyEngine(_Engine):
    async def wait_healthy(self, identifier: str, *, timeout: float) -> None:
        await asyncio.sleep(0.45)
        await super().wait_healthy(identifier, timeout=timeout)


def _config(tmp_path: Path) -> UpdaterConfig:
    return UpdaterConfig(
        image_ref=IMAGE_REF,
        container_name="plex-manager",
        docker_socket="/var/run/docker.sock",
        coordinator_url="http://plex-manager:8000/api/v1/internal/updates",
        secret_file=tmp_path / "secret",
        state_file=tmp_path / "state.json",
        poll_seconds=30,
        request_timeout_seconds=10,
        health_timeout_seconds=240,
        drain_timeout_seconds=300,
    )


def _pending(stage: UpdateStage, *, candidate_id: str | None = None) -> UpdateState:
    return UpdateState(
        version=1,
        operation_id="operation1234567890",
        operation="install",
        stage=stage,
        lease_token="l" * 32,
        action_generation=1,
        target_id="old-container",
        target_name="plex-manager",
        old_image_id=IMAGE_ID,
        old_digest=DIGEST,
        old_build="build-old",
        desired_image_id=NEW_IMAGE_ID,
        desired_digest=NEW_DIGEST,
        desired_build="build-new",
        networks={"pm_default": {"Aliases": ["plex-manager"]}},
        port_bindings={"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8000"}]},
        candidate_id=candidate_id,
    )


async def test_unchanged_image_is_a_check_no_op_without_maintenance_claim(
    tmp_path: Path,
) -> None:
    config = UpdaterConfig(
        image_ref=IMAGE_REF,
        container_name="plex-manager",
        docker_socket="/var/run/docker.sock",
        coordinator_url="http://plex-manager:8000/api/v1/internal/updates",
        secret_file=tmp_path / "secret",
        state_file=tmp_path / "state.json",
        poll_seconds=30,
        request_timeout_seconds=10,
        health_timeout_seconds=240,
        drain_timeout_seconds=300,
    )
    engine = _NoOpEngine()
    coordinator = _InstallCoordinator()
    runner = UpdaterRunner(
        config,
        cast(Any, engine),
        cast(Any, coordinator),
        cast(Any, _EmptyState()),
    )

    await runner.run_once()

    assert engine.pull_calls == 1
    assert coordinator.claim_calls == 0
    assert coordinator.outcomes == [
        {
            "operation": "check",
            "outcome": "no_update",
            "current_digest": DIGEST,
            "available_digest": DIGEST,
            "current_build": "build-current",
            "available_build": "build-current",
        }
    ]


async def test_successful_install_recreates_then_acknowledges_before_removing_previous(
    tmp_path: Path,
) -> None:
    engine = _Engine()
    coordinator = _Coordinator()
    state = _MemoryState()
    runner = UpdaterRunner(
        _config(tmp_path), cast(Any, engine), cast(Any, coordinator), cast(Any, state)
    )

    await runner.run_once()

    current = engine.container("plex-manager")
    assert current["Image"] == NEW_IMAGE_ID
    assert current["HostConfig"]["Binds"] == [
        "pm-data:/app/data:rw",
        "/srv/media:/media:rw",
    ]
    assert "old-container" not in engine.containers
    assert state.value is None
    assert coordinator.outcomes[-1] == {
        "operation": "install",
        "outcome": "succeeded",
        "lease_token": "l" * 32,
        "current_digest": NEW_DIGEST,
        "available_digest": NEW_DIGEST,
        "current_build": "build-new",
        "available_build": "build-new",
        "from_build": "build-old",
        "to_build": "build-new",
    }
    assert state.saved_stages[:5] == [
        "prepared",
        "stop_requested",
        "old_stopped",
        "old_disconnected",
        "candidate_created",
    ]
    assert state.saved_stages[-1] == "outcome_acknowledged"


async def test_live_install_renews_lease_while_health_gate_is_running(tmp_path: Path) -> None:
    engine = _SlowHealthyEngine()
    coordinator = _Coordinator(
        claim=LeaseStatus(
            lease_token="l" * 32,
            ready=True,
            lease_seconds=1,
            blocker=None,
            action_generation=1,
        )
    )
    state = _MemoryState()
    runner = UpdaterRunner(
        _config(tmp_path), cast(Any, engine), cast(Any, coordinator), cast(Any, state)
    )

    await runner.run_once()

    assert coordinator.renew_calls >= 2
    assert state.value is None


async def test_unhealthy_candidate_rolls_back_with_direct_non_migrating_entrypoint(
    tmp_path: Path,
) -> None:
    engine = _Engine(unhealthy_candidate=True)
    coordinator = _Coordinator()
    state = _MemoryState()
    runner = UpdaterRunner(
        _config(tmp_path), cast(Any, engine), cast(Any, coordinator), cast(Any, state)
    )

    await runner.run_once()

    current = engine.container("plex-manager")
    assert current["Image"] == IMAGE_ID
    assert current["Config"]["Entrypoint"] == ["python", "-m", "plex_manager"]
    assert not any(
        container["Config"]["Labels"].get(ROLE_LABEL) == "candidate"
        for container in engine.containers.values()
    )
    assert coordinator.outcomes[-1]["outcome"] == "rolled_back"
    assert coordinator.outcomes[-1]["detail_code"] == "replacement_unhealthy"
    assert state.value is None


async def test_prepared_interruption_acknowledges_failure_without_docker_mutation(
    tmp_path: Path,
) -> None:
    engine = _Engine()
    coordinator = _Coordinator()
    state = _MemoryState(_pending("prepared"))
    runner = UpdaterRunner(
        _config(tmp_path), cast(Any, engine), cast(Any, coordinator), cast(Any, state)
    )

    await runner.run_once()

    assert engine.calls == []
    assert coordinator.outcomes[-1]["outcome"] == "failed"
    assert coordinator.outcomes[-1]["detail_code"] == "update_interrupted"
    assert state.value is None


async def _seed_healthy_candidate(engine: _Engine, *, canonical: bool) -> str:
    from plex_manager.updater.recreation import build_candidate_spec, capture_networks

    target = engine.container("old-container")
    networks = capture_networks(target)
    await engine.rename_container("old-container", "plex-manager-previous-operation123")
    spec, _primary = build_candidate_spec(
        target,
        engine.images[IMAGE_ID],
        engine.images[NEW_IMAGE_ID],
        image_ref=IMAGE_REF,
        operation_id="operation1234567890",
        networks=networks,
    )
    candidate = await engine.create_container("plex-manager-candidate-operation123", spec)
    await engine.start_container(candidate)
    await engine.stop_container("old-container", timeout=10)
    await engine.disconnect_network("pm_default", "old-container")
    if canonical:
        await engine.rename_container("old-container", "plex-manager-previous-operation123")
        await engine.rename_container(candidate, "plex-manager")
    engine.calls.clear()
    return candidate


async def test_candidate_started_interruption_finishes_healthy_cutover(tmp_path: Path) -> None:
    engine = _Engine()
    candidate = await _seed_healthy_candidate(engine, canonical=False)
    coordinator = _Coordinator()
    state = _MemoryState(_pending("candidate_started", candidate_id=candidate))
    runner = UpdaterRunner(
        _config(tmp_path), cast(Any, engine), cast(Any, coordinator), cast(Any, state)
    )

    await runner.run_once()

    assert engine.container("plex-manager")["Image"] == NEW_IMAGE_ID
    assert coordinator.outcomes[-1]["outcome"] == "succeeded"
    assert not any(call[:2] == ("create", "rollback") for call in engine.calls)


async def test_expired_recovery_lease_reclaims_before_any_docker_mutation(
    tmp_path: Path,
) -> None:
    engine = _Engine()
    coordinator = _Coordinator(
        renew_conflicts=1,
        claim=LeaseStatus(
            lease_token=None,
            ready=False,
            lease_seconds=120,
            blocker="active_critical_work",
            action_generation=1,
        ),
    )
    state = _MemoryState(_pending("stop_requested"))
    runner = UpdaterRunner(
        _config(tmp_path), cast(Any, engine), cast(Any, coordinator), cast(Any, state)
    )

    await runner.run_once()

    assert coordinator.renew_calls == 1
    assert coordinator.claim_calls == 1
    assert engine.calls == []
    assert state.value is not None
    assert state.value.stage == "stop_requested"


async def test_discovered_rollback_container_resumes_from_create_before_save(
    tmp_path: Path,
) -> None:
    from plex_manager.updater.recreation import build_rollback_spec, capture_networks

    engine = _Engine(unhealthy_candidate=True)
    target = engine.container("old-container")
    networks = capture_networks(target)
    spec, _primary = build_rollback_spec(
        target,
        engine.images[IMAGE_ID],
        image_ref=IMAGE_REF,
        operation_id="operation1234567890",
        networks=networks,
        port_bindings={"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8000"}]},
    )
    rollback_id = await engine.create_container("plex-manager", spec)
    engine.calls.clear()
    coordinator = _Coordinator()
    state = _MemoryState(_pending("old_disconnected"))
    runner = UpdaterRunner(
        _config(tmp_path), cast(Any, engine), cast(Any, coordinator), cast(Any, state)
    )

    await runner.run_once()

    assert ("start", "rollback", rollback_id) in engine.calls
    assert coordinator.outcomes[-1]["outcome"] == "rolled_back"
    assert state.value is None


@pytest.mark.parametrize(
    "stage",
    ["rollback_created", "rollback_started", "rollback_healthy", "rollback_acknowledged"],
)
async def test_each_persisted_rollback_stage_converges(tmp_path: Path, stage: UpdateStage) -> None:
    from plex_manager.updater.recreation import build_rollback_spec, capture_networks

    engine = _Engine()
    target = engine.container("old-container")
    networks = capture_networks(target)
    await engine.rename_container("old-container", "plex-manager-previous-operation123")
    spec, _primary = build_rollback_spec(
        target,
        engine.images[IMAGE_ID],
        image_ref=IMAGE_REF,
        operation_id="operation1234567890",
        networks=networks,
        port_bindings={"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8000"}]},
    )
    rollback_id = await engine.create_container("plex-manager", spec)
    if stage in {"rollback_started", "rollback_healthy", "rollback_acknowledged"}:
        await engine.start_container(rollback_id)
    engine.calls.clear()
    pending = _pending("old_disconnected")
    pending.stage = stage
    pending.rollback_id = rollback_id
    pending.detail_code = "replacement_unhealthy"
    coordinator = _Coordinator()
    state = _MemoryState(pending)
    runner = UpdaterRunner(
        _config(tmp_path), cast(Any, engine), cast(Any, coordinator), cast(Any, state)
    )

    await runner.run_once()

    assert state.value is None
    assert not await engine.exists("old-container")
    if stage == "rollback_acknowledged":
        assert coordinator.outcomes == []
    else:
        assert coordinator.outcomes[-1]["outcome"] == "rolled_back"
        assert coordinator.outcomes[-1]["detail_code"] == "replacement_unhealthy"


async def test_disabled_target_healthcheck_is_rejected_before_pull_or_claim(
    tmp_path: Path,
) -> None:
    engine = _Engine()
    engine.containers["old-container"]["Config"]["Healthcheck"] = {"Test": ["NONE"]}
    coordinator = _Coordinator()
    runner = UpdaterRunner(
        _config(tmp_path), cast(Any, engine), cast(Any, coordinator), cast(Any, _MemoryState())
    )

    with pytest.raises(UpdaterError, match="target_healthcheck_missing"):
        await runner.run_once()

    assert engine.calls == []
    assert coordinator.claim_calls == 0


async def test_lost_success_outcome_retries_without_rolling_back_healthy_candidate(
    tmp_path: Path,
) -> None:
    engine = _Engine()
    candidate = await _seed_healthy_candidate(engine, canonical=True)
    coordinator = _Coordinator(fail_success_outcomes=1)
    state = _MemoryState(_pending("candidate_renamed", candidate_id=candidate))
    runner = UpdaterRunner(
        _config(tmp_path), cast(Any, engine), cast(Any, coordinator), cast(Any, state)
    )

    with pytest.raises(CoordinatorError, match="coordinator_unavailable"):
        await runner.run_once()

    assert state.value is not None
    assert state.value.stage == "candidate_renamed"
    assert engine.container("plex-manager")["Image"] == NEW_IMAGE_ID
    assert "old-container" in engine.containers
    assert not any(call[:2] == ("create", "rollback") for call in engine.calls)

    await runner.run_once()

    assert state.value is None
    assert "old-container" not in engine.containers
    assert coordinator.outcomes[-1]["outcome"] == "succeeded"


async def test_null_busy_claim_is_clean_deferral_without_stopping_target(tmp_path: Path) -> None:
    engine = _Engine()
    coordinator = _Coordinator(
        claim=LeaseStatus(
            lease_token=None,
            ready=False,
            lease_seconds=120,
            blocker="concurrent_update_claim",
        )
    )
    state = _MemoryState()
    runner = UpdaterRunner(
        _config(tmp_path), cast(Any, engine), cast(Any, coordinator), cast(Any, state)
    )

    await runner.run_once()

    assert coordinator.claim_calls == 1
    assert not any(call[0] == "stop" for call in engine.calls)
    assert coordinator.outcomes == []
    assert state.value is None


async def test_name_match_without_required_target_label_is_rejected_before_pull(
    tmp_path: Path,
) -> None:
    engine = _Engine()
    engine.containers["old-container"]["Config"]["Labels"].pop(TARGET_LABEL)
    coordinator = _Coordinator()
    runner = UpdaterRunner(
        _config(tmp_path),
        cast(Any, engine),
        cast(Any, coordinator),
        cast(Any, _MemoryState()),
    )

    with pytest.raises(UpdaterError, match="configured_target_label_missing"):
        await runner.run_once()

    assert engine.calls == []
    assert coordinator.claim_calls == 0
