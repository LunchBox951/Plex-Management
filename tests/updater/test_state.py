"""Crash-durable updater write-ahead state."""

from __future__ import annotations

import json
import stat
from dataclasses import asdict
from pathlib import Path

import pytest

from plex_manager.updater import state as state_module
from plex_manager.updater.state import StateError, StateStore, UpdateStage, UpdateState

_LEASE_TOKEN = "lease-token-1234567890"  # noqa: S105 - synthetic test credential


def _state(*, stage: UpdateStage = "prepared") -> UpdateState:
    return UpdateState(
        version=1,
        operation_id="operation-123",
        operation="install",
        stage=stage,
        lease_token=_LEASE_TOKEN,
        action_generation=7,
        target_id="container-old",
        target_name="plex-manager",
        old_image_id="sha256:" + "a" * 64,
        old_digest="ghcr.io/example/plex@sha256:" + "a" * 64,
        old_build="build-old",
        desired_image_id="sha256:" + "b" * 64,
        desired_digest="ghcr.io/example/plex@sha256:" + "b" * 64,
        desired_build="build-new",
        networks={"project_default": {"Aliases": ["plex-manager"]}},
        port_bindings={"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8000"}]},
        candidate_id="candidate-new" if stage == "candidate_started" else None,
    )


def test_state_round_trips_atomically_with_private_permissions(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "state.json"
    store = StateStore(path)

    store.save(_state())

    assert store.load() == _state()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not path.with_suffix(".json.tmp").exists()
    assert json.loads(path.read_text(encoding="utf-8"))["operation_id"] == "operation-123"

    store.save(_state(stage="candidate_started"))
    assert store.load() == _state(stage="candidate_started")

    store.clear()
    assert store.load() is None


def test_failed_atomic_replace_keeps_last_durable_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.save(_state(stage="prepared"))

    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(state_module.os, "replace", fail_replace)

    with pytest.raises(OSError, match="synthetic replace failure"):
        store.save(_state(stage="candidate_started"))

    assert store.load() == _state(stage="prepared")
    assert not path.with_suffix(".json.tmp").exists()


@pytest.mark.parametrize(
    "payload",
    [
        "not-json",
        "[]",
        '{"version":2,"operation":"install"}',
        '{"version":1,"operation":"check","networks":{}}',
        '{"version":1,"operation":"install","networks":[]}',
    ],
)
def test_corrupt_or_incompatible_state_fails_closed(tmp_path: Path, payload: str) -> None:
    path = tmp_path / "state.json"
    path.write_text(payload, encoding="utf-8")

    with pytest.raises(StateError):
        StateStore(path).load()


@pytest.mark.parametrize(
    ("stage", "candidate_id", "rollback_id"),
    [
        ("unknown-future-stage", None, None),
        ("candidate_started", None, None),
        ("rollback_started", "candidate-new", None),
    ],
)
def test_unknown_stage_or_missing_stage_container_fails_closed(
    tmp_path: Path,
    stage: str,
    candidate_id: str | None,
    rollback_id: str | None,
) -> None:
    payload = asdict(_state())
    payload.update({"stage": stage, "candidate_id": candidate_id, "rollback_id": rollback_id})
    path = tmp_path / "state.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(StateError):
        StateStore(path).load()


def test_state_lock_rejects_a_concurrent_updater(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    first = StateStore(path)
    second = StateStore(path)

    with first, pytest.raises(StateError, match="another updater process"):
        second.acquire()

    with second:
        assert second.load() is None
