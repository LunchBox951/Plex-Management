"""Remap a HOST-namespace path to the CONTAINER-namespace path Plex Manager can
actually see (issues #131/#132/#133).

The deployment contract (``docker-compose.yml``: ``PLEX_MANAGER_MEDIA_ROOT`` ->
``/media``, ``PLEX_MANAGER_DOWNLOADS_ROOT`` -> ``/downloads``) means every path
the operator picks in setup/Settings, or that qBittorrent (running on the HOST)
reports, arrives in the HOST's namespace -- not this container's. A library root
or download path stored/used verbatim then fails every disk probe, import, and
report-issue purge with a confusing "no such file" even though the content is
sitting right there, one mount away.

:func:`remap_to_visible` is the single shared fix: try the path as given, then
suffix-match its trailing path components against the KNOWN container mounts,
returning the first candidate that satisfies ``predicate``. Lexical + a probe
only -- no ``realpath``/symlink resolution (the delete-guard's
``LocalFileSystem.resolve_guarded`` stays the one symlink-safe authority; this
module must never be consulted on a delete path).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

__all__ = ["KNOWN_CONTAINER_MOUNTS", "remap_to_visible"]

#: The volumes ``docker-compose.yml`` maps into the container (see the
#: "Required: paths selected in setup must be visible inside the container"
#: comment there). Referenced module-qualified (``path_visibility.
#: KNOWN_CONTAINER_MOUNTS``) at every call site so tests can
#: ``monkeypatch.setattr(path_visibility, "KNOWN_CONTAINER_MOUNTS", (...))``.
KNOWN_CONTAINER_MOUNTS: Final[tuple[str, ...]] = ("/media", "/downloads")


def remap_to_visible(
    path: str | None,
    candidate_mounts: Sequence[str],
    *,
    predicate: Callable[[str], bool] = os.path.isdir,
    probe_original: bool = True,
) -> str | None:
    """Return a path THIS process can see that corresponds to ``path``, or ``None``.

    1. When ``probe_original`` (the default) and ``predicate(path)`` already
       holds, ``path`` is returned UNCHANGED (the exact string the operator/
       client supplied) -- it is already visible, nothing to remap.
    2. Otherwise, ``path``'s components (after ``normpath``, dropping any ``.``/
       ``..`` segment -- so a crafted path can never lexically escape a mount via
       ``os.path.join``) are tried as a suffix under each of ``candidate_mounts``,
       LONGEST suffix first, then in ``candidate_mounts`` order for ties: the
       first candidate satisfying ``predicate`` wins. This is what lets
       ``/home/Media/Movies`` resolve to ``/media/Movies`` when ``/media`` is the
       configured mount.
    3. ``None`` when nothing under any mount satisfies ``predicate`` -- an honest
       "still not visible", never a guess.

    ``predicate`` defaults to ``os.path.isdir`` (library roots: a fresh root may
    legitimately be empty, so existence is the only bar). Pass
    ``predicate=os.path.exists`` for a download source path (file or dir).
    ``probe_original=False`` skips the step-1 probe of the RAW path -- required
    pre-init, where the path can come from an unauthenticated, caller-supplied
    Plex server and probing it would be a pre-auth local-FS existence oracle.

    Synchronous (``os.path`` stat calls); every async caller offloads via
    ``asyncio.to_thread``.
    """
    if not path:
        return None
    if probe_original and predicate(path):
        return path
    norm = os.path.normpath(path)
    comps = [c for c in norm.split(os.sep) if c and c not in (".", "..")]
    for length in range(len(comps), 0, -1):
        suffix = comps[-length:]
        for mount in candidate_mounts:
            if not mount:
                continue
            candidate = os.path.join(mount, *suffix)
            if predicate(candidate):
                return candidate
    return None
