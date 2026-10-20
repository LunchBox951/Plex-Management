"""Filename policy for a complete torrent manifest; not a content scanner."""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

from plex_manager.domain.plex_video import PLEX_EXTRAS_DIR_NAMES, plex_video_extension

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


HELD_METADATA_STATES = frozenset({"metaDL", "forcedMetaDL"})


def is_unstarted_held_torrent(raw_state: str, progress: float, *, has_manifest: bool) -> bool:
    """Whether a torrent still has the shape of this app's own never-started held add.

    Adds stop once metadata arrives, so such a torrent is either still fetching
    metadata or stopped at zero progress with a readable manifest. This is the one
    definition of a held torrent the app may take ownership of and start after
    validation; anything else keeps its live state.
    """
    if raw_state in HELD_METADATA_STATES:
        return True
    return raw_state in {"stoppedDL", "pausedDL"} and progress == 0 and has_manifest


@dataclass(frozen=True)
class PayloadInspection:
    video_indices: tuple[int, ...] = ()
    reason: str | None = None


def _is_feature_candidate(name: str) -> bool:
    """Mirror the import scan, so a payload that passes here has a feature to import.

    Samples and extras folders never count. The first component of a multi-file
    name is the torrent's own folder, which the import scan walks from rather than
    prunes.
    """
    parts = PurePosixPath(name.replace("\\", "/")).parts
    folders = parts[1:-1]
    if "sample" in parts[-1].casefold() or any(
        "sample" in part.casefold() or part.casefold() in PLEX_EXTRAS_DIR_NAMES for part in folders
    ):
        return False
    return plex_video_extension(name) is not None


def inspect_payload(names: Sequence[str]) -> PayloadInspection:
    """Inspect all siblings, including those that would not be selected."""
    for name in names:
        parts = PurePosixPath(name.replace("\\", "/")).parts
        if any(PurePosixPath(part).suffix.casefold() in DANGEROUS_EXTENSIONS for part in parts):
            return PayloadInspection(reason=f"release payload contains executable: {name}")
    videos = tuple(i for i, name in enumerate(names) if _is_feature_candidate(name))
    if not videos:
        return PayloadInspection(reason="release payload contains no Plex-supported video file")
    return PayloadInspection(video_indices=videos)
