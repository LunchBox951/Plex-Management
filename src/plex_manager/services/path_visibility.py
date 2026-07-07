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

__all__ = [
    "KNOWN_CONTAINER_MOUNTS",
    "KNOWN_DOWNLOAD_MOUNTS",
    "KNOWN_LIBRARY_MOUNTS",
    "remap_library_root",
    "remap_to_visible",
]

#: The volumes ``docker-compose.yml`` maps into the container, split BY PURPOSE
#: (see the "Required: paths selected in setup must be visible inside the
#: container" comment there). The split is load-bearing, not cosmetic: a library
#: root and a completed torrent can share a trailing basename (an old library file
#: ``/media/Foo.mkv`` beside a fresh download ``/downloads/Foo.mkv``), so remapping
#: each KIND of path against ONLY its own mount is what stops a content remap from
#: validating/placing the wrong source, or a Plex library root from being accepted
#: as the ``/downloads`` torrent tree. Referenced module-qualified
#: (``path_visibility.KNOWN_LIBRARY_MOUNTS`` / ``.KNOWN_DOWNLOAD_MOUNTS``) at every
#: call site so tests can ``monkeypatch.setattr(path_visibility, ..., (...))``.
KNOWN_LIBRARY_MOUNTS: Final[tuple[str, ...]] = ("/media",)
KNOWN_DOWNLOAD_MOUNTS: Final[tuple[str, ...]] = ("/downloads",)
#: The union, for the few call sites that legitimately span both namespaces (e.g.
#: the ``trigger_scan`` container->host reverse map, which only needs the set of
#: container mount prefixes to strip). Purpose-scoped callers must use the two
#: lists above, never this union.
KNOWN_CONTAINER_MOUNTS: Final[tuple[str, ...]] = KNOWN_LIBRARY_MOUNTS + KNOWN_DOWNLOAD_MOUNTS


def remap_to_visible(
    path: str | None,
    candidate_mounts: Sequence[str],
    *,
    predicate: Callable[[str], bool] = os.path.isdir,
    probe_original: bool = True,
    allow_mount_root: bool = False,
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
    3. With ``allow_mount_root`` (library roots only), the ZERO-suffix case is
       also tried LAST: a HOST path that IS the bind SOURCE root maps to the
       container mount ROOT itself (docker-compose ``PLEX_MANAGER_MEDIA_ROOT=
       /srv/media`` -> ``/media``, with Plex reporting the whole media root as one
       library at ``/srv/media``). Such a path has no trailing component below the
       mount, so the suffix loop above (which needs >=1 component) can never reach
       it. To keep the honest "``None`` = still not visible" contract -- the mount
       root itself ALWAYS satisfies ``predicate``, so an unconstrained fallback
       would collapse EVERY unresolved path onto it -- the match is accepted only
       when ``path``'s final directory name equals the mount's (the documented
       bind convention, e.g. ``.../media`` -> ``/media``). A differently-named or
       typo'd root still resolves to ``None``. Deliberately OFF by default and
       never enabled for download-content remapping: a torrent must never resolve
       to the whole ``/downloads`` tree.
    4. ``None`` when nothing under any mount satisfies ``predicate`` -- an honest
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
    if allow_mount_root and comps:
        # Zero-suffix (bind-source-root) match, constrained to the mount whose own
        # final name matches this path's -- see step 3. Tried only after every
        # deeper suffix so a real subdirectory match always wins first.
        tail = comps[-1]
        for mount in candidate_mounts:
            if not mount:
                continue
            if os.path.basename(mount.rstrip(os.sep)) == tail and predicate(mount):
                return mount
    return None


def remap_library_root(path: str | None, *, probe_original: bool = True) -> str | None:
    """Remap a submitted/Plex-reported LIBRARY root to a container-visible path.

    THE one policy binding for library roots, shared by ``POST /setup/complete``,
    ``PUT /settings``, and the Plex-location picker so they cannot drift: a library
    root resolves under the LIBRARY mounts ONLY (never ``/downloads`` -- a Plex
    library must never be accepted as the torrent tree) and MAY resolve to a mount
    ROOT itself (``allow_mount_root`` -- a bind-source-root library like
    ``/srv/media`` -> ``/media``). ``probe_original=False`` for the pre-init picker,
    which must never stat the raw, caller-supplied path (pre-auth oracle).
    """
    return remap_to_visible(
        path,
        KNOWN_LIBRARY_MOUNTS,
        probe_original=probe_original,
        allow_mount_root=True,
    )
