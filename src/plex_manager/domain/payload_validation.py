"""Filename policy for a complete torrent manifest; not a content scanner."""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

from plex_manager.domain.plex_video import plex_video_extension

DANGEROUS_EXTENSIONS = frozenset(
    {
        ".exe",
        ".bat",
        ".sh",
        ".py",
        ".dll",
        ".msi",
        ".app",
        ".deb",
        ".rpm",
        ".jar",
        ".cmd",
        ".ps1",
        ".vbs",
        ".scr",
        ".com",
        ".pif",
        ".apk",
        ".dmg",
        ".run",
        ".bin",
        ".lnk",
        ".js",
    }
)


@dataclass(frozen=True)
class PayloadInspection:
    video_indices: tuple[int, ...] = ()
    reason: str | None = None


def inspect_payload(names: Sequence[str]) -> PayloadInspection:
    """Inspect all siblings, including those that would not be selected."""
    for name in names:
        parts = PurePosixPath(name.replace("\\", "/")).parts
        if any(PurePosixPath(part).suffix.casefold() in DANGEROUS_EXTENSIONS for part in parts):
            return PayloadInspection(reason=f"release payload contains executable: {name}")
    videos = tuple(i for i, name in enumerate(names) if plex_video_extension(name) is not None)
    if not videos:
        return PayloadInspection(reason="release payload contains no Plex-supported video file")
    return PayloadInspection(video_indices=videos)
