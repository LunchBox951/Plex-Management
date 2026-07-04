"""Shared library-root resolution: deepest-match ownership + the roots bundle.

The nested-configured-roots semantics (an anime root mounted INSIDE
``movies_root``) that both the correction failsafe and eviction's per-root
candidate assignment build on -- see ``services/library_roots.py``.
"""

from __future__ import annotations

from plex_manager.services.library_roots import LibraryRoots, deepest_containing_root


def test_deepest_root_wins_regardless_of_caller_order() -> None:
    # The Codex P2 core case: with a nested anime root, first-match-in-caller-order
    # (movies first) would return the PARENT for a path the child owns.
    path = "/media/movies/anime/Film (2020)/Film.mkv"
    nested_first = ["/media/movies/anime", "/media/movies"]
    parent_first = ["/media/movies", "/media/movies/anime"]
    assert deepest_containing_root(path, nested_first) == "/media/movies/anime"
    assert deepest_containing_root(path, parent_first) == "/media/movies/anime"


def test_parent_root_still_owns_its_own_direct_content() -> None:
    path = "/media/movies/Film (2020)/Film.mkv"
    roots = ["/media/movies", "/media/movies/anime"]
    assert deepest_containing_root(path, roots) == "/media/movies"


def test_containment_is_separator_bounded_never_a_substring_match() -> None:
    # /media/movies must not claim /media/movies2 (a sibling whose name merely
    # extends the root's).
    assert deepest_containing_root("/media/movies2/Film.mkv", ["/media/movies"]) is None


def test_path_equal_to_a_root_is_owned_by_it() -> None:
    assert deepest_containing_root("/media/movies", ["/media/movies"]) == "/media/movies"


def test_no_containing_root_returns_none_and_fails_closed() -> None:
    assert deepest_containing_root("/srv/elsewhere/Film.mkv", ["/media/movies"]) is None
    assert deepest_containing_root("", ["/media/movies"]) is None
    assert deepest_containing_root("/media/movies/Film.mkv", []) is None
    assert deepest_containing_root("/media/movies/Film.mkv", [""]) is None


def test_trailing_slash_and_dot_segments_are_normalized() -> None:
    assert (
        deepest_containing_root("/media/movies/./anime/Film.mkv", ["/media/movies/anime/"])
        == "/media/movies/anime/"
    )


def test_configured_lists_only_set_roots_in_declaration_order() -> None:
    roots = LibraryRoots(movies="/m", anime_tv="/at")
    assert roots.configured() == ("/m", "/at")
    assert LibraryRoots().configured() == ()


def test_fallback_for_prefers_the_anime_root_only_when_configured() -> None:
    both = LibraryRoots(movies="/m", tv="/t", anime_movie="/am", anime_tv="/at")
    no_anime = LibraryRoots(movies="/m", tv="/t")
    assert both.fallback_for("movie", is_anime=True) == "/am"
    assert both.fallback_for("movie", is_anime=False) == "/m"
    assert both.fallback_for("tv", is_anime=True) == "/at"
    assert both.fallback_for("tv", is_anime=False) == "/t"
    # Anime with NO anime root configured falls back to the normal root -- the
    # pre-ADR-0015 behavior (anime imports route to the normal libraries).
    assert no_anime.fallback_for("movie", is_anime=True) == "/m"
    assert no_anime.fallback_for("tv", is_anime=True) == "/t"
    # Nothing configured at all -> None (the caller surfaces the honest refusal).
    assert LibraryRoots().fallback_for("movie", is_anime=False) is None
