"""Three-way Docker container recreation fidelity and rollback behavior."""

from __future__ import annotations

from copy import deepcopy

import pytest

from plex_manager.updater.config import (
    IMAGE_REF_LABEL,
    OPERATION_LABEL,
    ROLE_LABEL,
    TARGET_LABEL,
)
from plex_manager.updater.engine import DockerError
from plex_manager.updater.recreation import (
    build_candidate_spec,
    build_rollback_spec,
    capture_networks,
    capture_port_bindings,
    remaining_networks,
)

IMAGE_REF = "ghcr.io/lunchbox951/plex-manager:stable"
OLD_ID = "sha256:" + "a" * 64
NEW_ID = "sha256:" + "b" * 64


def _old_image() -> dict[str, object]:
    return {
        "Id": OLD_ID,
        "Config": {
            "Env": [
                "PATH=/usr/local/bin",
                "PLEX_MANAGER_BUILD_ID=build-old",
                "IMAGE_DEFAULT=old-default",
                "OVERRIDDEN=old-default",
            ],
            "Labels": {"org.example.release": "old"},
            "Entrypoint": ["/app/docker-entrypoint.sh"],
            "Cmd": ["python", "-m", "plex_manager"],
            "Healthcheck": {"Test": ["CMD", "old-health"]},
            "ExposedPorts": {"8000/tcp": {}},
            "Volumes": {"/app/data": {}},
            "User": "1000:1000",
            "WorkingDir": "/app",
        },
    }


def _new_image() -> dict[str, object]:
    return {
        "Id": NEW_ID,
        "Config": {
            "Env": [
                "PATH=/usr/local/sbin:/usr/local/bin",
                "PLEX_MANAGER_BUILD_ID=build-new",
                "IMAGE_DEFAULT=new-default",
                "NEW_DEFAULT=enabled",
                "OVERRIDDEN=new-default",
            ],
            "Labels": {"org.example.release": "new", "org.example.new-label": "present"},
            "Entrypoint": ["/app/new-entrypoint.sh"],
            "Cmd": ["serve"],
            "Healthcheck": {"Test": ["CMD", "new-health"]},
            "ExposedPorts": {"8000/tcp": {}, "9000/tcp": {}},
            "Volumes": {"/app/data": {}, "/app/cache": {}},
            "User": "1000:1000",
            "WorkingDir": "/app",
        },
    }


def _container() -> dict[str, object]:
    old_config = deepcopy(_old_image()["Config"])
    assert isinstance(old_config, dict)
    old_config.update(
        {
            "Hostname": "old-containr",
            "Image": IMAGE_REF,
            "Env": [
                "PATH=/usr/local/bin",
                "PLEX_MANAGER_BUILD_ID=build-old",
                "IMAGE_DEFAULT=old-default",
                "OVERRIDDEN=operator-value",
                "RUNTIME_ONLY=keep-me",
            ],
            "Labels": {
                "org.example.release": "old",
                "operator.label": "keep-me",
                "com.docker.compose.project": "plex",
                "com.docker.compose.image": OLD_ID,
                "com.docker.compose.replace": "stale-id",
                TARGET_LABEL: "true",
                IMAGE_REF_LABEL: IMAGE_REF,
            },
        }
    )
    return {
        "Id": "old-container-id-1234567890",
        "Config": old_config,
        "HostConfig": {
            "AutoRemove": False,
            "Binds": [
                "plex-manager-data:/app/data:rw",
                "/srv/media:/media:rw",
            ],
            "PortBindings": {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8000"}]},
            "RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
            "NetworkMode": "plex_default",
            "ReadonlyRootfs": False,
            "ContainerIDFile": "/stale/engine/internal",
        },
        "Mounts": [
            {
                "Type": "volume",
                "Name": "anonymous-cache-volume",
                "Source": "/var/lib/docker/volumes/anonymous-cache-volume/_data",
                "Destination": "/app/cache",
                "RW": True,
            }
        ],
        "NetworkSettings": {
            "Ports": {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8000"}]},
            "Networks": {
                "plex_default": {
                    "Aliases": ["plex-manager", "app"],
                    "IPAMConfig": {"IPv4Address": "172.20.0.10"},
                    "NetworkID": "runtime-network-id",
                    "EndpointID": "runtime-endpoint-id",
                    "IPAddress": "172.20.0.10",
                },
                "monitoring": {
                    "Aliases": ["plex-manager-metrics"],
                    "DriverOpts": {"com.example.option": "one"},
                    "NetworkID": "other-network-id",
                },
            },
        },
    }


def test_candidate_three_way_merge_preserves_runtime_contract_and_adopts_new_image() -> None:
    container = _container()
    networks = capture_networks(container)

    spec, primary = build_candidate_spec(
        container,
        _old_image(),
        _new_image(),
        image_ref=IMAGE_REF,
        operation_id="operation-123",
        networks=networks,
    )

    assert spec["Image"] == NEW_ID
    assert spec["Entrypoint"] == ["/app/new-entrypoint.sh"]
    assert spec["Cmd"] == ["serve"]
    assert spec["Healthcheck"] == {"Test": ["CMD", "new-health"]}
    assert spec["ExposedPorts"] == {"8000/tcp": {}, "9000/tcp": {}}
    assert spec["Volumes"] == {"/app/data": {}, "/app/cache": {}}

    assert spec["Env"] == [
        "PATH=/usr/local/sbin:/usr/local/bin",
        "PLEX_MANAGER_BUILD_ID=build-new",
        "IMAGE_DEFAULT=new-default",
        "NEW_DEFAULT=enabled",
        "OVERRIDDEN=operator-value",
        "RUNTIME_ONLY=keep-me",
    ]

    labels = spec["Labels"]
    assert isinstance(labels, dict)
    assert labels["org.example.release"] == "new"
    assert labels["org.example.new-label"] == "present"
    assert labels["operator.label"] == "keep-me"
    assert labels["com.docker.compose.project"] == "plex"
    assert labels["com.docker.compose.image"] == NEW_ID
    assert "com.docker.compose.replace" not in labels
    assert labels[TARGET_LABEL] == "true"
    assert labels[IMAGE_REF_LABEL] == IMAGE_REF
    assert labels[OPERATION_LABEL] == "operation-123"
    assert labels[ROLE_LABEL] == "candidate"

    host = spec["HostConfig"]
    assert isinstance(host, dict)
    assert host["Binds"] == ["plex-manager-data:/app/data:rw", "/srv/media:/media:rw"]
    assert host["Mounts"] == [
        {
            "Type": "volume",
            "Source": "anonymous-cache-volume",
            "Target": "/app/cache",
            "ReadOnly": False,
        }
    ]
    assert host["PortBindings"] == {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8000"}]}
    assert host["RestartPolicy"] == {"Name": "unless-stopped", "MaximumRetryCount": 0}
    assert host["NetworkMode"] == "plex_default"
    assert host["AutoRemove"] is False
    assert "ContainerIDFile" not in host

    assert primary == "plex_default"
    assert spec["NetworkingConfig"] == {
        "EndpointsConfig": {
            "plex_default": {
                "Aliases": ["plex-manager", "app"],
                "IPAMConfig": {"IPv4Address": "172.20.0.10"},
            }
        }
    }
    assert remaining_networks(networks, primary) == [
        (
            "monitoring",
            {
                "Aliases": ["plex-manager-metrics"],
                "DriverOpts": {"com.example.option": "one"},
            },
        )
    ]
    assert "NetworkID" not in networks["plex_default"]
    assert "EndpointID" not in networks["plex_default"]


def test_docker_assigned_port_is_materialized_for_candidate_and_rollback() -> None:
    container = _container()
    host = container["HostConfig"]
    assert isinstance(host, dict)
    host["PortBindings"] = {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": ""}]}
    settings = container["NetworkSettings"]
    assert isinstance(settings, dict)
    settings["Ports"] = {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "32780"}]}
    materialized = capture_port_bindings(container)
    assert materialized == {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "32780"}]}

    networks = capture_networks(container)
    candidate, _ = build_candidate_spec(
        container,
        _old_image(),
        _new_image(),
        image_ref=IMAGE_REF,
        operation_id="operation-ports",
        networks=networks,
        port_bindings=materialized,
    )
    rollback, _ = build_rollback_spec(
        container,
        _old_image(),
        image_ref=IMAGE_REF,
        operation_id="operation-ports",
        networks=networks,
        port_bindings=materialized,
    )
    assert candidate["HostConfig"]["PortBindings"] == materialized  # type: ignore[index]
    assert rollback["HostConfig"]["PortBindings"] == materialized  # type: ignore[index]


def test_disabled_candidate_healthcheck_is_rejected() -> None:
    new_image = _new_image()
    config = new_image["Config"]
    assert isinstance(config, dict)
    config["Healthcheck"] = {"Test": ["NONE"]}
    with pytest.raises(DockerError, match="target_healthcheck_missing"):
        build_candidate_spec(
            _container(),
            _old_image(),
            new_image,
            image_ref=IMAGE_REF,
            operation_id="operation-disabled-health",
            networks=capture_networks(_container()),
        )


def test_rollback_reuses_previous_image_but_bypasses_migration_entrypoint() -> None:
    container = _container()
    networks = capture_networks(container)

    spec, primary = build_rollback_spec(
        container,
        _old_image(),
        image_ref=IMAGE_REF,
        operation_id="operation-rollback",
        networks=networks,
    )

    assert spec["Image"] == OLD_ID
    assert spec["Entrypoint"] == ["python", "-m", "plex_manager"]
    assert spec["Cmd"] == []
    assert spec["Env"] == _container()["Config"]["Env"]  # type: ignore[index]
    labels = spec["Labels"]
    assert isinstance(labels, dict)
    assert labels["com.docker.compose.image"] == OLD_ID
    assert labels[ROLE_LABEL] == "rollback"
    assert primary == "plex_default"
