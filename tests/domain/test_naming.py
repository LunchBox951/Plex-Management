"""Tests for the pure movie-naming module (Radarr ``CleanFileName`` rules)."""

from __future__ import annotations

from pathlib import PurePosixPath

import pytest

from plex_manager.domain.naming import clean_title, plex_movie_relative_path

# (raw title, expected cleaned component). Each row exercises one Radarr rule.
_CLEAN_TITLE_CASES: tuple[tuple[str, str], ...] = (
    # already-clean titles pass through untouched
    ("The Matrix", "The Matrix"),
    # Smart colon: ": " -> " - "
    ("Mission: Impossible", "Mission - Impossible"),
    # bare colon -> "-"
    ("Mission:Impossible", "Mission-Impossible"),
    # slash -> "+"
    ("Face/Off", "Face+Off"),
    # backslash -> "+"
    ("A\\B", "A+B"),
    # star -> "!", pipe -> "-"
    ("2*3", "2!3"),
    ("Either|Or", "Either-Or"),
    # dropped characters: < > ? "
    ('What? <Yes> "No"', "What Yes No"),
    # repeated separators collapse to one
    ("A   B", "A B"),
    ("A---B", "A-B"),
    ("A...B", "A.B"),
    ("A___B", "A_B"),
    # trailing dot and space are trimmed
    ("The Movie. ", "The Movie"),
    ("  Padded  ", "Padded"),
    # reserved device name followed by a dot -> dot becomes "_"
    ("con.mkv", "con_mkv"),
    ("PRN.txt", "PRN_txt"),
    # a bare reserved name (no dot) is left untouched
    ("con", "con"),
    # unicode is preserved
    ("Amélie", "Amélie"),
    ("七人の侍", "七人の侍"),
)


@pytest.mark.parametrize(("raw", "expected"), _CLEAN_TITLE_CASES)
def test_clean_title(raw: str, expected: str) -> None:
    assert clean_title(raw) == expected


def test_plex_path_with_year() -> None:
    path = plex_movie_relative_path("The Matrix", 1999, "mkv")
    assert path == PurePosixPath("The Matrix (1999)/The Matrix (1999).mkv")


def test_plex_path_without_year() -> None:
    path = plex_movie_relative_path("The Matrix", None, "mkv")
    assert path == PurePosixPath("The Matrix/The Matrix.mkv")


def test_plex_path_cleans_components() -> None:
    path = plex_movie_relative_path("Mission: Impossible", 1996, "mp4")
    assert path == PurePosixPath("Mission - Impossible (1996)/Mission - Impossible (1996).mp4")


@pytest.mark.parametrize(
    ("title", "year", "ext"),
    (
        ("The Matrix", 1999, "mkv"),
        ("Mission: Impossible", 1996, "mp4"),
        ("Face/Off", 1997, "avi"),
        ("Amélie", 2001, "mkv"),
        ("No Year", None, "mkv"),
    ),
)
def test_file_basename_is_folder_plus_ext(title: str, year: int | None, ext: str) -> None:
    path = plex_movie_relative_path(title, year, ext)
    folder = path.parent.name
    assert path.name == f"{folder}.{ext}"
