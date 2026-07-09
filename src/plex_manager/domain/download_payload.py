"""Torrent payload safety checks.

The release parser and import validator decide whether a completed video is the
right media. This module handles the earlier, narrower security gate: once the
download client exposes a torrent's manifest, every file in that immutable
manifest must be either media or a subtitle sidecar. Anything else is rejected
before the torrent can finish and reach import.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath, PureWindowsPath

from plex_manager.ports.download_client import DownloadedFile
from plex_manager.ports.filesystem import VIDEO_EXTENSIONS

__all__ = [
    "ALLOWED_PAYLOAD_EXTENSIONS",
    "EMPTY_PAYLOAD_REJECTION_REASON",
    "SUBTITLE_EXTENSIONS",
    "PayloadRejection",
    "PayloadRejectionReason",
    "PayloadValidation",
    "format_payload_rejection",
    "validate_payload_files",
]

# Subtitle sidecars Plex can use directly. Text/readme/artwork/NFO files are not
# included: they do not improve playback enough to justify retaining unrelated
# payload types from an untrusted torrent.
SUBTITLE_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".srt",
        ".ass",
        ".ssa",
        ".vtt",
        ".sub",
        ".idx",
        ".sup",
        ".smi",
    }
)

ALLOWED_PAYLOAD_EXTENSIONS: frozenset[str] = VIDEO_EXTENSIONS | SUBTITLE_EXTENSIONS
EMPTY_PAYLOAD_REJECTION_REASON = "torrent payload rejected: no files reported after completion"
_DISPLAY_NAME_MAX_LENGTH = 180
_UNSAFE_UNICODE_CATEGORIES = frozenset({"Cc", "Cf"})


class PayloadRejectionReason(StrEnum):
    """Why a torrent manifest file is not safe to keep downloading."""

    UNSAFE_PATH = "unsafe_path"
    UNSUPPORTED_EXTENSION = "unsupported_extension"


@dataclass(frozen=True)
class PayloadRejection:
    """One unsafe file from a torrent manifest."""

    name: str
    reason: PayloadRejectionReason
    detail: str


@dataclass(frozen=True)
class PayloadValidation:
    """Result of validating a torrent's immutable file manifest."""

    accepted: bool
    rejections: tuple[PayloadRejection, ...]


def _is_safe_relative_path(name: str) -> bool:
    if not name or "\x00" in name:
        return False
    if any(unicodedata.category(char) in _UNSAFE_UNICODE_CATEGORIES for char in name):
        return False
    if PurePosixPath(name.replace("\\", "/")).is_absolute():
        return False
    windows_path = PureWindowsPath(name)
    if windows_path.is_absolute() or windows_path.drive:
        return False

    parts = name.replace("\\", "/").split("/")
    return all(part not in ("", ".", "..") for part in parts)


def _extension(name: str) -> str:
    basename = name.replace("\\", "/").rsplit("/", 1)[-1]
    dot = basename.rfind(".")
    if dot <= 0:
        return ""
    return basename[dot:].lower()


def validate_payload_files(files: Sequence[DownloadedFile]) -> PayloadValidation:
    """Allow only video containers and subtitle sidecars in a torrent manifest."""
    rejections: list[PayloadRejection] = []
    for file in files:
        if not _is_safe_relative_path(file.name):
            rejections.append(
                PayloadRejection(
                    name=file.name,
                    reason=PayloadRejectionReason.UNSAFE_PATH,
                    detail="unsafe relative path",
                )
            )
            continue

        extension = _extension(file.name)
        if extension not in ALLOWED_PAYLOAD_EXTENSIONS:
            rejections.append(
                PayloadRejection(
                    name=file.name,
                    reason=PayloadRejectionReason.UNSUPPORTED_EXTENSION,
                    detail=f"unsupported file type {extension or '<none>'}",
                )
            )

    return PayloadValidation(accepted=not rejections, rejections=tuple(rejections))


def _display_name(name: str) -> str:
    sanitized = "".join(
        "?" if unicodedata.category(char) in _UNSAFE_UNICODE_CATEGORIES else char for char in name
    )
    if len(sanitized) <= _DISPLAY_NAME_MAX_LENGTH:
        return sanitized
    return sanitized[: _DISPLAY_NAME_MAX_LENGTH - 3] + "..."


def format_payload_rejection(validation: PayloadValidation) -> str:
    """Build the concise failed_reason shown in the queue for an unsafe payload."""
    if not validation.rejections:
        return "torrent payload rejected"
    first = validation.rejections[0]
    return f"torrent payload rejected: {first.detail} ({_display_name(first.name)})"
