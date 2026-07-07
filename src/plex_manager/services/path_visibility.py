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
returning the first candidate that satisfies ``predicate``. A KNOWN mount
participates only while it is genuinely a mount point (:func:`is_live_mount`) --
a stock distro's plain ``/media`` directory never counts, so behaviour cannot
silently differ between the container topology and a bare-metal host. Lexical +
a probe only -- no ``realpath``/symlink resolution (the delete-guard's
``LocalFileSystem.resolve_guarded`` stays the one symlink-safe authority; this
module must never be consulted on a delete path).
"""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

__all__ = [
    "KNOWN_CONTAINER_MOUNTS",
    "KNOWN_DOWNLOAD_MOUNTS",
    "KNOWN_LIBRARY_MOUNTS",
    "host_downloads_root_from_mountinfo",
    "is_live_mount",
    "remap_download_content",
    "remap_library_root",
    "remap_to_visible",
    "resolve_downloads_host_root",
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


def is_live_mount(path: str) -> bool:
    """Whether ``path`` counts as one of the app's container mounts RIGHT NOW.

    The ``KNOWN_*`` lists name where docker-compose binds volumes INTO this
    container; a candidate counts only while it is genuinely a MOUNTED volume
    (``os.path.ismount``), never merely an existing directory. The distinction is
    load-bearing for honesty: stock Ubuntu/Debian ship an empty ``/media``
    directory on the root filesystem, so a bare ``isdir`` gate makes every remap
    and picker suggestion ENVIRONMENT-dependent -- a bare-metal (non-Docker)
    install, or a stock CI runner, would treat the distro's empty ``/media`` as
    the app's library mount and offer/accept bogus remaps into it (e.g. the
    zero-suffix bind-root match resolving ``/srv/media`` onto a directory nothing
    is mounted at). In the supported container topology every compose bind/volume
    IS a real mount point, so this gate changes nothing there while keeping every
    other host honest. ``isdir`` + ``ismount`` both swallow ``OSError`` (they
    return ``False``), so an unreadable path is simply not a mount, never a crash.

    Referenced module-qualified by every consumer (including within this module)
    so tests can ``monkeypatch.setattr(path_visibility, "is_live_mount",
    os.path.isdir)`` to let plain ``tmp_path`` directories stand in as mounts.
    """
    return os.path.isdir(path) and os.path.ismount(path)


# Linux's ``/proc/*/mountinfo`` octal-escapes space/tab/backslash/newline inside the
# ``root`` and ``mount point`` fields (the same convention as ``/proc/mounts``), so a
# bind source containing one of those bytes round-trips correctly instead of being
# read as a truncated/garbled path.
_MOUNTINFO_OCTAL_ESCAPE: Final = re.compile(r"\\([0-7]{3})")


def _unescape_mountinfo_field(value: str) -> str:
    return _MOUNTINFO_OCTAL_ESCAPE.sub(lambda m: chr(int(m.group(1), 8)), value)


def host_downloads_root_from_mountinfo(container_mount: str = "/downloads") -> str | None:
    """Best-effort HOST-namespace directory backing ``container_mount``.

    The fallback source (design order #2, after ``PLEX_MANAGER_DOWNLOADS_ROOT``) for
    the value handed to qBittorrent's ``save_path`` on ``add`` (issues #133/#157):
    qBittorrent runs on the HOST, so the value must stay in the HOST namespace -- the
    one function in this module that does NOT resolve to a container-visible path;
    every other helper here answers "what can THIS process see", this one answers
    "what does the HOST call the directory THIS process sees at ``container_mount``".

    Reads THIS process's own ``/proc/self/mountinfo`` and returns the ``root`` field
    (the path within the mount's source filesystem that corresponds to
    ``container_mount``) of the LAST line whose mount point matches -- mount stacking
    means a later bind at the same point shadows an earlier one, and mountinfo lists
    mounts in mount order, so the last match is the CURRENT one. Octal escapes in
    either field are decoded (the kernel escapes space/tab/backslash/newline in both).

    Gated on :func:`is_live_mount`: a bare-metal (non-Docker) process has no
    ``/downloads`` mount at all, so this returns ``None`` without even opening
    ``/proc/self/mountinfo`` -- the same "only ever act on a genuinely mounted
    volume" honesty the rest of the module holds to. Any I/O failure (missing
    ``/proc``, permission, an unparsable line) is swallowed to ``None`` -- an honest
    "could not derive", never a crash or a guessed path; callers fall back to
    qBittorrent's own default (unchanged prior behaviour).
    """
    if not is_live_mount(container_mount):
        return None
    norm_target = os.path.normpath(container_mount)
    best: str | None = None
    try:
        with open("/proc/self/mountinfo", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                fields = line.split(" ")
                try:
                    separator_index = fields.index("-")
                except ValueError:
                    continue  # malformed line (no optional-fields terminator): skip
                # Fields, in order: mount ID, parent ID, major:minor, root, mount
                # point, mount options, then >=0 optional fields, then "-". The
                # terminator can therefore never appear before index 6.
                if separator_index < 6:
                    continue
                root, mount_point = fields[3], fields[4]
                if not root or not mount_point:
                    continue
                if os.path.normpath(_unescape_mountinfo_field(mount_point)) == norm_target:
                    best = _unescape_mountinfo_field(root)
    except OSError:
        return None
    return best or None


def resolve_downloads_host_root(configured: str | None) -> str | None:
    """The HOST-namespace downloads root to direct qBittorrent's ``save_path`` at.

    ``configured`` (``Settings.downloads_root`` / ``PLEX_MANAGER_DOWNLOADS_ROOT`` --
    the SAME variable docker-compose already uses as the ``/downloads`` bind source;
    ``env_file: .env`` hands it to this container too, just unread by the app until
    now) wins when set -- computed by the operator's own compose file, never
    operator-typed a second time. Falls back to
    :func:`host_downloads_root_from_mountinfo` (source order #2) when unset.
    ``None`` when NEITHER resolves (bare metal, no Docker split, or a container whose
    mountinfo can't be read): callers then leave qBittorrent's own default in
    charge -- an honest "could not derive", never a guessed path.
    """
    if configured:
        return configured
    return host_downloads_root_from_mountinfo()


def _is_under(norm_path: str, mount: str) -> bool:
    """Whether normalized ``norm_path`` lexically equals or sits under ``mount``."""
    norm_mount = os.path.normpath(mount)
    return norm_path == norm_mount or norm_path.startswith(norm_mount + os.sep)


def remap_to_visible(
    path: str | None,
    candidate_mounts: Sequence[str],
    *,
    predicate: Callable[[str], bool] = os.path.isdir,
    probe_original: bool = True,
    allow_mount_root: bool = False,
) -> str | None:
    """Return a path THIS process can see that corresponds to ``path``, or ``None``.

    1. A ``path`` already lexically UNDER one of the live mounts is treated as a
       mounted path AS-IS: returned unchanged when ``predicate(path)`` holds,
       ``None`` when it doesn't -- NEVER suffix-probed deeper. Without this, an
       already-container-visible ``/media/Movies`` would enter the longest-first
       suffix search and could resolve to a nested ``/media/media/Movies`` when
       one happens to exist. This probe runs even with ``probe_original=False``:
       probing a path under the app's OWN mounts is never a remote-server
       oracle -- the pre-auth guard below is only about ARBITRARY raw paths.
    2. When ``probe_original`` (the default) and ``predicate(path)`` already
       holds, ``path`` is accepted (the exact string the operator/client
       supplied) -- but a visible path lying OUTSIDE every live mount can be a
       pre-fix PHANTOM (the old importer ``os.makedirs``-ed host-shaped trees
       like ``/home/Media/Movies`` inside this container), so when the same
       suffix ALSO resolves inside a live mount, the MOUNTED candidate is
       preferred (steps 3-4 run first and win); the outside-mount original is
       returned only when no mounted candidate exists (an operator's legitimate
       EXTRA volume at a custom path). With ZERO live mounts (bare metal, no
       Docker split) the original is accepted as-is, unchanged.
    3. Otherwise, ``path``'s components (after ``normpath``, dropping any ``.``/
       ``..`` segment -- so a crafted path can never lexically escape a mount via
       ``os.path.join``) are tried as a suffix under each live mount, LONGEST
       suffix first, then in ``candidate_mounts`` order for ties: the first
       candidate satisfying ``predicate`` wins. This is what lets
       ``/home/Media/Movies`` resolve to ``/media/Movies`` when ``/media`` is the
       configured mount.
    4. With ``allow_mount_root`` (library roots only), the ZERO-suffix case is
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
    5. ``None`` when nothing matched -- an honest "still not visible", never a
       guess.

    A candidate mount participates only while :func:`is_live_mount` holds for it
    -- a genuinely MOUNTED volume, not merely an existing directory. Without that
    gate the behaviour is environment-dependent: stock Ubuntu/Debian ship an
    empty ``/media`` directory, so a bare-metal (non-Docker) install would have
    its unresolvable paths remapped onto a directory nothing is mounted at (most
    sharply via the ``allow_mount_root`` case, whose only other bar is a
    basename match).

    ``predicate`` defaults to ``os.path.isdir`` (library roots: a fresh root may
    legitimately be empty, so existence is the only bar). Pass
    ``predicate=os.path.exists`` for a download source path (file or dir).
    ``probe_original=False`` skips the step-2 probe of an arbitrary RAW path --
    required pre-init, where the path can come from an unauthenticated,
    caller-supplied Plex server and probing it would be a pre-auth local-FS
    existence oracle (step 1's under-OUR-OWN-mount probe is exempt, see above).

    Synchronous (``os.path`` stat calls); every async caller offloads via
    ``asyncio.to_thread``.
    """
    if not path:
        return None
    # Module-global lookup at CALL time (tests monkeypatch path_visibility.
    # is_live_mount to let tmp dirs stand in as mounts); probed once per call,
    # before the per-suffix loop, so each mount costs two stats total.
    live_mounts = [m for m in candidate_mounts if m and is_live_mount(m)]
    norm = os.path.normpath(path)
    # Step 1: already under one of OUR mounts -> a mounted path as-is, never
    # suffix-probed deeper (no /media/media/Movies nesting).
    for mount in live_mounts:
        if _is_under(norm, mount):
            return path if predicate(path) else None
    original_visible = probe_original and predicate(path)
    if not live_mounts:
        # Bare metal (no Docker split): the visible path is the truth, and with
        # no mounts there is nothing to remap against.
        return path if original_visible else None
    comps = [c for c in norm.split(os.sep) if c and c not in (".", "..")]
    for length in range(len(comps), 0, -1):
        suffix = comps[-length:]
        for mount in live_mounts:
            candidate = os.path.join(mount, *suffix)
            if predicate(candidate):
                return candidate
    if allow_mount_root and comps:
        # Zero-suffix (bind-source-root) match, constrained to the mount whose own
        # final name matches this path's -- see step 4. Tried only after every
        # deeper suffix so a real subdirectory match always wins first.
        tail = comps[-1]
        for mount in live_mounts:
            if os.path.basename(mount.rstrip(os.sep)) == tail and predicate(mount):
                return mount
    if original_visible:
        # Visible, outside every live mount, and no mounted candidate shadows it:
        # honestly accept it (an operator's extra volume at a custom path). A
        # phantom that DID have a mounted twin was preferred away above (step 2).
        return path
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


def _proves_content(
    anchor_dir: str,
    remainder: Sequence[str],
    expected_files: Sequence[tuple[str, int]],
) -> bool:
    """PROOF that ``anchor_dir`` really is this torrent's remapped save directory.

    ``expected_files`` is the torrent's OWN file list from the download client
    (each entry: path relative to the save path + exact byte size). The candidate
    interpretation is proven only when, among the entries under ``remainder``
    (the content's own subtree), at least ONE exists at its exact relative
    location with its EXACT size -- and NONE exists there with a DIFFERENT size
    (a same-name-different-size file is a stale/unrelated tree, an immediate
    disproof). An absent entry is neutral: a deselected (priority-0) torrent file
    legitimately never materializes on disk, so absence is neither proof nor
    contradiction. No entries under ``remainder`` at all -> not proven.

    This is what lets the bind-source-root save-path topology keep working
    (``save_path`` IS the bind source, so the mount root is the correct
    interpretation) WITHOUT guessing: the mount root qualifies only by exhibiting
    the torrent's own named-and-sized payload, never by mere existence of a
    same-named file.
    """
    witnessed = False
    for name, size in expected_files:
        comps = [c for c in name.split("/") if c and c not in (".", "..")]
        if not comps or comps[: len(remainder)] != list(remainder):
            continue  # a torrent file outside the resolved content subtree
        try:
            actual = os.path.getsize(os.path.join(anchor_dir, *comps))
        except OSError:
            continue  # absent (e.g. a deselected file): neutral, keep looking
        if actual != size:
            return False  # same name, wrong size: a stale/unrelated tree
        witnessed = True
    return witnessed


def remap_download_content(
    content: str | None,
    save_path: str | None,
    expected_files: Sequence[tuple[str, int]],
    *,
    candidate_mounts: Sequence[str] | None = None,
) -> str | None:
    """Container-visible remap for a download's CONTENT path: anchored + PROVEN.

    Unlike :func:`remap_to_visible`'s free longest-first suffix search, this never
    shortens the file's path INDEPENDENTLY of its download directory, and it never
    accepts a remapped candidate on bare existence. The bugs that force this: a
    HOST content ``/srv/qbt/movies/Foo.mkv`` whose real container location
    ``/downloads/movies/Foo.mkv`` is MISSING would, under a free suffix search,
    keep shortening the suffix until the bare ``Foo.mkv`` matched a STALE,
    unrelated ``/downloads/Foo.mkv`` -- validating and PLACING the wrong source;
    and an existence-only bind-root fallback could do the same whenever no deeper
    save directory matched. A torrent's file position WITHIN its save directory is
    invariant across the host->container bind (docker preserves the subtree below
    the bind source), so only the ``save_path`` prefix may be remapped, never the
    file below it -- and the winning interpretation must carry PROOF:

    1. return ``content`` unchanged when it already exists here (the
       same-namespace fast path -- no remap happened, so no proof is needed);
    2. otherwise remap the torrent's ``save_path`` ONCE to a container-visible
       download directory -- its DEEPEST existing suffix under a live mount (a
       real category dir like ``/downloads/movies`` always wins over the mount
       root), or, only when NO suffix is a real directory, the mount root itself
       (``save_path`` IS the download bind-source root -- the documented compose
       topology). Either interpretation is accepted ONLY on
       :func:`_proves_content`: the torrent's own file list (relative path +
       exact size, from the download client) must be exhibited at the candidate
       location. No proof -> ``None`` (an honest, retryable "not visible /
       content mismatch" block), NEVER an existence-only guess.

    Without a ``save_path`` anchor (a stored crash-resume breadcrumb, or a client
    status that carried no save path) there is nothing to anchor to, so ONLY the
    verbatim ``content`` counts (step 1) -- a free suffix search would reintroduce
    exactly the stale-match hazard, so it is deliberately not attempted.

    Download mounts only (never the library mounts): a completed torrent and an
    old library file can share a basename, and content must never place from
    ``/media``. The remainder is always >= 1 component (``content`` must be
    strictly under ``save_path``), so a torrent never resolves onto the bare
    ``/downloads`` tree. Pure stat probes (sync); async callers offload via
    ``asyncio.to_thread``.
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
    # under a live mount. allow_mount_root stays OFF -- the bind-root case is the
    # separate, proof-gated interpretation below, so a bare torrent tree can never
    # be the direct answer of this search.
    save_dir = remap_to_visible(save_path, mounts, predicate=os.path.isdir, probe_original=False)
    if save_dir is not None:
        # The deepest-directory interpretation is the ONLY one tried when it
        # exists -- no fallthrough to the bind-root guess on a failed proof, so a
        # same-named (even same-sized) stray at the mount root can never shadow a
        # genuinely-missing file under the real category directory.
        if _proves_content(save_dir, remainder, expected_files):
            return os.path.join(save_dir, *remainder)
        return None
    # No suffix of ``save_path`` is a real directory under a live mount: the one
    # remaining legitimate topology is that ``save_path`` IS the download
    # bind-source root (mapped to the mount root, zero suffix below it). That
    # interpretation must PROVE itself via the torrent's own file list -- never
    # mere existence of a same-named file (the round-3 finding).
    for mount in mounts:
        if mount and is_live_mount(mount) and _proves_content(mount, remainder, expected_files):
            return os.path.join(mount, *remainder)
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
