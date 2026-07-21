"""Bootstrap-only configuration for the updater sidecar."""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from pathlib import Path

TARGET_LABEL = "io.github.lunchbox951.plex-manager.update.target"
IMAGE_REF_LABEL = "io.github.lunchbox951.plex-manager.update.image-ref"
OPERATION_LABEL = "io.github.lunchbox951.plex-manager.update.operation"
ROLE_LABEL = "io.github.lunchbox951.plex-manager.update.role"

_DEFAULT_IMAGE = "ghcr.io/lunchbox951/plex-manager:stable"
# The app declares the sidecar unavailable once its last liveness signal is
# older than 45 seconds (web/routers/updates.py). An idle sidecar only signals
# on each eligibility poll, so the poll interval must stay comfortably inside
# that window; otherwise the Status page reports ``updater_unavailable`` for
# most of every interval and manual actions are refused while the sidecar is in
# fact healthy. 30s (the default) leaves a 15s margin for request latency.
_MAX_POLL_SECONDS = 30.0
_IMAGE_RE = re.compile(
    r"(?=.{1,255}\Z)(?:[a-zA-Z0-9.-]+(?::[0-9]+)?/)?"
    r"[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*"
    r":[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}"
)
_CONTAINER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


class UpdaterConfigError(ValueError):
    """The install-time sidecar configuration is unsafe or incomplete."""


def _positive_float(
    name: str,
    default: float,
    *,
    minimum: float = 0.0,
    maximum: float = 3600.0,
) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise UpdaterConfigError(f"{name} must be a number") from exc
    if not math.isfinite(value) or value <= 0 or value < minimum or value > maximum:
        lower = f"at least {minimum:g}" if minimum > 0 else "greater than zero"
        raise UpdaterConfigError(f"{name} must be {lower} and at most {maximum:g}")
    return value


def _image_ref(value: str) -> str:
    candidate = value.strip()
    if candidate != value or _IMAGE_RE.fullmatch(candidate) is None:
        raise UpdaterConfigError("PLEX_MANAGER_IMAGE must be one fixed repository:tag reference")
    return candidate


def _container_name(value: str) -> str:
    candidate = value.strip()
    if candidate != value or _CONTAINER_RE.fullmatch(candidate) is None:
        raise UpdaterConfigError("PLEX_MANAGER_CONTAINER_NAME is invalid")
    return candidate


@dataclass(frozen=True)
class UpdaterConfig:
    """The small, fixed authority envelope granted to the sidecar."""

    image_ref: str
    container_name: str
    docker_socket: str
    coordinator_url: str
    secret_file: Path
    state_file: Path
    poll_seconds: float
    request_timeout_seconds: float
    health_timeout_seconds: float
    drain_timeout_seconds: float

    @classmethod
    def from_env(cls) -> UpdaterConfig:
        """Load and validate only bootstrap values; policy never enters this process."""
        image_ref = _image_ref(os.environ.get("PLEX_MANAGER_IMAGE", _DEFAULT_IMAGE))
        container_name = _container_name(
            os.environ.get("PLEX_MANAGER_CONTAINER_NAME", "plex-manager")
        )
        socket = os.environ.get("PLEX_MANAGER_UPDATER_DOCKER_SOCKET", "/var/run/docker.sock")
        if not socket.startswith("/") or any(ch in socket for ch in "\r\n\0"):
            raise UpdaterConfigError("PLEX_MANAGER_UPDATER_DOCKER_SOCKET must be absolute")
        coordinator_url = os.environ.get(
            "PLEX_MANAGER_UPDATER_COORDINATOR_URL",
            "http://plex-manager:8000/api/v1/internal/updates",
        ).rstrip("/")
        if not coordinator_url.startswith(("http://", "https://")) or any(
            ch in coordinator_url for ch in "\r\n\0"
        ):
            raise UpdaterConfigError("PLEX_MANAGER_UPDATER_COORDINATOR_URL is invalid")
        secret_file = Path(
            os.environ.get("PLEX_MANAGER_UPDATER_SECRET_FILE", "/run/secrets/plex_manager_updater")
        )
        state_file = Path(
            os.environ.get(
                "PLEX_MANAGER_UPDATER_STATE_FILE", "/var/lib/plex-manager-updater/state.json"
            )
        )
        if not secret_file.is_absolute() or not state_file.is_absolute():
            raise UpdaterConfigError("updater secret and state paths must be absolute")
        return cls(
            image_ref=image_ref,
            container_name=container_name,
            docker_socket=socket,
            coordinator_url=coordinator_url,
            secret_file=secret_file,
            state_file=state_file,
            poll_seconds=_positive_float(
                "PLEX_MANAGER_UPDATER_POLL_SECONDS",
                30.0,
                minimum=1.0,
                maximum=_MAX_POLL_SECONDS,
            ),
            request_timeout_seconds=_positive_float(
                "PLEX_MANAGER_UPDATER_REQUEST_TIMEOUT_SECONDS", 10.0
            ),
            health_timeout_seconds=_positive_float(
                "PLEX_MANAGER_UPDATER_HEALTH_TIMEOUT_SECONDS", 240.0, maximum=240.0
            ),
            drain_timeout_seconds=_positive_float(
                "PLEX_MANAGER_UPDATER_DRAIN_TIMEOUT_SECONDS", 300.0
            ),
        )

    def read_secret(self) -> str:
        """Read the Compose secret without ever placing it in configuration reprs."""
        try:
            token = self.secret_file.read_text(encoding="utf-8").strip()
        except FileNotFoundError as exc:
            raise UpdaterConfigError("updater Compose secret file is missing") from exc
        except PermissionError as exc:
            raise UpdaterConfigError(
                "updater Compose secret file is unreadable; check its ownership and mode"
            ) from exc
        except OSError as exc:
            raise UpdaterConfigError(
                "updater Compose secret file cannot be read; "
                "check the secret mount and host filesystem"
            ) from exc
        if not 32 <= len(token) <= 512 or any(ch in token for ch in "\r\n\0"):
            raise UpdaterConfigError("updater Compose secret has an invalid shape")
        return token
