"""Shared library-root resolution (ADR-0015 follow-up): ONE deepest-match owner
per breadcrumb, used by both the correction failsafe and eviction's per-root
candidate assignment so the two can never drift.

Configured roots may legitimately NEST (e.g. ``anime_movie_root=/media/movies/anime``
under ``movies_root=/media/movies`` -- often its own mount). Any "which root does
this path belong to?" question must then pick the MOST SPECIFIC (deepest) containing
root, never the first ancestor in some caller-defined order:

* the report-issue mount failsafe (``correction_service``) must verify the
  breadcrumb's OWN root -- checking a mounted parent while the nested child mount
  is down would wave the purge through against a file that is not really gone;
* an eviction sweep for a parent root must NOT claim breadcrumbs that belong to a
  nested child root -- the child is its own mount with its own disk pressure and
  its own sweep iteration, so the parent evicting its content both frees the wrong
  filesystem and double-exposes the child's content to the parent's pressure.

Matching here is LEXICAL (``os.path.normpath``; no disk I/O, safe to call inline
from async code): the importer records every breadcrumb literally under the
configured root string, so lexical containment is the correct assignment semantic.
A breadcrumb that only matches a root through a symlink alias resolves to "no
owner" -- which every caller treats as an honest fail-closed refusal/exclusion --
and the realpath-based delete guard (:meth:`LocalFileSystem.resolve_guarded`)
remains the symlink-safe authority at actual purge time.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass

__all__ = ["LibraryRoots", "deepest_containing_root"]


@dataclass(frozen=True)
class LibraryRoots:
    """The four configured library roots, each optional (``None``/empty = unset).

    A plain value object (no I/O) so the web layer can resolve the roots once via
    the ``get_*_root_optional`` dependencies and hand the services a SINGLE typed
    bundle -- keeping "which root plays which role" knowledge out of positional
    string lists that silently lose it.
    """

    movies: str | None = None
    tv: str | None = None
    anime_movie: str | None = None
    anime_tv: str | None = None

    def configured(self) -> tuple[str, ...]:
        """Every non-empty configured root, in declaration order."""
        return tuple(r for r in (self.movies, self.tv, self.anime_movie, self.anime_tv) if r)

    def fallback_for(self, media_type: str, *, is_anime: bool) -> str | None:
        """The media-type-appropriate root for a row with NO breadcrumb to derive
        an owner from: the anime root when the row is anime AND that root is
        configured, else the normal root (matching the import router's own
        placement pick, and the pre-ADR-0015-fix failsafe semantics exactly).
        ``None`` when the applicable root is unset.
        """
        if media_type == "movie":
            return self.anime_movie if is_anime and self.anime_movie else self.movies
        return self.anime_tv if is_anime and self.anime_tv else self.tv


def deepest_containing_root(path: str, roots: Sequence[str]) -> str | None:
    """Return the DEEPEST configured root that lexically contains ``path`` (the
    root itself, or an ancestor of it), or ``None`` when no root contains it.

    "Deepest" = the most path components after ``os.path.normpath`` -- with nested
    configured roots, the most specific one owns the path (see the module
    docstring for why first-match-in-caller-order is wrong there). Two DIFFERENT
    equal-depth roots can never both contain one path (a path has exactly one
    ancestor per depth), so ties only arise between identical strings, where the
    first occurrence wins and equality-comparisons by the caller are unaffected.

    Pure and lexical (``normpath`` + separator-boundary prefix check -- never a
    substring match, so ``/media/movies`` does not claim ``/media/movies2``): no
    disk I/O, safe to call inline from async code. Returns the ORIGINAL root
    string as configured (not its normalized form) so callers can compare it
    directly against their own configured values. Empty ``path`` or empty/blank
    roots never match (fail closed).
    """
    if not path:
        return None
    candidate = os.path.normpath(path)
    best: str | None = None
    best_depth = -1
    for root in roots:
        if not root:
            continue
        root_norm = os.path.normpath(root)
        if candidate != root_norm and not candidate.startswith(root_norm + os.sep):
            continue
        depth = root_norm.count(os.sep)
        if depth > best_depth:
            best = root
            best_depth = depth
    return best
