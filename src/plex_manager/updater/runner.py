"""Digest-aware, crash-recoverable update orchestration."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable
from contextlib import suppress
from typing import Any, Literal, cast

from plex_manager.domain.update_recovery import dispatch_starts_work
from plex_manager.updater.config import (
    IMAGE_REF_LABEL,
    OPERATION_LABEL,
    ROLE_LABEL,
    TARGET_LABEL,
    UpdaterConfig,
)
from plex_manager.updater.coordinator import CoordinatorClient, CoordinatorError, LeaseStatus
from plex_manager.updater.engine import (
    DockerEngine,
    DockerError,
    DockerNotFound,
    JsonObject,
    image_build,
    image_digest,
    image_id,
)
from plex_manager.updater.recreation import (
    MINIMUM_STOP_TIMEOUT,
    build_candidate_spec,
    build_rollback_spec,
    capture_networks,
    capture_port_bindings,
    enabled_healthcheck,
    remaining_networks,
)
from plex_manager.updater.state import StateStore, UpdateStage, UpdateState

_logger = logging.getLogger(__name__)
_PROGRESS_INTERVAL_SECONDS = 15.0
_MAX_STOP_TIMEOUT_SECONDS = 300
_OFFLINE_ROLLBACK_STAGES = frozenset(
    {
        "stop_requested",
        "old_stopped",
        "old_disconnected",
        "candidate_created",
        "candidate_started",
        "rollback_requested",
        "rollback_created",
        "rollback_networked",
        "rollback_started",
    }
)


class UpdaterError(RuntimeError):
    """A bounded sidecar failure whose code is safe to acknowledge and log."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _container_name(container: JsonObject) -> str:
    name = container.get("Name")
    if not isinstance(name, str) or not name.startswith("/"):
        raise UpdaterError("target_name_missing")
    return name[1:]


def _container_image_id(container: JsonObject) -> str:
    value = container.get("Image")
    if not isinstance(value, str) or not value.startswith("sha256:"):
        raise UpdaterError("target_image_id_missing")
    return value


def _config_labels(container: JsonObject) -> dict[str, str]:
    config = container.get("Config")
    if not isinstance(config, dict):
        raise UpdaterError("target_labels_missing")
    labels_value = cast(JsonObject, config).get("Labels")
    if not isinstance(labels_value, dict):
        raise UpdaterError("target_labels_missing")
    labels = cast(dict[object, object], labels_value)
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in labels.items()):
        raise UpdaterError("target_labels_invalid")
    return cast(dict[str, str], labels)


def _stop_timeout(container: JsonObject) -> int:
    config = container.get("Config")
    if not isinstance(config, dict):
        raise UpdaterError("target_stop_timeout_invalid")
    value = cast(JsonObject, config).get("StopTimeout", 10)
    if value is None:
        value = 10
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= _MAX_STOP_TIMEOUT_SECONDS
    ):
        raise UpdaterError("target_stop_timeout_unsupported")
    return value


class UpdaterRunner:
    """Own the one privileged state machine; app policy remains remote."""

    def __init__(
        self,
        config: UpdaterConfig,
        engine: DockerEngine,
        coordinator: CoordinatorClient,
        state: StateStore,
        *,
        sleep: Any = asyncio.sleep,
    ) -> None:
        self.config = config
        self.engine = engine
        self.coordinator = coordinator
        self.state = state
        self._sleep = sleep
        self._lease_stop: asyncio.Event | None = None
        self._lease_task: asyncio.Task[None] | None = None

    async def run_forever(self) -> None:
        while True:
            try:
                await self.run_once()
            except (CoordinatorError, DockerError, UpdaterError) as exc:
                # Codes are fixed locally; Docker/HTTP response bodies and bearer
                # values never enter the log capture pipeline.
                _logger.warning("container updater iteration failed (%s)", exc.code)
            except Exception:
                _logger.exception("container updater iteration failed unexpectedly")
            await self._sleep(self.config.poll_seconds)

    async def run_once(self) -> None:
        pending = self.state.load()
        if pending is not None:
            if pending.stage in {"outcome_acknowledged", "rollback_acknowledged"}:
                await self._recover(pending)
                return
            if pending.stage == "prepared":
                if pending.detail_code is None:
                    pending.detail_code = "update_interrupted"
                    self.state.save(pending)
                try:
                    await self._ack_pre_cutover_failure(pending, pending.detail_code)
                    return
                except CoordinatorError as exc:
                    if exc.code != "coordinator_conflict":
                        raise
            if pending.stage == "candidate_renamed":
                try:
                    await self._finish_success(pending)
                    return
                except CoordinatorError as exc:
                    if exc.code != "coordinator_conflict":
                        raise
                except UpdaterError as exc:
                    if exc.code != "candidate_missing_during_recovery":
                        raise
                except DockerError:
                    pass
            if pending.stage == "rollback_healthy":
                try:
                    await self._finish_rollback_outcome(pending)
                    return
                except CoordinatorError as exc:
                    if exc.code != "coordinator_conflict":
                        raise
                except UpdaterError as exc:
                    if exc.code != "rollback_missing_during_recovery":
                        raise
                except DockerError:
                    pass
            try:
                lease = await self._ensure_recovery_lease(pending)
            except CoordinatorError as exc:
                if (
                    exc.code != "coordinator_unavailable"
                    or pending.stage not in _OFFLINE_ROLLBACK_STAGES
                ):
                    raise
                await self._rollback(
                    pending,
                    detail_code=pending.detail_code or "coordinator_unavailable",
                    acknowledge=False,
                )
                return
            if lease is None:
                return
            await self._run_with_lease_keeper(pending, lease.lease_seconds, self._recover(pending))
            return
        eligibility = await self.coordinator.eligibility()
        # Eligibility blockers are advisory snapshots; the claim below remains
        # the atomic race check. Avoid expensive Docker work when the coordinator
        # already knows an idle-only install cannot currently acquire that claim.
        # This guard and the coordinator's work-dispatch anchor stamp share ONE
        # predicate (``dispatch_starts_work``) so "the runner will act" and
        # "the recovery clock restarts" can never drift apart.
        if not dispatch_starts_work(eligibility.action, eligibility.blocker):
            return
        try:
            preflight = await self._run_with_check_heartbeat(
                eligibility.action_generation,
            )
        except (DockerError, UpdaterError) as exc:
            await self.coordinator.outcome(
                operation="check",
                outcome="failed",
                action_generation=eligibility.action_generation,
                detail_code=exc.code,
            )
            return
        target, old_image, desired = preflight
        old_id = _container_image_id(target)
        desired_id = image_id(desired)
        old_digest = image_digest(old_image, self.config.image_ref)
        desired_digest = image_digest(desired, self.config.image_ref)
        old_build = image_build(old_image)
        desired_build = image_build(desired)

        if old_id == desired_id:
            await self.coordinator.outcome(
                # No mutation occurred and therefore no install lease exists.
                # The coordinator's install outcome contract intentionally
                # requires a lease, so this is a check result even when the
                # triggering eligibility action was install.
                operation="check",
                outcome="no_update",
                action_generation=eligibility.action_generation,
                current_digest=old_digest,
                current_build=old_build,
            )
            return
        await self.coordinator.outcome(
            operation="check",
            outcome="update_available",
            action_generation=eligibility.action_generation,
            current_digest=old_digest,
            available_digest=desired_digest,
            current_build=old_build,
            available_build=desired_build,
        )
        if eligibility.action == "check":
            return
        try:
            networks = capture_networks(target)
            port_bindings = capture_port_bindings(target)
            # Persist the same only-raise floor build_candidate_spec/
            # build_rollback_spec apply to the recreated container's
            # StopTimeout, so the rollback stop's HTTP client timeout
            # (state.stop_timeout_seconds + 10.0) never undercuts the
            # candidate's actual graceful-shutdown window (issue #435).
            stop_timeout_seconds = max(_stop_timeout(target), MINIMUM_STOP_TIMEOUT)
            multi_network_create = await self._multi_network_create()
            # Validate the complete candidate payload, including legacy-MAC
            # portability, before claiming a lease or stopping the live target.
            build_candidate_spec(
                target,
                old_image,
                desired,
                image_ref=self.config.image_ref,
                operation_id="preflight",
                networks=networks,
                port_bindings=port_bindings,
                multi_network_create=multi_network_create,
            )
        except (DockerError, UpdaterError) as exc:
            await self.coordinator.outcome(
                operation="check",
                outcome="failed",
                action_generation=eligibility.action_generation,
                current_digest=old_digest,
                current_build=old_build,
                detail_code=exc.code,
            )
            return
        lease = await self._claim_ready(expected_generation=eligibility.action_generation)
        if lease is None:
            return
        if lease.lease_token is None:  # guarded by _claim_ready
            raise UpdaterError("maintenance_claim_missing")
        if lease.action_generation is None:
            raise UpdaterError("maintenance_generation_missing")
        lease_token = lease.lease_token
        operation_id = uuid.uuid4().hex
        state = UpdateState(
            version=1,
            operation_id=operation_id,
            operation="install",
            stage="prepared",
            lease_token=lease_token,
            action_generation=lease.action_generation,
            target_id=cast(str, target["Id"]),
            old_image_id=old_id,
            old_digest=old_digest,
            old_build=old_build,
            desired_image_id=desired_id,
            desired_digest=desired_digest,
            desired_build=desired_build,
            networks=networks,
            port_bindings=port_bindings,
            stop_timeout_seconds=stop_timeout_seconds,
        )
        self.state.save(state)
        renewed = await self.coordinator.renew(lease_token, phase="installing")
        if not renewed.ready:
            raise UpdaterError("maintenance_drain_lost")
        await self._run_with_lease_keeper(
            state,
            renewed.lease_seconds,
            self._execute_install(state, target, old_image, desired),
        )

    async def _preflight(self) -> tuple[JsonObject, JsonObject, JsonObject]:
        target, old_image = await self._target()
        desired = await self.engine.pull(self.config.image_ref)
        return target, old_image, desired

    async def _run_with_check_heartbeat(
        self,
        action_generation: int,
    ) -> tuple[JsonObject, JsonObject, JsonObject]:
        await self.coordinator.heartbeat(action_generation=action_generation)
        stop = asyncio.Event()
        lost: list[CoordinatorError] = []
        owner = asyncio.current_task()

        async def keep_alive() -> None:
            while True:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=_PROGRESS_INTERVAL_SECONDS)
                    return
                except TimeoutError:
                    try:
                        await self.coordinator.heartbeat(action_generation=action_generation)
                    except CoordinatorError as exc:
                        lost.append(exc)
                        if owner is not None:
                            owner.cancel()
                        return

        task = asyncio.create_task(keep_alive())
        try:
            return await self._preflight()
        except asyncio.CancelledError:
            if lost:
                raise lost[0] from None
            raise
        finally:
            stop.set()
            _ = await task

    async def _execute_install(
        self,
        state: UpdateState,
        target: JsonObject,
        old_image: JsonObject,
        desired: JsonObject,
    ) -> None:
        try:
            await self._install(state, target, old_image, desired)
        except (CoordinatorError, DockerError, UpdaterError) as exc:
            if state.stage == "prepared":
                await self._ack_pre_cutover_failure(state, exc.code)
                return
            if state.stage in {
                "candidate_healthy",
                "old_renamed",
                "candidate_renamed",
                "outcome_acknowledged",
            }:
                # Once the replacement is healthy, a lost rename/outcome
                # response leaves the durable state for retry. It must never
                # trigger rollback of a potentially acknowledged success.
                raise
            await self._rollback(state, detail_code=exc.code)

    async def _run_with_lease_keeper(
        self,
        state: UpdateState,
        lease_seconds: int,
        work: Awaitable[None],
    ) -> None:
        stop = asyncio.Event()
        lost = asyncio.Event()
        owner = asyncio.current_task()
        task = asyncio.create_task(
            self._keep_lease(state, lease_seconds, stop=stop, lost=lost, owner=owner)
        )
        self._lease_stop = stop
        self._lease_task = task
        try:
            await work
        except asyncio.CancelledError:
            if lost.is_set():
                raise UpdaterError("maintenance_drain_lost") from None
            raise
        finally:
            stop.set()
            _ = await task
            if self._lease_task is task:
                self._lease_stop = None
                self._lease_task = None

    async def _keep_lease(
        self,
        state: UpdateState,
        lease_seconds: int,
        *,
        stop: asyncio.Event,
        lost: asyncio.Event,
        owner: asyncio.Task[object] | None,
    ) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + lease_seconds
        delay = max(0.1, min(_PROGRESS_INTERVAL_SECONDS, lease_seconds / 3))
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
                return
            except TimeoutError:
                try:
                    renewed = await self.coordinator.renew(
                        state.lease_token,
                        phase=self._progress_phase(state),
                    )
                except CoordinatorError as exc:
                    if exc.code == "coordinator_conflict" or loop.time() >= deadline:
                        lost.set()
                        if owner is not None:
                            owner.cancel()
                        return
                    delay = 1.0
                    continue
                if not renewed.ready:
                    lost.set()
                    if owner is not None:
                        owner.cancel()
                    return
                lease_seconds = renewed.lease_seconds
                deadline = loop.time() + lease_seconds
                delay = max(0.1, min(_PROGRESS_INTERVAL_SECONDS, lease_seconds / 3))

    async def _stop_lease_keeper(self) -> None:
        stop = self._lease_stop
        task = self._lease_task
        if stop is None or task is None:
            return
        stop.set()
        _ = await task

    async def _claim_ready(
        self,
        *,
        recovery: bool = False,
        expected_generation: int | None = None,
    ) -> LeaseStatus | None:
        lease = (
            await self.coordinator.claim(
                recovery=True,
                expected_generation=expected_generation,
            )
            if recovery
            else await self.coordinator.claim(expected_generation=expected_generation)
        )
        if lease.lease_token is None:
            # Idle-only busy or another updater already owns the drain. This is
            # normal deferral, not a malformed response and not a failed update.
            return None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.config.drain_timeout_seconds
        while not lease.ready:
            if loop.time() >= deadline:
                await self.coordinator.release(lease.lease_token)
                raise UpdaterError("maintenance_drain_timeout")
            await self._sleep(max(0.5, min(5.0, lease.lease_seconds / 3)))
            lease = await self.coordinator.renew(lease.lease_token)
            if lease.lease_token is None:
                raise UpdaterError("maintenance_drain_lost")
        return lease

    async def _ensure_recovery_lease(self, state: UpdateState) -> LeaseStatus | None:
        """Revalidate ownership before any interrupted-stage Docker mutation."""
        try:
            renewed = await self.coordinator.renew(
                state.lease_token,
                phase=self._progress_phase(state),
            )
        except CoordinatorError as exc:
            if exc.code != "coordinator_conflict":
                raise
        else:
            if renewed.ready:
                return renewed
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self.config.drain_timeout_seconds
            while not renewed.ready:
                if loop.time() >= deadline:
                    return None
                await self._sleep(max(0.5, min(5.0, renewed.lease_seconds / 3)))
                renewed = await self.coordinator.renew(
                    state.lease_token,
                    phase=self._progress_phase(state),
                )
            return renewed

        replacement = await self._claim_ready(
            recovery=True,
            expected_generation=state.action_generation,
        )
        if replacement is None or replacement.lease_token is None:
            return None
        state.lease_token = replacement.lease_token
        self.state.save(state)
        return replacement

    @staticmethod
    def _progress_phase(state: UpdateState) -> Literal["installing", "rollback"]:
        return "rollback" if state.stage.startswith("rollback_") else "installing"

    async def _multi_network_create(self) -> bool:
        return await self.engine.api_version() >= (1, 44)

    async def _target(self) -> tuple[JsonObject, JsonObject]:
        try:
            container = await self.engine.inspect_container(self.config.container_name)
        except DockerNotFound as exc:
            raise UpdaterError("configured_target_missing") from exc
        if _container_name(container) != self.config.container_name:
            raise UpdaterError("configured_target_mismatch")
        labels = _config_labels(container)
        if labels.get(TARGET_LABEL) != "true":
            raise UpdaterError("configured_target_label_missing")
        if labels.get(IMAGE_REF_LABEL) != self.config.image_ref:
            raise UpdaterError("configured_image_label_mismatch")
        old_image = await self.engine.inspect_image(_container_image_id(container))
        # A health gate is mandatory before any destructive operation; otherwise
        # there is no objective success/rollback decision.
        config = container.get("Config")
        if not isinstance(config, dict) or not enabled_healthcheck(
            cast(JsonObject, config).get("Healthcheck")
        ):
            raise UpdaterError("target_healthcheck_missing")
        identifier = container.get("Id")
        if not isinstance(identifier, str) or not identifier:
            raise UpdaterError("target_id_missing")
        return container, old_image

    async def _install(
        self,
        state: UpdateState,
        target: JsonObject,
        old_image: JsonObject,
        desired: JsonObject,
    ) -> None:
        self._stage(state, "stop_requested")
        await self.engine.stop_container(
            state.target_id,
            request_timeout=state.stop_timeout_seconds + 10.0,
        )
        self._stage(state, "old_stopped")
        for network in state.networks:
            await self.engine.disconnect_network(network, state.target_id)
        self._stage(state, "old_disconnected")

        spec, created_networks = build_candidate_spec(
            target,
            old_image,
            desired,
            image_ref=self.config.image_ref,
            operation_id=state.operation_id,
            networks=state.networks,
            port_bindings=state.port_bindings,
            multi_network_create=await self._multi_network_create(),
        )
        candidate_name = f"{self.config.container_name}-candidate-{state.operation_id[:12]}"
        state.candidate_id = await self.engine.create_container(candidate_name, spec)
        self._stage(state, "candidate_created")
        for network, endpoint in remaining_networks(state.networks, created_networks):
            await self.engine.connect_network(network, state.candidate_id, endpoint)
        await self.engine.start_container(state.candidate_id)
        self._stage(state, "candidate_started")
        await self.engine.wait_healthy(
            state.candidate_id, timeout=self.config.health_timeout_seconds
        )
        self._stage(state, "candidate_healthy")
        await self._finish_success(state)

    async def _finish_success(self, state: UpdateState) -> None:
        await self._validate_candidate(state)
        if state.candidate_id is None:  # guarded by _validate_candidate
            raise UpdaterError("candidate_missing_during_recovery")
        await self.engine.wait_healthy(
            state.candidate_id, timeout=self.config.health_timeout_seconds
        )
        if state.stage not in {"old_renamed", "candidate_renamed", "outcome_acknowledged"}:
            await self._ensure_name(
                state.target_id,
                expected=f"{self.config.container_name}-previous-{state.operation_id[:12]}",
                allowed_current=self.config.container_name,
            )
            self._stage(state, "old_renamed")
        if state.stage == "old_renamed":
            await self._ensure_name(
                state.candidate_id,
                expected=self.config.container_name,
                allowed_current=f"{self.config.container_name}-candidate-{state.operation_id[:12]}",
            )
            self._stage(state, "candidate_renamed")
        if state.stage == "candidate_renamed":
            await self._stop_lease_keeper()
            await self.coordinator.outcome(
                operation="install",
                outcome="succeeded",
                action_generation=state.action_generation,
                lease_token=state.lease_token,
                current_digest=state.desired_digest,
                available_digest=state.desired_digest,
                current_build=state.desired_build,
                available_build=state.desired_build,
                from_build=state.old_build,
                to_build=state.desired_build,
            )
            self._stage(state, "outcome_acknowledged")
        await self._cleanup_success(state)

    async def _rollback(
        self,
        state: UpdateState,
        *,
        detail_code: str,
        acknowledge: bool = True,
    ) -> None:
        if state.detail_code is None:
            state.detail_code = detail_code
            self.state.save(state)
        if not state.stage.startswith("rollback_"):
            self._stage(state, "rollback_requested")
        try:
            progress = await self.coordinator.renew(state.lease_token, phase="rollback")
        except CoordinatorError as exc:
            if exc.code != "coordinator_unavailable":
                raise
        else:
            if not progress.ready:
                raise UpdaterError("maintenance_drain_lost")
        if state.candidate_id is None:
            state.candidate_id = await self._find_operation_container(state, "candidate")
            if state.candidate_id is not None:
                self.state.save(state)
        if state.candidate_id is not None and await self.engine.exists(state.candidate_id):
            await self._validate_operation_container(
                state,
                state.candidate_id,
                role="candidate",
                expected_image=state.desired_image_id,
                allowed_names={
                    self.config.container_name,
                    f"{self.config.container_name}-candidate-{state.operation_id[:12]}",
                },
            )
            with suppress(DockerError):
                await self.engine.stop_container(
                    state.candidate_id,
                    request_timeout=state.stop_timeout_seconds + 10.0,
                )
            await self.engine.remove_container(state.candidate_id, force=True)
        await self._validate_previous(state)
        with suppress(DockerError):
            await self.engine.stop_container(
                state.target_id,
                request_timeout=state.stop_timeout_seconds + 10.0,
            )
        await self._ensure_name(
            state.target_id,
            expected=f"{self.config.container_name}-previous-{state.operation_id[:12]}",
            allowed_current=self.config.container_name,
        )
        previous = await self._validate_previous(
            state,
            allowed_names={f"{self.config.container_name}-previous-{state.operation_id[:12]}"},
        )
        # Ensure the retained container owns no endpoint while the rollback clone
        # takes its aliases/static addresses. Already-disconnected is harmlessly
        # ignored because recovery may enter at any checkpoint.
        for network in state.networks:
            with suppress(DockerError):
                await self.engine.disconnect_network(network, state.target_id)

        if state.rollback_id is None:
            state.rollback_id = await self._find_operation_container(state, "rollback")
            if state.rollback_id is not None:
                self._stage(state, "rollback_created")
        if state.rollback_id is not None and not await self.engine.exists(state.rollback_id):
            state.rollback_id = None
            self._stage(state, "rollback_requested")
        if state.rollback_id is not None:
            await self._validate_rollback(state)
            if state.stage == "rollback_healthy":
                try:
                    await self.engine.wait_healthy(
                        state.rollback_id,
                        timeout=self.config.health_timeout_seconds,
                    )
                except DockerError:
                    with suppress(DockerError):
                        await self.engine.stop_container(
                            state.rollback_id,
                            request_timeout=state.stop_timeout_seconds + 10.0,
                        )
                    await self.engine.remove_container(state.rollback_id, force=True)
                    state.rollback_id = None
                    self._stage(state, "rollback_requested")
        if state.rollback_id is None:
            old_image = await self.engine.inspect_image(state.old_image_id)
            spec, _created_networks = build_rollback_spec(
                previous,
                old_image,
                image_ref=self.config.image_ref,
                operation_id=state.operation_id,
                networks=state.networks,
                port_bindings=state.port_bindings,
                multi_network_create=await self._multi_network_create(),
            )
            state.rollback_id = await self.engine.create_container(self.config.container_name, spec)
            self._stage(state, "rollback_created")
        if state.stage == "rollback_created":
            await self._ensure_rollback_networks(state)
            self._stage(state, "rollback_networked")
        if state.stage == "rollback_networked":
            await self.engine.start_container(state.rollback_id)
            self._stage(state, "rollback_started")
        await self.engine.wait_healthy(
            state.rollback_id, timeout=self.config.health_timeout_seconds
        )
        self._stage(state, "rollback_healthy")
        if acknowledge:
            await self._finish_rollback_outcome(state)

    async def _finish_rollback_outcome(self, state: UpdateState) -> None:
        rollback = await self._validate_rollback(state)
        self._validate_network_fidelity(rollback, state)
        if state.rollback_id is None:  # guarded by _validate_rollback
            raise UpdaterError("rollback_missing_during_recovery")
        await self.engine.wait_healthy(
            state.rollback_id,
            timeout=self.config.health_timeout_seconds,
        )
        await self._stop_lease_keeper()
        await self.coordinator.outcome(
            operation="install",
            outcome="rolled_back",
            action_generation=state.action_generation,
            lease_token=state.lease_token,
            current_digest=state.old_digest,
            available_digest=state.desired_digest,
            current_build=state.old_build,
            available_build=state.desired_build,
            from_build=state.old_build,
            to_build=state.desired_build,
            detail_code=state.detail_code or "update_interrupted",
        )
        self._stage(state, "rollback_acknowledged")
        await self._cleanup_rollback(state)

    async def _find_operation_container(self, state: UpdateState, role: str) -> str | None:
        matches = await self.engine.containers_by_labels(
            {OPERATION_LABEL: state.operation_id, ROLE_LABEL: role}
        )
        if not matches:
            return None
        if len(matches) != 1:
            raise UpdaterError("ambiguous_operation_containers")
        identifier = matches[0].get("Id")
        if not isinstance(identifier, str) or not identifier:
            raise UpdaterError("operation_container_id_missing")
        return identifier

    async def _validate_operation_container(
        self,
        state: UpdateState,
        identifier: str,
        *,
        role: str,
        expected_image: str,
        allowed_names: set[str],
    ) -> JsonObject:
        try:
            container = await self.engine.inspect_container(identifier)
        except DockerNotFound as exc:
            raise UpdaterError(f"{role}_missing_during_recovery") from exc
        if container.get("Id") != identifier or _container_image_id(container) != expected_image:
            raise UpdaterError(f"{role}_identity_mismatch")
        if _container_name(container) not in allowed_names:
            raise UpdaterError(f"{role}_name_mismatch")
        labels = _config_labels(container)
        required = {
            TARGET_LABEL: "true",
            IMAGE_REF_LABEL: self.config.image_ref,
            OPERATION_LABEL: state.operation_id,
            ROLE_LABEL: role,
        }
        if any(labels.get(key) != value for key, value in required.items()):
            raise UpdaterError(f"{role}_label_mismatch")
        return container

    async def _validate_candidate(self, state: UpdateState) -> JsonObject:
        if state.candidate_id is None:
            raise UpdaterError("candidate_missing_during_recovery")
        names = {self.config.container_name}
        if state.stage not in {"candidate_renamed", "outcome_acknowledged"}:
            names.add(f"{self.config.container_name}-candidate-{state.operation_id[:12]}")
        return await self._validate_operation_container(
            state,
            state.candidate_id,
            role="candidate",
            expected_image=state.desired_image_id,
            allowed_names=names,
        )

    async def _validate_rollback(self, state: UpdateState) -> JsonObject:
        if state.rollback_id is None:
            raise UpdaterError("rollback_missing_during_recovery")
        return await self._validate_operation_container(
            state,
            state.rollback_id,
            role="rollback",
            expected_image=state.old_image_id,
            allowed_names={self.config.container_name},
        )

    async def _validate_previous(
        self,
        state: UpdateState,
        *,
        allowed_names: set[str] | None = None,
    ) -> JsonObject:
        try:
            container = await self.engine.inspect_container(state.target_id)
        except DockerNotFound as exc:
            raise UpdaterError("previous_container_missing") from exc
        if (
            container.get("Id") != state.target_id
            or _container_image_id(container) != state.old_image_id
        ):
            raise UpdaterError("previous_container_identity_mismatch")
        names = allowed_names or {
            self.config.container_name,
            f"{self.config.container_name}-previous-{state.operation_id[:12]}",
        }
        if _container_name(container) not in names:
            raise UpdaterError("previous_container_name_mismatch")
        labels = _config_labels(container)
        if (
            labels.get(TARGET_LABEL) != "true"
            or labels.get(IMAGE_REF_LABEL) != self.config.image_ref
        ):
            raise UpdaterError("previous_container_label_mismatch")
        return container

    async def _ensure_rollback_networks(self, state: UpdateState) -> None:
        rollback = await self._validate_rollback(state)
        settings = rollback.get("NetworkSettings")
        if not isinstance(settings, dict):
            raise UpdaterError("rollback_networks_missing")
        current = cast(JsonObject, settings).get("Networks")
        if not isinstance(current, dict):
            raise UpdaterError("rollback_networks_missing")
        current_keys = cast(dict[object, object], current)
        if not all(isinstance(name, str) for name in current_keys):
            raise UpdaterError("rollback_networks_invalid")
        current_names = {cast(str, name) for name in current_keys}
        expected_names = set(state.networks)
        if current_names - expected_names:
            raise UpdaterError("rollback_network_mismatch")
        if state.rollback_id is None:  # guarded by _validate_rollback
            raise UpdaterError("rollback_missing_during_recovery")
        for name in state.networks:
            if name not in current_names:
                await self.engine.connect_network(name, state.rollback_id, state.networks[name])

    @staticmethod
    def _validate_network_fidelity(container: JsonObject, state: UpdateState) -> None:
        settings = container.get("NetworkSettings")
        if not isinstance(settings, dict):
            raise UpdaterError("rollback_networks_missing")
        current_value = cast(JsonObject, settings).get("Networks")
        if not isinstance(current_value, dict):
            raise UpdaterError("rollback_networks_missing")
        current = cast(dict[object, object], current_value)
        if not all(
            isinstance(name, str) and isinstance(value, dict) for name, value in current.items()
        ):
            raise UpdaterError("rollback_networks_invalid")
        if set(cast(dict[str, object], current)) != set(state.networks):
            raise UpdaterError("rollback_network_mismatch")
        for name, expected in state.networks.items():
            expected_mac = expected.get("MacAddress")
            if expected_mac is not None:
                actual = cast(JsonObject, current[name])
                if actual.get("MacAddress") != expected_mac:
                    raise UpdaterError("rollback_network_mac_mismatch")

    async def _cleanup_success(self, state: UpdateState) -> None:
        await self._validate_candidate(state)
        if state.candidate_id is None:  # guarded by _validate_candidate
            raise UpdaterError("candidate_missing_during_recovery")
        await self.engine.wait_healthy(
            state.candidate_id,
            timeout=self.config.health_timeout_seconds,
        )
        if await self.engine.exists(state.target_id):
            await self._validate_previous(
                state,
                allowed_names={f"{self.config.container_name}-previous-{state.operation_id[:12]}"},
            )
            await self.engine.remove_container(state.target_id)
        self.state.clear()

    async def _cleanup_rollback(self, state: UpdateState) -> None:
        rollback = await self._validate_rollback(state)
        self._validate_network_fidelity(rollback, state)
        if state.rollback_id is None:  # guarded by _validate_rollback
            raise UpdaterError("rollback_missing_during_recovery")
        await self.engine.wait_healthy(
            state.rollback_id,
            timeout=self.config.health_timeout_seconds,
        )
        if await self.engine.exists(state.target_id):
            await self._validate_previous(
                state,
                allowed_names={f"{self.config.container_name}-previous-{state.operation_id[:12]}"},
            )
            await self.engine.remove_container(state.target_id)
        self.state.clear()

    async def _ensure_name(self, identifier: str, *, expected: str, allowed_current: str) -> None:
        current = _container_name(await self.engine.inspect_container(identifier))
        if current == expected:
            return
        if current != allowed_current:
            raise UpdaterError("operation_container_name_mismatch")
        await self.engine.rename_container(identifier, expected)

    async def _recover(self, state: UpdateState) -> None:
        if state.stage == "outcome_acknowledged":
            await self._cleanup_success(state)
            return
        if state.stage == "rollback_acknowledged":
            await self._cleanup_rollback(state)
            return
        if state.stage.startswith("rollback_"):
            await self._rollback(state, detail_code="update_interrupted")
            return
        if state.candidate_id is not None and await self.engine.exists(state.candidate_id):
            await self._validate_candidate(state)
            try:
                await self.engine.wait_healthy(
                    state.candidate_id, timeout=self.config.health_timeout_seconds
                )
            except DockerError as exc:
                await self._rollback(state, detail_code=exc.code)
                return
            await self._finish_success(state)
            return
        if state.stage == "prepared":
            await self._ack_pre_cutover_failure(state, "update_interrupted")
            return
        await self._rollback(state, detail_code="update_interrupted")

    async def _ack_pre_cutover_failure(self, state: UpdateState, detail_code: str) -> None:
        if state.detail_code is None:
            state.detail_code = detail_code
            self.state.save(state)
        await self._stop_lease_keeper()
        await self.coordinator.outcome(
            operation="install",
            outcome="failed",
            action_generation=state.action_generation,
            lease_token=state.lease_token,
            current_digest=state.old_digest,
            available_digest=state.desired_digest,
            current_build=state.old_build,
            available_build=state.desired_build,
            from_build=state.old_build,
            to_build=state.desired_build,
            detail_code=state.detail_code,
        )
        self.state.clear()

    def _stage(self, state: UpdateState, stage: UpdateStage) -> None:
        state.stage = stage
        self.state.save(state)
