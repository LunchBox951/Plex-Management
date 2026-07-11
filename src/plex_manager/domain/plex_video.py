"""Conservative Plex video-container policy shared by discovery and import.

An allowed suffix makes a path a *candidate* video only.  Callers still have to
probe the bytes and require a real, non-cover-art video stream before import.
The values are the atomic short names from ffprobe's comma-separated
``format.format_name`` field, grouped by the filename suffixes that can carry
those containers.

Pure domain: this module performs no I/O and depends only on the standard
library.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import PurePosixPath
from types import MappingProxyType

__all__ = [
    "PLEX_VIDEO_EXTENSIONS",
    "PLEX_VIDEO_FORMATS",
    "expected_probe_formats",
    "is_plex_disc_structure_path",
    "plex_video_extension",
]

_MATROSKA_FORMATS = frozenset({"matroska", "webm"})
_MOV_FORMATS = frozenset({"mov", "mp4", "m4a", "3gp", "3g2", "mj2"})
_AVI_FORMATS = frozenset({"avi"})
_ASF_FORMATS = frozenset({"asf"})
_MPEG_PS_FORMATS = frozenset({"mpeg"})
_MPEG_TS_FORMATS = frozenset({"mpegts"})
_FLV_FORMATS = frozenset({"flv"})
_OGG_FORMATS = frozenset({"ogg"})
_NO_FORMATS: frozenset[str] = frozenset()
_DISC_STRUCTURE_DIR_NAMES = frozenset({"bdmv", "video_ts"})

# Keep this mapping explicit.  Adding a suffix is a Plex-library policy change,
# not a generic "ffprobe can decode it" decision.  In particular, DVD ``.vob``
# is deliberately absent even though ffprobe understands MPEG program streams.
PLEX_VIDEO_FORMATS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        ".mkv": _MATROSKA_FORMATS,
        ".mp4": _MOV_FORMATS,
        ".m4v": _MOV_FORMATS,
        ".avi": _AVI_FORMATS,
        ".mov": _MOV_FORMATS,
        ".divx": _AVI_FORMATS,
        ".wmv": _ASF_FORMATS,
        ".mpg": _MPEG_PS_FORMATS,
        ".mpeg": _MPEG_PS_FORMATS,
        ".ts": _MPEG_TS_FORMATS,
        ".m2ts": _MPEG_TS_FORMATS,
        ".mts": _MPEG_TS_FORMATS,
        ".webm": _MATROSKA_FORMATS,
        ".flv": _FLV_FORMATS,
        ".ogv": _OGG_FORMATS,
    }
)

PLEX_VIDEO_EXTENSIONS: frozenset[str] = frozenset(PLEX_VIDEO_FORMATS)


def is_plex_disc_structure_path(path: str) -> bool:
    """Return whether any path component belongs to an optical-disc tree.

    Plex does not treat ``BDMV``/``VIDEO_TS`` directory structures as ordinary
    movie files. Matching is case-insensitive and accepts either path separator
    because download-client paths may come from a different host platform.
    """

    normalized = PurePosixPath(path.replace("\\", "/"))
    return any(part.casefold() in _DISC_STRUCTURE_DIR_NAMES for part in normalized.parts)


def plex_video_extension(path: str) -> str | None:
    """Return ``path``'s supported, normalized suffix or ``None``.

    Both POSIX and Windows separators are accepted because download-client file
    manifests are not guaranteed to use the host platform's separator.  A
    compound name is classified by its final suffix only. Files inside a
    ``BDMV``/``VIDEO_TS`` optical-disc tree are not standalone candidates even
    when their suffix would otherwise be supported.
    """

    normalized = PurePosixPath(path.replace("\\", "/"))
    if is_plex_disc_structure_path(path):
        return None
    suffix = normalized.suffix.casefold()
    if suffix in PLEX_VIDEO_FORMATS:
        return suffix
    return None


def expected_probe_formats(path: str) -> frozenset[str]:
    """Return ffprobe format aliases allowed for ``path``'s suffix.

    Unsupported or extensionless paths return an empty set.  Callers therefore
    fail closed by requiring a non-empty intersection with the probed format
    aliases.
    """

    extension = plex_video_extension(path)
    if extension is None:
        return _NO_FORMATS
    return PLEX_VIDEO_FORMATS[extension]
