"""Tests for the pure movie-naming module (Radarr ``CleanFileName`` rules)."""

from __future__ import annotations

from pathlib import PurePosixPath

import pytest

from plex_manager.domain.naming import (
    _episode_token,  # pyright: ignore[reportPrivateUsage]
    clean_title,
    plex_movie_relative_path,
    plex_tv_episode_relative_path,
    plex_tv_season_relative_dir,
    plex_tv_show_relative_dir,
)

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


# -- TV: show / season / episode naming -----------------------------------------


def test_plex_tv_show_relative_dir_with_year() -> None:
    assert plex_tv_show_relative_dir("Breaking Bad", 2008) == PurePosixPath("Breaking Bad (2008)")


def test_plex_tv_show_relative_dir_without_year() -> None:
    assert plex_tv_show_relative_dir("Breaking Bad", None) == PurePosixPath("Breaking Bad")


def test_plex_tv_show_relative_dir_cleans_title() -> None:
    # Mirrors the movie path's Smart-colon handling.
    assert plex_tv_show_relative_dir("Mission: Impossible", 1996) == PurePosixPath(
        "Mission - Impossible (1996)"
    )


def test_plex_tv_season_relative_dir_normal() -> None:
    path = plex_tv_season_relative_dir("Breaking Bad", 2008, 2)
    assert path == PurePosixPath("Breaking Bad (2008)/Season 02")


def test_plex_tv_season_relative_dir_specials() -> None:
    # Season 0 is Plex's "Specials" bucket; still renders as "Season 00".
    path = plex_tv_season_relative_dir("Breaking Bad", 2008, 0)
    assert path == PurePosixPath("Breaking Bad (2008)/Season 00")


def test_plex_tv_season_relative_dir_no_year() -> None:
    path = plex_tv_season_relative_dir("Breaking Bad", None, 1)
    assert path == PurePosixPath("Breaking Bad/Season 01")


def test_episode_token_single_episode() -> None:
    assert _episode_token(2, [5]) == "S02E05"


def test_episode_token_multi_episode_sorts_and_ranges() -> None:
    # Caller order must not matter: a *contiguous* set spans lowest -> highest.
    assert _episode_token(2, [6, 5]) == "S02E05-E06"
    assert _episode_token(2, [5, 6, 7]) == "S02E05-E07"
    assert _episode_token(2, [7, 5, 6]) == "S02E05-E07"


def test_episode_token_non_contiguous_enumerates_instead_of_ranging() -> None:
    # A dash range ("S02E04-E06") is a Plex *inclusive* range and would falsely
    # mark episode 5 present when the file doesn't contain it. Non-contiguous
    # sets must enumerate each episode explicitly instead (Sonarr style).
    assert _episode_token(2, [4, 6]) == "S02E04E06"
    assert _episode_token(2, [6, 4]) == "S02E04E06"
    assert _episode_token(2, [1, 3, 5]) == "S02E01E03E05"


def test_episode_token_specials_season() -> None:
    assert _episode_token(0, [3]) == "S00E03"


def test_episode_token_raises_on_empty() -> None:
    with pytest.raises(ValueError, match="at least one episode"):
        _episode_token(1, [])


def test_plex_tv_episode_relative_path_normal() -> None:
    path = plex_tv_episode_relative_path("Breaking Bad", 2008, 2, [5], "mkv")
    assert path == PurePosixPath("Breaking Bad (2008)/Season 02/Breaking Bad - S02E05.mkv")


def test_plex_tv_episode_relative_path_multi_ep() -> None:
    path = plex_tv_episode_relative_path("Breaking Bad", 2008, 2, [5, 6], "mkv")
    assert path == PurePosixPath("Breaking Bad (2008)/Season 02/Breaking Bad - S02E05-E06.mkv")


def test_plex_tv_episode_relative_path_multi_ep_non_contiguous() -> None:
    path = plex_tv_episode_relative_path("Breaking Bad", 2008, 2, [4, 6], "mkv")
    assert path == PurePosixPath("Breaking Bad (2008)/Season 02/Breaking Bad - S02E04E06.mkv")


def test_plex_tv_episode_relative_path_specials() -> None:
    path = plex_tv_episode_relative_path("Breaking Bad", 2008, 0, [1], "mkv")
    assert path == PurePosixPath("Breaking Bad (2008)/Season 00/Breaking Bad - S00E01.mkv")


def test_plex_tv_episode_relative_path_colon_title() -> None:
    path = plex_tv_episode_relative_path("Mission: Impossible", 1996, 1, [1], "mp4")
    assert path == PurePosixPath(
        "Mission - Impossible (1996)/Season 01/Mission - Impossible - S01E01.mp4"
    )


def test_plex_tv_episode_relative_path_no_year() -> None:
    # The filename never carries the year regardless; without a show year the
    # folder tree drops it too.
    path = plex_tv_episode_relative_path("Breaking Bad", None, 1, [1], "mkv")
    assert path == PurePosixPath("Breaking Bad/Season 01/Breaking Bad - S01E01.mkv")


def test_plex_tv_episode_relative_path_raises_on_empty_episodes() -> None:
    with pytest.raises(ValueError, match="at least one episode"):
        plex_tv_episode_relative_path("Breaking Bad", 2008, 2, [], "mkv")
