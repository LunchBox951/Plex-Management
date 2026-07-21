"""Capture and faithfully reconstruct the one allowlisted app container."""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Final, cast

from plex_manager.updater.config import (
    IMAGE_REF_LABEL,
    OPERATION_LABEL,
    ROLE_LABEL,
    TARGET_LABEL,
)
from plex_manager.updater.engine import DockerError, JsonObject, image_id

_IMAGE_BACKED_FIELDS = (
    "User",
    "WorkingDir",
    "Entrypoint",
    "Cmd",
    "Healthcheck",
    "ExposedPorts",
    "Volumes",
    "StopSignal",
    "Shell",
)
_DROP_CONFIG_FIELDS = frozenset({"ArgsEscaped", "OnBuild"})
_DROP_HOST_CONFIG_FIELDS = frozenset({"ContainerIDFile"})
_IMAGE_OWNED_ENV = frozenset({"PLEX_MANAGER_BUILD_ID"})
# Keep recreated installs aligned with compose's 75s stop_grace_period: the
# 60s shutdown task bound, Uvicorn drain, and a safety margin. Only raise an
# existing value so an operator's intentionally longer grace period survives.
# Public so runner.py can floor its persisted stop_timeout_seconds against the
# exact same value the candidate/rollback spec was migrated to (issue #435) —
# no duplicated literal.
MINIMUM_STOP_TIMEOUT: Final = 75
_MAC_RE = re.compile(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}")


def _object(value: object, code: str) -> JsonObject:
    if not isinstance(value, dict):
        raise DockerError(code)
    return cast(JsonObject, value)


def _env(values: object) -> tuple[list[str], dict[str, str]]:
    ordered: list[str] = []
    mapping: dict[str, str] = {}
    if values is None:
        return ordered, mapping
    if not isinstance(values, list):
        raise DockerError("docker_invalid_environment")
    for item in cast(list[object], values):
        if not isinstance(item, str) or "=" not in item:
            raise DockerError("docker_invalid_environment")
        key, _, value = item.partition("=")
        if not key or any(ch in key for ch in "\r\n\0"):
            raise DockerError("docker_invalid_environment")
        if key not in mapping:
            ordered.append(key)
        mapping[key] = value
    return ordered, mapping


def _candidate_environment(current: JsonObject, new_image: JsonObject) -> list[str]:
    # Inspect exposes only the effective environment, not whether an operator
    # explicitly supplied a value equal to an image default. Preserve every
    # effective value under that ambiguity. Only fixed image-owned metadata is
    # allowed to advance with the new image.
    current_order, active = _env(current.get("Env"))
    new_order, desired = _env(new_image.get("Env"))
    merged_order = list(new_order)
    for key in current_order:
        if key not in _IMAGE_OWNED_ENV and key not in merged_order:
            merged_order.append(key)
    desired.update({key: value for key, value in active.items() if key not in _IMAGE_OWNED_ENV})
    return [f"{key}={desired[key]}" for key in merged_order]


def _labels(
    current: JsonObject,
    old_image: JsonObject,
    new_image: JsonObject,
    *,
    image_ref: str,
    operation_id: str,
    role: str,
    target_image_id: str,
) -> dict[str, str]:
    active = _object(current.get("Labels") or {}, "docker_invalid_labels")
    old = _object(old_image.get("Labels") or {}, "docker_invalid_labels")
    new = _object(new_image.get("Labels") or {}, "docker_invalid_labels")
    if not all(isinstance(value, str) for value in active.values()):
        raise DockerError("docker_invalid_labels")
    runtime = {key: value for key, value in active.items() if old.get(key) != value}
    labels = {key: value for key, value in new.items() if isinstance(value, str)}
    labels.update(cast(dict[str, str], runtime))
    labels.pop("com.docker.compose.replace", None)
    if "com.docker.compose.image" in labels:
        labels["com.docker.compose.image"] = target_image_id
    labels[TARGET_LABEL] = "true"
    labels[IMAGE_REF_LABEL] = image_ref
    labels[OPERATION_LABEL] = operation_id
    labels[ROLE_LABEL] = role
    return labels


def capture_networks(container: JsonObject) -> dict[str, JsonObject]:
    settings = _object(container.get("NetworkSettings"), "docker_networks_missing")
    networks = _object(settings.get("Networks"), "docker_networks_missing")
    captured: dict[str, JsonObject] = {}
    for name, raw in networks.items():
        if not isinstance(raw, dict):
            raise DockerError("docker_networks_invalid")
        endpoint = cast(JsonObject, raw)
        requested: JsonObject = {}
        for key in ("Aliases", "Links", "DriverOpts", "IPAMConfig", "GwPriority"):
            value = endpoint.get(key)
            if value not in (None, [], {}, "", 0):
                requested[key] = deepcopy(value)
        mac = endpoint.get("MacAddress")
        if mac not in (None, ""):
            if not isinstance(mac, str) or _MAC_RE.fullmatch(mac) is None:
                raise DockerError("docker_invalid_network_mac")
            requested["MacAddress"] = mac
        captured[name] = requested
    return captured


def _host_config(container: JsonObject) -> JsonObject:
    value = deepcopy(_object(container.get("HostConfig"), "docker_host_config_missing"))
    if value.get("AutoRemove") is True:
        raise DockerError("target_auto_remove_unsupported")
    for key in _DROP_HOST_CONFIG_FIELDS:
        value.pop(key, None)
    _preserve_anonymous_volumes(container, value)
    value["AutoRemove"] = False
    return value


def _mount_destinations(host_config: JsonObject) -> set[str]:
    destinations: set[str] = set()
    binds = host_config.get("Binds")
    if isinstance(binds, list):
        for raw in cast(list[object], binds):
            if isinstance(raw, str):
                parts = raw.split(":")
                if len(parts) >= 2:
                    destinations.add(parts[1])
    mounts = host_config.get("Mounts")
    if isinstance(mounts, list):
        for raw in cast(list[object], mounts):
            if isinstance(raw, dict):
                target = cast(JsonObject, raw).get("Target")
                if isinstance(target, str):
                    destinations.add(target)
    tmpfs = host_config.get("Tmpfs")
    if isinstance(tmpfs, dict):
        destinations.update(
            key for key in cast(dict[object, object], tmpfs) if isinstance(key, str)
        )
    return destinations


def _preserve_anonymous_volumes(container: JsonObject, host_config: JsonObject) -> None:
    """Materialize anonymous/image-declared volumes by their Engine-assigned name."""
    raw_mounts = container.get("Mounts")
    if not isinstance(raw_mounts, list):
        return
    destinations = _mount_destinations(host_config)
    raw_existing = deepcopy(host_config.get("Mounts"))
    if raw_existing is None:
        mounts: list[object] = []
    elif isinstance(raw_existing, list):
        mounts = cast(list[object], raw_existing)
    else:
        raise DockerError("docker_invalid_mounts")
    for raw in cast(list[object], raw_mounts):
        if not isinstance(raw, dict):
            raise DockerError("docker_invalid_mounts")
        mount = cast(JsonObject, raw)
        if mount.get("Type") != "volume":
            continue
        destination = mount.get("Destination")
        source = mount.get("Name") or mount.get("Source")
        if not isinstance(destination, str) or not isinstance(source, str):
            raise DockerError("docker_invalid_mounts")
        if destination in destinations:
            continue
        mounts.append(
            {
                "Type": "volume",
                "Source": source,
                "Target": destination,
                "ReadOnly": mount.get("RW") is False,
            }
        )
        destinations.add(destination)
    if mounts:
        host_config["Mounts"] = mounts


def capture_port_bindings(container: JsonObject) -> JsonObject:
    """Resolve Docker-assigned host ports before the target is stopped."""
    host = _object(container.get("HostConfig"), "docker_host_config_missing")
    raw_requested = deepcopy(host.get("PortBindings"))
    if raw_requested is None:
        requested: dict[str, object] = {}
    elif not isinstance(raw_requested, dict):
        raise DockerError("docker_invalid_port_bindings")
    else:
        requested = cast(dict[str, object], raw_requested)
    settings = _object(container.get("NetworkSettings"), "docker_networks_missing")
    effective = settings.get("Ports")
    if effective is not None and not isinstance(effective, dict):
        raise DockerError("docker_invalid_port_bindings")
    effective_values = cast(dict[str, object], effective) if isinstance(effective, dict) else {}
    publish_all = host.get("PublishAllPorts") is True

    def resolved_bindings(container_port: str) -> list[JsonObject]:
        resolved = effective_values.get(container_port)
        if not isinstance(resolved, list) or not resolved:
            raise DockerError("docker_assigned_port_missing")
        normalized: list[JsonObject] = []
        for item in cast(list[object], resolved):
            if not isinstance(item, dict):
                raise DockerError("docker_invalid_port_bindings")
            value = cast(JsonObject, item)
            host_ip = value.get("HostIp")
            host_port = value.get("HostPort")
            if (
                not isinstance(host_ip, str)
                or not isinstance(host_port, str)
                or not host_port
                or host_port == "0"
            ):
                raise DockerError("docker_assigned_port_missing")
            normalized.append({"HostIp": host_ip, "HostPort": host_port})
        return normalized

    for container_port, bindings in list(requested.items()):
        if bindings is None:
            if publish_all:
                requested[container_port] = resolved_bindings(container_port)
            continue
        if not isinstance(bindings, list):
            raise DockerError("docker_invalid_port_bindings")
        binding_items = cast(list[object], bindings)
        if not all(isinstance(item, dict) for item in binding_items):
            raise DockerError("docker_invalid_port_bindings")
        needs_resolution = any(
            cast(JsonObject, item).get("HostPort") in {None, "", "0"} for item in binding_items
        )
        if not needs_resolution:
            continue
        requested[container_port] = resolved_bindings(container_port)
    if publish_all:
        for container_port, bindings in effective_values.items():
            if bindings is not None and container_port not in requested:
                requested[container_port] = resolved_bindings(container_port)
    return cast(JsonObject, requested)


def enabled_healthcheck(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    test = cast(JsonObject, value).get("Test")
    items = cast(list[object], test) if isinstance(test, list) else []
    return (
        len(items) >= 2
        and items[0] in {"CMD", "CMD-SHELL"}
        and all(isinstance(item, str) for item in items)
    )


def _primary_network(host_config: JsonObject, networks: dict[str, JsonObject]) -> str | None:
    mode = host_config.get("NetworkMode")
    if not isinstance(mode, str) or mode in {"host", "none"}:
        return None
    if mode in {"", "default", "bridge"}:
        return "bridge" if "bridge" in networks else None
    return mode if mode in networks else None


def _base_config(container: JsonObject) -> JsonObject:
    current = deepcopy(_object(container.get("Config"), "docker_container_config_missing"))
    for key in _DROP_CONFIG_FIELDS:
        current.pop(key, None)
    identifier = container.get("Id")
    if isinstance(identifier, str) and current.get("Hostname") == identifier[:12]:
        current.pop("Hostname", None)
    timeout = current.get("StopTimeout")
    current["StopTimeout"] = max(timeout if isinstance(timeout, int) else 10, MINIMUM_STOP_TIMEOUT)
    return current


def _with_networking(
    config: JsonObject,
    host_config: JsonObject,
    networks: dict[str, JsonObject],
    *,
    multi_network_create: bool,
) -> tuple[JsonObject, frozenset[str]]:
    primary = _primary_network(host_config, networks)
    config["HostConfig"] = host_config
    if multi_network_create and networks:
        config["NetworkingConfig"] = {"EndpointsConfig": deepcopy(networks)}
        return config, frozenset(networks)
    if primary is not None:
        endpoint = networks[primary]
        mac = endpoint.get("MacAddress")
        if isinstance(mac, str):
            config["MacAddress"] = mac
        config["NetworkingConfig"] = {"EndpointsConfig": {primary: endpoint}}
    unsupported_macs = [
        name
        for name, endpoint in networks.items()
        if name != primary and endpoint.get("MacAddress")
    ]
    if unsupported_macs:
        raise DockerError("docker_secondary_mac_unsupported")
    return config, frozenset({primary}) if primary is not None else frozenset()


def build_candidate_spec(
    container: JsonObject,
    old_image: JsonObject,
    new_image: JsonObject,
    *,
    image_ref: str,
    operation_id: str,
    networks: dict[str, JsonObject],
    port_bindings: JsonObject | None = None,
    multi_network_create: bool = False,
) -> tuple[JsonObject, frozenset[str]]:
    """Three-way merge old image, active runtime overrides, and new image defaults."""
    current = _base_config(container)
    old_config = _object(old_image.get("Config") or {}, "docker_old_image_config_missing")
    new_config = _object(new_image.get("Config") or {}, "docker_new_image_config_missing")
    target_image_id = image_id(new_image)
    current["Image"] = target_image_id
    current["Env"] = _candidate_environment(current, new_config)
    current["Labels"] = _labels(
        current,
        old_config,
        new_config,
        image_ref=image_ref,
        operation_id=operation_id,
        role="candidate",
        target_image_id=target_image_id,
    )
    for field in _IMAGE_BACKED_FIELDS:
        if current.get(field) == old_config.get(field):
            if field in new_config:
                current[field] = deepcopy(new_config[field])
            else:
                current.pop(field, None)
    if not enabled_healthcheck(current.get("Healthcheck")):
        raise DockerError("target_healthcheck_missing")
    host_config = _host_config(container)
    if port_bindings is not None:
        host_config["PortBindings"] = deepcopy(port_bindings)
    return _with_networking(
        current,
        host_config,
        networks,
        multi_network_create=multi_network_create,
    )


def build_rollback_spec(
    container: JsonObject,
    old_image: JsonObject,
    *,
    image_ref: str,
    operation_id: str,
    networks: dict[str, JsonObject],
    port_bindings: JsonObject | None = None,
    multi_network_create: bool = False,
) -> tuple[JsonObject, frozenset[str]]:
    """Recreate the previous bytes/config but bypass their now-behind Alembic graph."""
    current = _base_config(container)
    old_config = _object(old_image.get("Config") or {}, "docker_old_image_config_missing")
    old_image_id = image_id(old_image)
    current["Image"] = old_image_id
    # The candidate may already have stamped a newer revision. Starting the
    # retained container's normal entrypoint would make N-1 Alembic reject that
    # unknown revision before the app can serve. Schema changes are required to
    # remain N-1 compatible; rollback starts the old app directly and never runs
    # an Alembic downgrade.
    current["Entrypoint"] = ["python", "-m", "plex_manager"]
    current["Cmd"] = []
    current["Labels"] = _labels(
        current,
        old_config,
        old_config,
        image_ref=image_ref,
        operation_id=operation_id,
        role="rollback",
        target_image_id=old_image_id,
    )
    if not enabled_healthcheck(current.get("Healthcheck")):
        raise DockerError("target_healthcheck_missing")
    host_config = _host_config(container)
    if port_bindings is not None:
        host_config["PortBindings"] = deepcopy(port_bindings)
    return _with_networking(
        current,
        host_config,
        networks,
        multi_network_create=multi_network_create,
    )


def remaining_networks(
    networks: dict[str, JsonObject], created: frozenset[str]
) -> list[tuple[str, JsonObject]]:
    return [(name, endpoint) for name, endpoint in networks.items() if name not in created]
