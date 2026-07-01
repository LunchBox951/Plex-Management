"""Movie/TV file/folder naming — Radarr/Sonarr's ``CleanFileName`` rules, ported.

Borrows the exact illegal-character handling from Radarr's
``Organizer/FileNameBuilder.cs`` (``CleanFileName`` ~line 675, ``CleanFolderName``
~262, ``ReplaceReservedDeviceNames`` ~669) so our on-disk layout matches what a
Radarr-trained operator expects. The colon handling is Radarr's *Smart* default
(``": "`` -> ``" - "``, bare ``":"`` -> ``"-"``); the bad-character table and its
replacements are copied verbatim from Radarr's ``BadCharacters`` /
``GoodCharacters`` arrays. The TV layout (show/season/episode) is Sonarr's default
naming, built on the SAME ``clean_title`` so a show's folder, season folder, and
episode filenames never disagree on how the title was sanitized.

Pure domain: stdlib only (``re`` + ``pathlib``). No I/O, no adapter/web imports.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import PurePosixPath

__all__ = [
    "clean_title",
    "plex_movie_relative_path",
    "plex_tv_episode_relative_path",
    "plex_tv_season_relative_dir",
    "plex_tv_show_relative_dir",
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


def plex_tv_show_relative_dir(title: str, year: int | None) -> PurePosixPath:
    """Build a Plex TV show relative dir: ``Series (Year)`` (``Series`` if no year).

    Same shape as :func:`plex_movie_relative_path`'s folder half — the show root
    Plex scans for season subfolders.
    """
    stem = f"{title} ({year})" if year is not None else title
    return PurePosixPath(clean_title(stem))


def plex_tv_season_relative_dir(title: str, year: int | None, season: int) -> PurePosixPath:
    """Build a Plex TV season relative dir: ``Series (Year)/Season NN``.

    ``season=0`` is Plex's "Specials" bucket and still renders as ``Season 00``
    (Plex reads the number, not a special-cased folder name).
    """
    return plex_tv_show_relative_dir(title, year) / f"Season {season:02d}"


def _episode_token(season: int, episodes: Sequence[int]) -> str:
    """Build the ``SxxEyy`` episode token: ``S02E05``, ``S02E05-E07`` (contiguous
    multi-ep) or ``S02E04E06`` (non-contiguous multi-ep).

    ``episodes`` is sorted first so caller order never matters. A single episode
    yields ``SxxEyy``. More than one *contiguous* episode (``ordered[-1] -
    ordered[0] == len(ordered) - 1``, i.e. no gaps) yields a dash range spanning
    the lowest to the highest episode number — Plex parses ``SxxEyy-Ezz`` as every
    episode in that inclusive range, so a range token is only correct when the
    file actually contains every episode in between. A non-contiguous set (e.g.
    ``[4, 6]``, missing 5) instead enumerates each episode explicitly
    (``S02E04E06``), matching Sonarr's style for gapped multi-episode releases.
    Raises :class:`ValueError` on an empty sequence: a file must name at least one
    episode.
    """
    if not episodes:
        raise ValueError("_episode_token requires at least one episode number")
    ordered = sorted(episodes)
    season_part = f"S{season:02d}"
    if len(ordered) == 1:
        return f"{season_part}E{ordered[0]:02d}"
    contiguous = ordered[-1] - ordered[0] == len(ordered) - 1
    if contiguous:
        return f"{season_part}E{ordered[0]:02d}-E{ordered[-1]:02d}"
    return season_part + "".join(f"E{episode:02d}" for episode in ordered)


def plex_tv_episode_relative_path(
    title: str,
    year: int | None,
    season: int,
    episodes: Sequence[int],
    ext: str,
) -> PurePosixPath:
    """Build a Plex TV episode relative path.

    ``Series (Year)/Season NN/Series - SxxEyy[-Eyy].ext``. The filename omits the
    year (Sonarr's default) but reuses the SAME ``clean_title(title)`` as the show
    folder, so the episode filename always agrees with the directory tree on how
    the title was sanitized. ``ext`` has no leading dot. Raises :class:`ValueError`
    via :func:`_episode_token` when ``episodes`` is empty.
    """
    season_dir = plex_tv_season_relative_dir(title, year, season)
    token = _episode_token(season, episodes)
    filename = f"{clean_title(title)} - {token}.{ext}"
    return season_dir / filename
