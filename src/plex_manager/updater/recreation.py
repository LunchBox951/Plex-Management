"""Capture and faithfully reconstruct the one allowlisted app container."""

from __future__ import annotations

from copy import deepcopy
from typing import cast

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


def _candidate_environment(
    current: JsonObject, old_image: JsonObject, new_image: JsonObject
) -> list[str]:
    old_order, old = _env(old_image.get("Env"))
    del old_order
    current_order, active = _env(current.get("Env"))
    new_order, desired = _env(new_image.get("Env"))

    # Container inspect exposes the resolved environment. Recover only runtime
    # overrides by subtracting the old image defaults; otherwise the old
    # PLEX_MANAGER_BUILD_ID would be pinned forever.
    overrides = {key: value for key, value in active.items() if key not in old or old[key] != value}
    merged_order = list(new_order)
    for key in current_order:
        if key in overrides and key not in merged_order:
            merged_order.append(key)
    desired.update(overrides)
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
        captured[name] = requested
    return captured


def _host_config(container: JsonObject) -> JsonObject:
    value = deepcopy(_object(container.get("HostConfig"), "docker_host_config_missing"))
    if value.get("AutoRemove") is True:
        raise DockerError("target_auto_remove_unsupported")
    for key in _DROP_HOST_CONFIG_FIELDS:
        value.pop(key, None)
    value["AutoRemove"] = False
    return value


def _primary_network(host_config: JsonObject, networks: dict[str, JsonObject]) -> str | None:
    mode = host_config.get("NetworkMode")
    if not isinstance(mode, str) or mode in {"", "default", "bridge", "host", "none"}:
        return None
    return mode if mode in networks else None


def _base_config(container: JsonObject) -> JsonObject:
    current = deepcopy(_object(container.get("Config"), "docker_container_config_missing"))
    for key in _DROP_CONFIG_FIELDS:
        current.pop(key, None)
    identifier = container.get("Id")
    if isinstance(identifier, str) and current.get("Hostname") == identifier[:12]:
        current.pop("Hostname", None)
    return current


def _with_networking(
    config: JsonObject, host_config: JsonObject, networks: dict[str, JsonObject]
) -> tuple[JsonObject, str | None]:
    primary = _primary_network(host_config, networks)
    config["HostConfig"] = host_config
    if primary is not None:
        config["NetworkingConfig"] = {"EndpointsConfig": {primary: networks[primary]}}
    return config, primary


def build_candidate_spec(
    container: JsonObject,
    old_image: JsonObject,
    new_image: JsonObject,
    *,
    image_ref: str,
    operation_id: str,
    networks: dict[str, JsonObject],
) -> tuple[JsonObject, str | None]:
    """Three-way merge old image, active runtime overrides, and new image defaults."""
    current = _base_config(container)
    old_config = _object(old_image.get("Config") or {}, "docker_old_image_config_missing")
    new_config = _object(new_image.get("Config") or {}, "docker_new_image_config_missing")
    target_image_id = image_id(new_image)
    current["Image"] = target_image_id
    current["Env"] = _candidate_environment(current, old_config, new_config)
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
    if not isinstance(current.get("Healthcheck"), dict):
        raise DockerError("target_healthcheck_missing")
    return _with_networking(current, _host_config(container), networks)


def build_rollback_spec(
    container: JsonObject,
    old_image: JsonObject,
    *,
    image_ref: str,
    operation_id: str,
    networks: dict[str, JsonObject],
) -> tuple[JsonObject, str | None]:
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
    if not isinstance(current.get("Healthcheck"), dict):
        raise DockerError("target_healthcheck_missing")
    return _with_networking(current, _host_config(container), networks)


def remaining_networks(
    networks: dict[str, JsonObject], primary: str | None
) -> list[tuple[str, JsonObject]]:
    return [(name, endpoint) for name, endpoint in networks.items() if name != primary]
