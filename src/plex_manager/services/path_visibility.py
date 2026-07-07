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
    "remap_download_content",
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


def _relative_components(path: str, base: str) -> list[str] | None:
    """``path``'s path components strictly BELOW ``base``, or ``None``.

    ``None`` when ``path`` is not a STRICT descendant of ``base`` -- equal to it, a
    ``..``-escape, or an unrelated tree -- so the caller never anchors a remap on a
    path that isn't actually inside the save directory. Any ``.``/``..`` segment is
    dropped structurally (mirroring :func:`remap_to_visible`) so a crafted remainder
    can never climb back out via ``os.path.join``.
    """
    rel = os.path.relpath(os.path.normpath(path), os.path.normpath(base))
    if (
        rel == os.curdir
        or rel == os.pardir
        or rel.startswith(os.pardir + os.sep)
        or os.path.isabs(rel)
    ):
        return None
    return [c for c in rel.split(os.sep) if c and c not in (".", "..")]


def remap_download_content(
    content: str | None,
    save_path: str | None,
    *,
    candidate_mounts: Sequence[str] | None = None,
) -> str | None:
    """Container-visible remap for a download's CONTENT path, ANCHORED on save_path.

    Unlike :func:`remap_to_visible`'s free longest-first suffix search, this never
    shortens the file's path INDEPENDENTLY of its download directory. The bug that
    forces the anchor: a HOST content ``/srv/qbt/movies/Foo.mkv`` whose real
    container location ``/downloads/movies/Foo.mkv`` is MISSING would, under a free
    suffix search, keep shortening the suffix until the bare ``Foo.mkv`` matched a
    STALE, unrelated ``/downloads/Foo.mkv`` -- validating and PLACING the wrong
    source. A torrent's file position WITHIN its save directory is invariant across
    the host->container bind (docker preserves the subtree below the bind source),
    so only the ``save_path`` prefix may be remapped, never the file below it:

    1. return ``content`` unchanged when it already exists here;
    2. otherwise remap the torrent's ``save_path`` ONCE to a container-visible
       download directory -- its DEEPEST existing suffix under a mount (so a real
       category dir like ``/downloads/movies`` always wins over the mount root and
       the mount-root guess is never reached while a deeper directory exists), or,
       only when NO suffix is a real directory (``save_path`` itself IS the
       download bind-source root, mapped to the mount root with zero suffix), the
       mount root -- and require ``<that dir>/<remainder>`` to exist VERBATIM, where
       ``remainder`` is ``content``'s path below ``save_path``. If it does not
       exist, return ``None`` (an honest, retryable "not visible" block), NEVER a
       shorter-suffix guess.

    Without a ``save_path`` anchor (a stored crash-resume breadcrumb, or a client
    status that carried no save path) there is nothing to anchor to, so ONLY the
    verbatim ``content`` counts (step 1) -- a free suffix search would reintroduce
    exactly the stale-match hazard, so it is deliberately not attempted.

    Download mounts only (never the library mounts): a completed torrent and an old
    library file can share a basename, and content must never place from ``/media``.
    The remainder is always >= 1 component (the caller guarantees ``content`` is
    strictly under ``save_path``), so a torrent never resolves onto the bare
    ``/downloads`` tree. Pure ``exists``/``isdir`` probes (sync); async callers
    offload via ``asyncio.to_thread``.
    """
    if not content:
        return None
    if os.path.exists(content):
        return content
    if not save_path:
        return None
    remainder = _relative_components(content, save_path)
    if not remainder:
        # ``content`` is not strictly under ``save_path`` (equal to it, or an
        # escape): refuse to remap rather than resolve a torrent onto the bare
        # mount tree or an unrelated location.
        return None
    # Read the module global at CALL time (never a def-time default) so a test's
    # ``monkeypatch.setattr(path_visibility, "KNOWN_DOWNLOAD_MOUNTS", ...)`` takes
    # effect, matching this module's documented call-site convention.
    mounts = KNOWN_DOWNLOAD_MOUNTS if candidate_mounts is None else candidate_mounts
    # Remap the save DIRECTORY once: the deepest suffix that is a real directory
    # under a mount. allow_mount_root stays OFF -- that path's basename guard is
    # for library roots; here the mount-root case is handled below with the
    # remainder anchored, so a bare torrent tree is never the answer.
    save_dir = remap_to_visible(save_path, mounts, predicate=os.path.isdir, probe_original=False)
    if save_dir is not None:
        candidate = os.path.join(save_dir, *remainder)
        return candidate if os.path.exists(candidate) else None
    # No suffix of ``save_path`` is a real directory under a mount: ``save_path`` may
    # itself BE the download bind-source root (mapped to the mount root, so zero
    # suffix below it). Anchor the remainder under each mount root -- reached ONLY
    # here, AFTER every deeper directory match has failed, so a real category dir is
    # never bypassed to collapse a deeper file onto a shallow stale one.
    for mount in mounts:
        if mount and os.path.isdir(mount):
            candidate = os.path.join(mount, *remainder)
            if os.path.exists(candidate):
                return candidate
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
