"""Compose profile and Docker-authority placement smoke tests."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[2]
_SECRET_PATH = "/run/secrets/plex_manager_updater"  # noqa: S105 - path, not a secret
_HARDENED_TMPFS = "/tmp:rw,noexec,nosuid,nodev,size=16m"  # noqa: S108 - asserted config


def _compose_config(tmp_path: Path, *, updater: bool) -> dict[str, Any]:
    project = tmp_path / "compose-project"
    (project / "docker").mkdir(parents=True)
    shutil.copy2(ROOT / "docker-compose.yml", project / "docker-compose.yml")
    shutil.copy2(
        ROOT / "docker" / "updater-disabled-secret",
        project / "docker" / "updater-disabled-secret",
    )
    (project / ".env").write_text("", encoding="utf-8")
    command = ["docker", "compose", "-f", str(project / "docker-compose.yml")]
    if updater:
        command.extend(["--profile", "auto-update"])
    command.extend(["config", "--format", "json"])
    environment = {
        **os.environ,
        "PLEX_MANAGER_MEDIA_ROOT": "/srv/media",
        "PLEX_MANAGER_DOWNLOADS_ROOT": "/srv/downloads",
    }
    completed = subprocess.run(  # noqa: S603 - fixed local Docker CLI and arguments
        command,
        cwd=project,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    result: object = json.loads(completed.stdout)
    assert isinstance(result, dict)
    return cast(dict[str, Any], result)


def _secret_sources(service: dict[str, Any]) -> set[str]:
    secrets = service.get("secrets", [])
    assert isinstance(secrets, list)
    return {
        cast(str, item["source"])
        for item in secrets
        if isinstance(item, dict) and isinstance(item.get("source"), str)
    }


def test_default_profile_is_valid_and_bootable_without_privileged_sidecar(
    tmp_path: Path,
) -> None:
    config = _compose_config(tmp_path, updater=False)
    services = cast(dict[str, dict[str, Any]], config["services"])

    assert set(services) == {"plex-manager"}
    app = services["plex-manager"]
    assert app["image"] == "ghcr.io/lunchbox951/plex-manager:stable"
    assert app["restart"] == "unless-stopped"
    assert app["ports"] == [
        {
            "mode": "ingress",
            "host_ip": "127.0.0.1",
            "target": 8000,
            "published": "8000",
            "protocol": "tcp",
        }
    ]
    assert _secret_sources(app) == {"plex_manager_updater"}
    assert all(
        volume.get("target") != "/var/run/docker.sock"
        for volume in cast(list[dict[str, Any]], app["volumes"])
    )


def test_auto_update_profile_confines_docker_authority_and_has_no_listener(
    tmp_path: Path,
) -> None:
    config = _compose_config(tmp_path, updater=True)
    services = cast(dict[str, dict[str, Any]], config["services"])

    assert set(services) == {"plex-manager", "updater"}
    app = services["plex-manager"]
    updater = services["updater"]
    socket_mounts = [
        (name, volume)
        for name, service in services.items()
        for volume in cast(list[dict[str, Any]], service.get("volumes", []))
        if volume.get("target") == "/var/run/docker.sock"
    ]
    assert len(socket_mounts) == 1
    socket_service, socket_mount = socket_mounts[0]
    assert socket_service == "updater"
    assert socket_mount["type"] == "bind"
    assert socket_mount["source"] == "/var/run/docker.sock"
    assert socket_mount["target"] == "/var/run/docker.sock"
    # Newer Docker Compose CLI versions populate the "bind" sub-dict with
    # additional defaults (e.g. create_host_path); only the socket
    # identity/confinement matters here, not the exact bind-option contents.
    assert isinstance(socket_mount.get("bind"), dict)
    assert "ports" not in updater
    assert _secret_sources(app) == _secret_sources(updater) == {"plex_manager_updater"}
    assert updater["environment"]["PLEX_MANAGER_UPDATER_SECRET_FILE"] == _SECRET_PATH
    assert app["environment"]["PLEX_MANAGER_UPDATER_SECRET_FILE"] == _SECRET_PATH
    assert updater["read_only"] is True
    assert updater["cap_drop"] == ["ALL"]
    assert updater["security_opt"] == ["no-new-privileges:true"]
    assert updater["tmpfs"] == [_HARDENED_TMPFS]
    assert updater["healthcheck"] == {"disable": True}
    assert updater["entrypoint"] == ["python", "-m", "plex_manager.updater"]
    assert updater["profiles"] == ["auto-update"]
