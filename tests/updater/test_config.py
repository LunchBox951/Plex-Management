"""Updater bootstrap configuration and Compose-secret validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from plex_manager.updater.config import UpdaterConfig, UpdaterConfigError

_UPDATER_ENV = (
    "PLEX_MANAGER_IMAGE",
    "PLEX_MANAGER_CONTAINER_NAME",
    "PLEX_MANAGER_UPDATER_DOCKER_SOCKET",
    "PLEX_MANAGER_UPDATER_COORDINATOR_URL",
    "PLEX_MANAGER_UPDATER_SECRET_FILE",
    "PLEX_MANAGER_UPDATER_STATE_FILE",
    "PLEX_MANAGER_UPDATER_POLL_SECONDS",
    "PLEX_MANAGER_UPDATER_REQUEST_TIMEOUT_SECONDS",
    "PLEX_MANAGER_UPDATER_HEALTH_TIMEOUT_SECONDS",
    "PLEX_MANAGER_UPDATER_DRAIN_TIMEOUT_SECONDS",
)


def _clean_updater_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _UPDATER_ENV:
        monkeypatch.delenv(name, raising=False)


def test_from_env_loads_a_fixed_authority_envelope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clean_updater_env(monkeypatch)
    secret = tmp_path / "updater.secret"
    state = tmp_path / "state.json"
    monkeypatch.setenv("PLEX_MANAGER_IMAGE", "registry.example.test:5443/media/plex:edge")
    monkeypatch.setenv("PLEX_MANAGER_CONTAINER_NAME", "plex-manager_2")
    monkeypatch.setenv("PLEX_MANAGER_UPDATER_DOCKER_SOCKET", "/run/docker.sock")
    monkeypatch.setenv("PLEX_MANAGER_UPDATER_COORDINATOR_URL", "https://app/internal/")
    monkeypatch.setenv("PLEX_MANAGER_UPDATER_SECRET_FILE", str(secret))
    monkeypatch.setenv("PLEX_MANAGER_UPDATER_STATE_FILE", str(state))
    monkeypatch.setenv("PLEX_MANAGER_UPDATER_POLL_SECONDS", "12.5")

    config = UpdaterConfig.from_env()

    assert config.image_ref == "registry.example.test:5443/media/plex:edge"
    assert config.container_name == "plex-manager_2"
    assert config.docker_socket == "/run/docker.sock"
    assert config.coordinator_url == "https://app/internal"
    assert config.secret_file == secret
    assert config.state_file == state
    assert config.poll_seconds == 12.5
    assert config.request_timeout_seconds == 10.0
    assert config.health_timeout_seconds == 240.0
    assert config.drain_timeout_seconds == 300.0


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PLEX_MANAGER_IMAGE", "ghcr.io/example/plex@sha256:" + "a" * 64),
        ("PLEX_MANAGER_IMAGE", " ghcr.io/example/plex:stable"),
        ("PLEX_MANAGER_CONTAINER_NAME", "plex/manager"),
        ("PLEX_MANAGER_UPDATER_DOCKER_SOCKET", "var/run/docker.sock"),
        ("PLEX_MANAGER_UPDATER_COORDINATOR_URL", "file:///tmp/coordinator"),
        ("PLEX_MANAGER_UPDATER_SECRET_FILE", "relative.secret"),
        ("PLEX_MANAGER_UPDATER_STATE_FILE", "relative-state.json"),
        ("PLEX_MANAGER_UPDATER_POLL_SECONDS", "0"),
        ("PLEX_MANAGER_UPDATER_POLL_SECONDS", "0.01"),
        ("PLEX_MANAGER_UPDATER_POLL_SECONDS", "0.999"),
        ("PLEX_MANAGER_UPDATER_POLL_SECONDS", "NaN"),
        ("PLEX_MANAGER_UPDATER_POLL_SECONDS", "-NaN"),
        # Above the cap the idle sidecar would outlive the app's 45s heartbeat
        # window and the Status page would report updater_unavailable between
        # polls despite a healthy sidecar. Fail closed at startup instead.
        ("PLEX_MANAGER_UPDATER_POLL_SECONDS", "30.001"),
        ("PLEX_MANAGER_UPDATER_POLL_SECONDS", "45"),
        ("PLEX_MANAGER_UPDATER_POLL_SECONDS", "3600"),
        ("PLEX_MANAGER_UPDATER_REQUEST_TIMEOUT_SECONDS", "NaN"),
        ("PLEX_MANAGER_UPDATER_HEALTH_TIMEOUT_SECONDS", "NaN"),
        ("PLEX_MANAGER_UPDATER_DRAIN_TIMEOUT_SECONDS", "NaN"),
        ("PLEX_MANAGER_UPDATER_REQUEST_TIMEOUT_SECONDS", "3601"),
        ("PLEX_MANAGER_UPDATER_HEALTH_TIMEOUT_SECONDS", "soon"),
    ],
)
def test_from_env_rejects_authority_expansion_or_invalid_bounds(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    _clean_updater_env(monkeypatch)
    monkeypatch.setenv(name, value)

    with pytest.raises(UpdaterConfigError):
        UpdaterConfig.from_env()


def test_from_env_accepts_minimum_poll_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    _clean_updater_env(monkeypatch)
    monkeypatch.setenv("PLEX_MANAGER_UPDATER_POLL_SECONDS", "1")

    assert UpdaterConfig.from_env().poll_seconds == 1.0


def test_poll_interval_is_capped_inside_the_heartbeat_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The maximum poll interval keeps idle heartbeats fresher than the app's 45s expiry."""
    _clean_updater_env(monkeypatch)
    monkeypatch.setenv("PLEX_MANAGER_UPDATER_POLL_SECONDS", "30")
    assert UpdaterConfig.from_env().poll_seconds == 30.0
    # The default itself must sit at or below the cap.
    _clean_updater_env(monkeypatch)
    assert UpdaterConfig.from_env().poll_seconds <= 30.0 < 45.0


def test_read_secret_accepts_a_private_file_without_exposing_its_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clean_updater_env(monkeypatch)
    token = "test-updater-token-0123456789abcdef"  # noqa: S105 - synthetic test secret
    secret = tmp_path / "updater.secret"
    secret.write_text(token + "\n", encoding="utf-8")
    monkeypatch.setenv("PLEX_MANAGER_UPDATER_SECRET_FILE", str(secret))

    config = UpdaterConfig.from_env()

    assert config.read_secret() == token
    assert token not in repr(config)


@pytest.mark.parametrize("contents", ["short", "x" * 513, "x" * 32 + "\0"])
def test_read_secret_fails_closed_for_missing_or_malformed_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, contents: str
) -> None:
    _clean_updater_env(monkeypatch)
    secret = tmp_path / "updater.secret"
    secret.write_text(contents, encoding="utf-8")
    monkeypatch.setenv("PLEX_MANAGER_UPDATER_SECRET_FILE", str(secret))

    with pytest.raises(UpdaterConfigError, match="invalid shape"):
        UpdaterConfig.from_env().read_secret()


def test_read_secret_fails_closed_when_secret_file_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clean_updater_env(monkeypatch)
    monkeypatch.setenv("PLEX_MANAGER_UPDATER_SECRET_FILE", str(tmp_path / "missing"))

    with pytest.raises(UpdaterConfigError, match="secret file is missing"):
        UpdaterConfig.from_env().read_secret()


def test_read_secret_reports_an_unreadable_secret_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clean_updater_env(monkeypatch)
    secret = tmp_path / "updater.secret"
    monkeypatch.setenv("PLEX_MANAGER_UPDATER_SECRET_FILE", str(secret))

    def raise_permission_error(*_args: object, **_kwargs: object) -> str:
        raise PermissionError("permission denied")

    monkeypatch.setattr(Path, "read_text", raise_permission_error)

    with pytest.raises(UpdaterConfigError, match="secret file is unreadable"):
        UpdaterConfig.from_env().read_secret()
