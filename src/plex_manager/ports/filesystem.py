"""FileSystemPort — the local-filesystem interface for the import step.

Defined now, used in v1: the import pipeline (validate -> rename -> route) calls
these. Operations are synchronous (local disk). ``hardlink_or_copy`` hardlinks
when possible and falls back to a copy across devices.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

__all__ = ["FileSystemPort"]


@runtime_checkable
class FileSystemPort(Protocol):
    """Disk-space queries and move / hardlink-or-copy operations."""

    def available_bytes(self, path: Path) -> int:
        """Return free bytes on the filesystem containing ``path``."""
        ...

    def move(self, src: Path, dst: Path) -> None:
        """Move ``src`` to ``dst`` (atomic rename when on the same device)."""
        ...

    def hardlink_or_copy(self, src: Path, dst: Path) -> None:
        """Hardlink ``src`` to ``dst``, falling back to a copy across devices."""
        ...
