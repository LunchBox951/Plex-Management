"""Movie file/folder naming — Radarr's ``CleanFileName`` rules, ported.

Borrows the exact illegal-character handling from Radarr's
``Organizer/FileNameBuilder.cs`` (``CleanFileName`` ~line 675, ``CleanFolderName``
~262, ``ReplaceReservedDeviceNames`` ~669) so our on-disk layout matches what a
Radarr-trained operator expects. The colon handling is Radarr's *Smart* default
(``": "`` -> ``" - "``, bare ``":"`` -> ``"-"``); the bad-character table and its
replacements are copied verbatim from Radarr's ``BadCharacters`` /
``GoodCharacters`` arrays.

Pure domain: stdlib only (``re`` + ``pathlib``). No I/O, no adapter/web imports.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

__all__ = [
    "clean_title",
    "plex_movie_relative_path",
]

# Radarr ``BadCharacters`` -> ``GoodCharacters`` (verbatim). With illegal-character
# replacement enabled, ``\`` and ``/`` become ``+``, ``*`` becomes ``!``, ``|``
# becomes ``-``, and ``< > ? "`` are dropped. Colons are handled separately (the
# Smart colon rule runs first), so ``:`` is not in this table.
_BAD_CHARACTERS: tuple[tuple[str, str], ...] = (
    ("\\", "+"),
    ("/", "+"),
    ("<", ""),
    (">", ""),
    ("?", ""),
    ("*", "!"),
    ("|", "-"),
    ('"', ""),
)

# Radarr ``FileNameCleanupRegex``: collapse a run of the *same* separator
# (space, ``-``, ``.`` or ``_``) down to a single character.
_REPEATED_SEPARATOR_RE: re.Pattern[str] = re.compile(r"([- ._])\1+")

# Radarr ``ReservedDeviceNamesRegex``: a Windows reserved device name at the start
# of a component, immediately followed by a dot. The match (e.g. ``"con."``) has
# its dot replaced by ``_`` so the result is no longer reserved.
_RESERVED_DEVICE_NAME_RE: re.Pattern[str] = re.compile(
    r"^(?:aux|com[1-9]|con|lpt[1-9]|nul|prn)\.",
    re.IGNORECASE,
)


def clean_title(name: str) -> str:
    """Sanitize a title for use as a file/folder component (Radarr rules).

    Applies, in order: the Smart colon rule (``": "`` -> ``" - "`` then ``":"`` ->
    ``"-"``); the bad-character replacement table; collapsing of repeated
    separators; the reserved-device-name guard (``con.`` -> ``con_``); and a final
    trim of leading/trailing spaces and dots. The transformation is total — any
    string maps to a valid, readable component.
    """
    result = name.replace(": ", " - ").replace(":", "-")
    for bad, good in _BAD_CHARACTERS:
        result = result.replace(bad, good)
    result = _REPEATED_SEPARATOR_RE.sub(lambda match: match.group(1), result)
    result = _RESERVED_DEVICE_NAME_RE.sub(
        lambda match: match.group(0).replace(".", "_"),
        result,
    )
    return result.strip(" .")


def plex_movie_relative_path(title: str, year: int | None, ext: str) -> PurePosixPath:
    """Build a Plex movie relative path: ``Title (Year)/Title (Year).ext``.

    ``ext`` is the extension *without* a leading dot (e.g. ``"mkv"``). When
    ``year`` is ``None`` the path collapses to ``Title/Title.ext``. The shared
    ``Title (Year)`` stem is cleaned once via :func:`clean_title`, so the file
    basename is always exactly the folder name plus ``.ext``.
    """
    stem = f"{title} ({year})" if year is not None else title
    cleaned = clean_title(stem)
    return PurePosixPath(cleaned) / f"{cleaned}.{ext}"
