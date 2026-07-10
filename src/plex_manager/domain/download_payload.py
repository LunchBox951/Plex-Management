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
from typing import Final

from plex_manager.ports.download_client import DownloadedFile
from plex_manager.ports.filesystem import VIDEO_EXTENSIONS

__all__ = [
    "ALLOWED_PAYLOAD_EXTENSIONS",
    "EMPTY_PAYLOAD_REJECTION_REASON",
    "PAYLOAD_REJECTION_REASON_PREFIX",
    "PAYLOAD_VALIDATION_POLICY_VERSION",
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

_AMBIGUOUS_SCRIPT_EXTENSIONS = frozenset({".ts"})
# ``.ts`` is both an MPEG transport-stream container and a directly executable
# TypeScript source extension. The client-neutral manifest exposes only an
# untrusted name and declared size, so neither an absolute nor relative size
# heuristic can distinguish them safely (a script can be padded). Fail closed at
# the untrusted torrent boundary; ``.m2ts`` remains available for transport-stream
# video, and downstream filesystem/import support for existing ``.ts`` media is
# intentionally unchanged.
ALLOWED_PAYLOAD_EXTENSIONS: frozenset[str] = (
    VIDEO_EXTENSIONS - _AMBIGUOUS_SCRIPT_EXTENSIONS
) | SUBTITLE_EXTENSIONS
# Bump this whenever the allowlist or path-safety semantics change. Import-started
# history stores the version alongside its manifest-validation attestation so a
# crash breadcrumb can never be resumed under a policy stricter than the one that
# originally approved its torrent.
PAYLOAD_VALIDATION_POLICY_VERSION: Final = "v1"
PAYLOAD_REJECTION_REASON_PREFIX = "torrent payload rejected"
EMPTY_PAYLOAD_REJECTION_REASON = (
    f"{PAYLOAD_REJECTION_REASON_PREFIX}: no files reported after completion"
)
_DISPLAY_NAME_MAX_LENGTH = 180
_UNSAFE_UNICODE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})
_WINDOWS_INVALID_FILENAME_CHARS = frozenset({"<", ">", '"', "|", "?", "*"})
_WINDOWS_RESERVED_BASENAMES = frozenset(
    {"con", "prn", "aux", "nul", "conin$", "conout$"}
    | {f"com{suffix}" for suffix in "123456789¹²³"}
    | {f"lpt{suffix}" for suffix in "123456789¹²³"}
)


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
    for part in parts:
        if part in ("", ".", ".."):
            return False
        if any(char in _WINDOWS_INVALID_FILENAME_CHARS for char in part):
            return False
        # Win32 strips trailing spaces/dots from ordinary path components, so
        # ``".. "`` becomes traversal and distinct torrent entries can alias the
        # same on-disk name. Colons introduce NTFS alternate data streams, while
        # reserved device basenames remain special even with an extension.
        windows_normalized = part.rstrip(" .")
        if windows_normalized != part or ":" in part:
            return False
        windows_basename = windows_normalized.split(".", 1)[0].rstrip(" ").casefold()
        if windows_basename in _WINDOWS_RESERVED_BASENAMES:
            return False
    return True


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


def _display_detail(detail: str) -> str:
    sanitized = "".join(
        "?" if unicodedata.category(char) in _UNSAFE_UNICODE_CATEGORIES else char for char in detail
    )
    if len(sanitized) <= _DISPLAY_NAME_MAX_LENGTH:
        return sanitized
    return sanitized[: _DISPLAY_NAME_MAX_LENGTH - 3] + "..."


def format_payload_rejection(validation: PayloadValidation) -> str:
    """Build the concise failed_reason shown in the queue for an unsafe payload."""
    if not validation.rejections:
        return PAYLOAD_REJECTION_REASON_PREFIX
    first = validation.rejections[0]
    return (
        f"{PAYLOAD_REJECTION_REASON_PREFIX}: "
        f"{_display_detail(first.detail)} ({_display_name(first.name)})"
    )
