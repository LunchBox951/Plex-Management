"""Crash-durable, external state for destructive Docker operations."""

from __future__ import annotations

import fcntl
import json
import os
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, Self, cast

Operation = Literal["install"]


class StateError(RuntimeError):
    """Persistent updater state is corrupt, incompatible, or locked."""


@dataclass
class UpdateState:
    """Write-ahead record for one install attempt.

    The short-lived lease token is persisted because a restarted sidecar must be
    able to renew/release it. The state volume is private to the updater and the
    file is always mode 0600. Container environment values are intentionally not
    copied here; the retained previous container remains the source of truth.
    """

    version: int
    operation_id: str
    operation: Operation
    stage: str
    lease_token: str
    target_id: str
    target_name: str
    old_image_id: str
    old_digest: str | None
    old_build: str | None
    desired_image_id: str
    desired_digest: str
    desired_build: str | None
    networks: dict[str, dict[str, Any]]
    candidate_id: str | None = None
    rollback_id: str | None = None

    @classmethod
    def from_json(cls, value: object) -> UpdateState:
        if not isinstance(value, dict):
            raise StateError("updater state must be a JSON object")
        data = cast(dict[str, object], value)
        required_strings = (
            "operation_id",
            "stage",
            "lease_token",
            "target_id",
            "target_name",
            "old_image_id",
            "desired_image_id",
            "desired_digest",
        )
        if data.get("version") != 1 or data.get("operation") != "install":
            raise StateError("unsupported updater state version or operation")
        if any(not isinstance(data.get(key), str) or not data[key] for key in required_strings):
            raise StateError("updater state is missing required identifiers")
        networks = data.get("networks")
        if not isinstance(networks, dict):
            raise StateError("updater state has invalid network data")
        for key in ("old_digest", "old_build", "desired_build", "candidate_id", "rollback_id"):
            if data.get(key) is not None and not isinstance(data.get(key), str):
                raise StateError(f"updater state field {key} is invalid")
        return cls(
            version=1,
            operation_id=cast(str, data["operation_id"]),
            operation="install",
            stage=cast(str, data["stage"]),
            lease_token=cast(str, data["lease_token"]),
            target_id=cast(str, data["target_id"]),
            target_name=cast(str, data["target_name"]),
            old_image_id=cast(str, data["old_image_id"]),
            old_digest=cast(str | None, data.get("old_digest")),
            old_build=cast(str | None, data.get("old_build")),
            desired_image_id=cast(str, data["desired_image_id"]),
            desired_digest=cast(str, data["desired_digest"]),
            desired_build=cast(str | None, data.get("desired_build")),
            networks=cast(dict[str, dict[str, Any]], networks),
            candidate_id=cast(str | None, data.get("candidate_id")),
            rollback_id=cast(str | None, data.get("rollback_id")),
        )


class StateStore:
    """Atomic state reads/writes plus a process-wide single-updater lock."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock_fd: int | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise StateError("another updater process owns the state lock") from exc
        self._lock_fd = fd

    def close(self) -> None:
        if self._lock_fd is None:
            return
        fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
        os.close(self._lock_fd)
        self._lock_fd = None

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        self.close()

    def load(self) -> UpdateState | None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise StateError("updater state could not be read") from exc
        try:
            return UpdateState.from_json(json.loads(raw))
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise StateError("updater state is corrupt") from exc

    def save(self, state: UpdateState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = json.dumps(asdict(state), sort_keys=True, separators=(",", ":")) + "\n"
        fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        try:
            os.chmod(temporary, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", closefd=True) as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
            self._fsync_parent()
        except BaseException:
            with suppress(FileNotFoundError):
                temporary.unlink()
            raise

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            return
        self._fsync_parent()

    def _fsync_parent(self) -> None:
        directory = os.open(self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
