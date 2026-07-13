"""At-rest encryption for secrets (ADR-0005).

Plex tokens and other secrets are stored encrypted in the database. The Fernet
key lives in a file (``<data_dir>/secret.key``), never in the database, so a DB
backup does not implicitly leak it. The key file is auto-generated on first
start; an optional ``PLEX_MANAGER_FERNET_KEY`` override exists for k8s users.

First-run key creation and ordinary key loading are deliberately separate. A
fresh install mints the key via :func:`ensure_secret_key`; an already-running
system loads it via :func:`get_fernet`, which **refuses to mint a replacement**
when the file is gone — a lost key fails loudly (so the operator can restore the
original) instead of silently encrypting new data under a fresh key while the
existing ciphertext becomes permanently undecryptable. :func:`prepare_encryption`
is the startup guard that picks the right path from the ``initialized`` flag.

The key is cached behind a module-level sentinel (not ``lru_cache``) so tests
can reset it with :func:`reset_fernet_cache`. The key itself and any ciphertext
are never logged.
"""

from __future__ import annotations

import contextlib
import errno
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Final

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import String
from sqlalchemy.types import TypeDecorator

from plex_manager.config import Settings, get_settings

if TYPE_CHECKING:
    from sqlalchemy.engine.interfaces import Dialect

__all__ = [
    "EncryptedStr",
    "ensure_secret_key",
    "get_fernet",
    "prepare_encryption",
    "reset_fernet_cache",
    "secret_key_path",
]

logger = logging.getLogger(__name__)

# Module-level sentinel (deliberately not lru_cache) so tests can reset it.
_fernet: Fernet | None = None

_FERNET_KEY_LENGTH: Final = 44  # urlsafe-b64 of 32 bytes (Fernet.generate_key())
_KEY_READ_ATTEMPTS: Final = 50
_KEY_READ_DELAY_SECONDS: Final = 0.02  # ~1s max, rides out an in-flight atomic publish

# Private key tempfile naming (``tempfile.mkstemp``) -- the glob a stale-orphan
# sweep matches, so it must stay in sync with the mkstemp prefix/suffix below.
_KEY_TEMPFILE_PREFIX: Final = ".secret."
_KEY_TEMPFILE_SUFFIX: Final = ".tmp"

# The sibling directory used as the no-hardlink publish mutex, and the age
# bounds for reaping a crashed publisher's leftovers (see
# ``_publish_key_no_hardlink``). All bounds are generous by orders of
# magnitude: a real publish (create + write 44 bytes + fsync) is milliseconds,
# so a LIVE holder's lock/tempfile/partial never looks stale and is never reaped.
_PUBLISH_LOCK_SUFFIX: Final = ".lock"
_STALE_PUBLISH_LOCK_SECONDS: Final = 30.0
_STALE_KEY_TEMPFILE_SECONDS: Final = 60.0
# An INVALID-shaped final (a crashed fallback committer's partial secret.key)
# may be reaped only once it is at least this old -- and only under the publish
# lock, with validity re-checked immediately before the unlink. A VALID key is
# never reaped, at any age (see ``_reap_stale_partial_final``).
_STALE_PARTIAL_FINAL_SECONDS: Final = 30.0
# Safety valve: acquisition normally succeeds at once, or recovers within one
# stale window (~30s). If it makes NO progress for this long the lock is
# un-removable (a stray non-directory at the path, a permissions fault), so fail
# LOUDLY with an actionable error rather than hang forever (north star #3).
_PUBLISH_LOCK_ACQUIRE_TIMEOUT_SECONDS: Final = 120.0

# os.link failures that mean "this filesystem does not support hardlinks at
# all" (exFAT/FAT/SMB mounts are the common self-hosted case: a Pi with
# data_dir on a USB exFAT drive) rather than "the destination already exists"
# (FileExistsError, handled separately). Mirrors
# adapters/filesystem/local.py's _COPY_FALLBACK_ERRNOS (EXDEV is omitted here:
# the tempfile is always created in key_path's own directory, so a
# cross-device error can never occur for this link).
_KEY_LINK_FALLBACK_ERRNOS: Final = frozenset(
    {errno.EPERM, errno.EOPNOTSUPP, errno.EMLINK, errno.EACCES}
)


def _is_valid_fernet_key(raw: bytes) -> bool:
    r"""Whether ``raw`` is a complete, well-shaped Fernet key. Total: never raises.

    Surrounding ASCII whitespace/newlines are tolerated (``raw.strip()``): a
    restored ``secret.key`` often carries a trailing ``\n``/``\r\n`` that Fernet
    ITSELF accepts (its base64 decode ignores non-alphabet bytes), so rejecting it
    on a bare ``len(raw) == 44`` check -- as the prior shape validator did -- turned
    a previously-working install undecryptable-looking after upgrade. Fernet is the
    authority: the stripped core must be exactly 44 chars AND construct a Fernet
    without error, so a key Fernet accepts is never rejected, while genuinely
    truncated/corrupt content (a 43-byte partial, ``tooshort``) still is.
    """
    core = raw.strip()
    if len(core) != _FERNET_KEY_LENGTH:
        return False
    try:
        Fernet(core)  # the ultimate authority on acceptance (binascii.Error <: ValueError)
    except ValueError:
        return False
    return True


def _read_valid_key_once(key_path: Path) -> bytes | None:
    """Read ``key_path`` ONCE and return its CANONICAL key bytes (surrounding
    whitespace stripped), or ``None`` if the file is absent or not a complete Fernet
    key. No retry -- see :func:`_read_valid_key` for the racing-writer retry variant."""
    try:
        raw = key_path.read_bytes()
    except FileNotFoundError:
        return None
    return raw.strip() if _is_valid_fernet_key(raw) else None


def _read_valid_key(key_path: Path) -> bytes | None:
    """Read a COMPLETE, shape-valid key, retrying briefly to ride out a racing
    writer's atomic publish (GHSA-7fhf). Returns the canonical key bytes, or
    ``None`` if none materialized within the bound (absent, or present-but-never-valid).
    """
    for attempt in range(_KEY_READ_ATTEMPTS):
        key = _read_valid_key_once(key_path)
        if key is not None:
            return key
        if attempt + 1 < _KEY_READ_ATTEMPTS:
            time.sleep(_KEY_READ_DELAY_SECONDS)
    return None


def _corrupt_key_error(key_path: Path) -> RuntimeError:
    """Build the actionable error raised when the key file exists but is not a
    valid Fernet key (truncated / corrupt) — never auto-replaced (see
    :func:`_missing_key_error`'s rationale: minting a replacement would orphan
    data encrypted under the original)."""
    return RuntimeError(
        f"Encryption key at {key_path} is present but is not a valid Fernet key "
        "(it may be truncated or corrupt). A replacement is NOT minted automatically, to "
        "avoid orphaning data encrypted under the original key; restore the original key file."
    )


def _fernet_override(settings: Settings) -> str | None:
    """Return the configured key-override plaintext, or ``None`` when unset/empty.

    ``Settings.fernet_key`` is a :class:`~pydantic.SecretStr` so the key never
    leaks through a settings ``repr`` or a log line; the raw value is read here
    via ``.get_secret_value()``. An unset (``None``) or empty override yields
    ``None`` so the caller falls back to the key file — identical to the prior
    truthiness check on the bare string.
    """
    if settings.fernet_key is None:
        return None
    value = settings.fernet_key.get_secret_value()
    return value or None


def secret_key_path(settings: Settings | None = None) -> Path:
    """Return the path to the Fernet key file (``<data_dir>/secret.key``)."""
    settings = settings or get_settings()
    return Path(settings.data_dir) / "secret.key"


def _unbreakable_publish_lock_error(key_path: Path, lock_dir: Path) -> RuntimeError:
    """Build the actionable error raised when the no-hardlink publish lock cannot
    be acquired (a stray non-directory at ``lock_dir``, or a permissions fault --
    never a live holder, which either publishes or ages out well inside the
    timeout). Names the lock so the operator can remove it."""
    return RuntimeError(
        f"Could not create the encryption key at {key_path}: the publish lock "
        f"{lock_dir} could not be acquired and would not clear. Remove it "
        "(it should be an empty directory) and restart; if a valid secret.key "
        "already exists it is used as-is and this lock is irrelevant."
    )


def _missing_key_error(key_path: Path) -> RuntimeError:
    """Build the actionable error raised when the key file is gone."""
    return RuntimeError(
        f"Encryption key not found at {key_path}. At-rest encryption is enabled "
        "but the key file is missing. On a fresh install, complete first-time "
        "setup to generate it; if the system was already initialized, restore "
        "the original key file — encrypted columns cannot be decrypted without "
        "it. A replacement key is NOT minted automatically, to avoid masking "
        "key loss."
    )


def _reap_stale_key_tempfiles(key_dir: Path) -> None:
    """Best-effort removal of orphaned key tempfiles a crashed publish left behind.

    A publish writes the key into a private ``.secret.*.tmp`` and only then makes
    it visible (hardlink or rename); a crash in between orphans that tempfile.
    Only files older than :data:`_STALE_KEY_TEMPFILE_SECONDS` are swept, so a
    concurrent live publish's in-flight tempfile (age ~0) is NEVER removed. Total:
    never raises -- hygiene must not break startup.
    """
    now = time.time()
    try:
        candidates = list(key_dir.glob(f"{_KEY_TEMPFILE_PREFIX}*{_KEY_TEMPFILE_SUFFIX}"))
    except OSError:
        return
    for candidate in candidates:
        try:
            if now - candidate.stat().st_mtime <= _STALE_KEY_TEMPFILE_SECONDS:
                continue
            candidate.unlink()
        except OSError:
            continue


def _reap_stale_publish_lock(lock_dir: Path) -> None:
    """Break the no-hardlink publish lock ONLY if its holder is demonstrably dead.

    A crash between ``os.mkdir`` (acquire) and ``os.rmdir`` (release) would leave
    the lock held forever, so a lock older than :data:`_STALE_PUBLISH_LOCK_SECONDS`
    is removed to let the next startup recover. The bound is generous by orders of
    magnitude: a real publish is milliseconds, so a LIVE holder's lock never looks
    stale and is never broken.

    Removing the lock is safe even against a paused-but-alive holder BECAUSE the
    commit primitive is no-overwrite (:func:`_commit_key_exclusive`): a
    stale-reaped holder that later resumes cannot replace a key published in the
    meantime -- its ``O_EXCL`` commit loses and it converges by adopting the
    published key BY NAME. (The round-3 predecessor committed via ``os.rename``,
    which overwrites; there a reaped-then-resumed holder silently REPLACED the
    published key and two workers returned different keys.) Total: never raises.
    """
    try:
        age = time.time() - lock_dir.stat().st_mtime
    except OSError:
        return
    if age <= _STALE_PUBLISH_LOCK_SECONDS:
        return
    with contextlib.suppress(OSError):
        os.rmdir(os.fspath(lock_dir))


def _commit_key_exclusive(key_path: Path, key: bytes) -> bool:
    """COMMIT primitive of the no-hardlink publish: atomically create the FINAL
    path via ``O_CREAT | O_EXCL`` (atomic + no-overwrite on every filesystem,
    exFAT/FAT/SMB included) and write the complete key + fsync in one shot
    before the fd closes.

    Returns ``False`` when the path already exists -- a racer (or a prior,
    possibly crashed, committer) got there first; the caller converges by
    re-reading the final BY NAME. Unlike an ``os.rename`` commit, a
    paused-then-resumed holder executing this can never replace an
    already-published key: ``O_EXCL`` LOSES instead of clobbering (the round-3
    fencing flaw -- no lock/lease can fence a rename, because rename
    overwrites). ``key_path`` is briefly visible empty between the create and
    the write (microseconds); readers ride that out with their bounded
    validated retries, and a CRASH inside the window leaves an invalid partial
    that :func:`_reap_stale_partial_final` recovers by age on a later
    attempt/startup.
    """
    try:
        fd = os.open(os.fspath(key_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    try:
        with contextlib.suppress(OSError):
            os.fchmod(fd, 0o600)  # defeat the umask; best-effort on perm-less mounts
        view = memoryview(key)
        while view:
            view = view[os.write(fd, view) :]
        os.fsync(fd)
    finally:
        os.close(fd)
    return True


def _reap_stale_partial_final(key_path: Path) -> None:
    """Remove a crashed fallback committer's INVALID partial ``secret.key`` --
    and nothing else. Fires only when the file is BOTH invalid-shaped AND older
    than :data:`_STALE_PARTIAL_FINAL_SECONDS`; validity is re-checked
    immediately before the unlink, so the gap to a racing valid commit is a
    single syscall wide. A VALID key is never reaped, at any age. The caller
    holds the publish lock, serializing this against other reapers and
    committers. Safe pre-init only (no data exists under any key yet) -- which
    is the only phase the publish ever runs in. Total: never raises.
    """
    try:
        if time.time() - key_path.stat().st_mtime <= _STALE_PARTIAL_FINAL_SECONDS:
            return
        raw = key_path.read_bytes()
    except OSError:
        return
    if _is_valid_fernet_key(raw):
        return
    with contextlib.suppress(OSError):
        key_path.unlink()


def _publish_key_no_hardlink(key_path: Path, key: bytes) -> bytes:
    """Publish the first-run key WITHOUT hardlinks (the exFAT/FAT/SMB fallback).

    The COMMIT step is :func:`_commit_key_exclusive` -- an atomic NO-OVERWRITE
    ``O_CREAT|O_EXCL`` create of the final path with the complete key written +
    fsynced in one shot. The previous design committed via ``os.rename``, which
    OVERWRITES: a holder paused past the stale-lock window between its re-check
    and its rename could be stale-reaped, a second worker would publish its own
    key, and the resumed holder's rename silently REPLACED it -- two workers
    returning DIFFERENT first-run keys (the round-3 finding). No lock or lease
    can fence a rename; ``O_EXCL`` loses instead of clobbering, so a zombie
    writer can never replace a published key.

    Convergence rule: the final BY NAME is the single source of truth. Every
    writer -- winner, loser, or resumed zombie -- finishes by re-reading and
    validating ``key_path`` and returning THOSE bytes (never its local buffer),
    so racers always hand back the one canonical key and an existing complete
    key is NEVER replaced.

    The sibling ``<name>.lock`` directory (``os.mkdir``: atomic + no-overwrite
    everywhere) serializes commit attempts and -- more importantly -- the
    recovery unlink of a crashed committer's INVALID partial
    (:func:`_reap_stale_partial_final`: age-gated, validity re-checked
    immediately before the unlink, never a valid key). Readers ride the
    microsecond create-to-write window with their bounded validated retries.

    Pause/reap/crash matrix (worker death, power loss, or a >stale-window pause
    at each step; every case converges or is recoverable on the next
    attempt/startup):

    * crash before ``mkdir``: no lock, no final -> clean first-run next time
      (an orphaned ``.secret.*.tmp`` from the hardlink route is swept by age).
    * crash holding the lock, before the commit: stale lock, no final -> the
      next worker/startup age-reaps the lock and publishes.
    * crash mid-commit (after the ``O_EXCL`` create, before the write/fsync
      completes): an INVALID partial final + a stale lock remain -> the next
      worker/startup age-reaps both and republishes. :func:`ensure_secret_key`
      routes an existing-but-invalid file back into this recovery (pre-init no
      data exists under any key, so re-minting is safe).
    * crash after the commit, before ``rmdir``: the final is complete + valid;
      the orphan lock is harmless (every path adopts a valid final before ever
      contending for the lock).
    * holder PAUSED mid-commit, then stale-reaped: its created-but-still-empty
      final is age-reaped, a second worker publishes, and the zombie's writes
      land in its (now unlinked) inode -- harmless; its by-name re-read then
      adopts the published key. Both workers return the SAME key.
    * holder paused between the partial-reap and its commit (the round-3
      choreography): the second worker publishes; the zombie's ``O_EXCL``
      commit LOSES (``FileExistsError``) and it adopts by name. Same key
      everywhere -- this is the exact interleaving the rename design broke on.
    * double-reap: reapers are serialized by the lock and re-validate the file
      immediately before the unlink. Residual: a reaper paused for a whole
      stale window exactly between that re-validation and its own unlink,
      while a second worker reaps AND publishes inside the pause, could still
      unlink the fresh key -- POSIX has no compare-and-unlink without
      hardlinks. That window is one syscall wide, only armed after a prior
      crash left a >=30s-old partial, and confined to pre-init (no encrypted
      data exists yet); the next read then simply re-mints.
    * reaper crash while holding the lock, partial already unlinked: no final
      + a stale lock -> age-reaped and republished next time.
    """
    lock_dir = key_path.with_name(key_path.name + _PUBLISH_LOCK_SUFFIX)
    deadline = time.monotonic() + _PUBLISH_LOCK_ACQUIRE_TIMEOUT_SECONDS
    while True:
        # Converge first: adopt a key that is already published and complete.
        existing = _read_valid_key_once(key_path)
        if existing is not None:
            return existing
        try:
            os.mkdir(os.fspath(lock_dir), 0o700)
        except FileExistsError:
            # A live holder is mid-commit (ride it out and adopt), or a crashed
            # one left the lock behind (age-reap it, then retry).
            adopted = _read_valid_key(key_path)  # bounded retry: ride out a live commit
            if adopted is not None:
                return adopted
            _reap_stale_publish_lock(lock_dir)
            if time.monotonic() >= deadline:
                raise _unbreakable_publish_lock_error(key_path, lock_dir) from None
            continue
        try:
            _reap_stale_partial_final(key_path)
            if _commit_key_exclusive(key_path, key):
                logger.info("generated new encryption key at %s", key_path)
        finally:
            with contextlib.suppress(OSError):
                os.rmdir(os.fspath(lock_dir))
        # The final BY NAME is the only truth: whether our commit won, lost, or
        # was zombie-reaped mid-write, validate and return what the name holds
        # (the bounded retry rides out a racer's in-flight commit).
        committed = _read_valid_key(key_path)
        if committed is not None:
            return committed
        if time.monotonic() >= deadline:
            raise _corrupt_key_error(key_path)
        # Still no valid final: a racer's YOUNG partial we must not reap yet, or
        # our own commit's file was reaped during a pause. Loop -- age gates
        # every reap; the deadline bounds the wait.


def _probe_hardlink_capability(tmp_name: str) -> bool:
    """Non-destructively determine whether ``tmp_name``'s filesystem supports
    hardlinks, WITHOUT ever touching the (possibly invalid) existing final.

    Links ``tmp_name`` to a throwaway probe path -- derived from ``tmp_name``
    itself, so it is guaranteed fresh and collision-free without a second
    ``mkstemp`` round trip -- in the same directory. A successful link proves
    the filesystem supports hardlinks (the probe is then removed; it was only
    ever a second name for the same, still-untouched tempfile inode). A
    ``_KEY_LINK_FALLBACK_ERRNOS`` ``OSError`` proves the opposite. Any other
    ``OSError`` is a genuine failure and propagates uncaught.

    This MUST run, and be resolved, before any reap of the existing final:
    reaping first and then retrying the real link would make a
    hardlink-CAPABLE filesystem's retry succeed for the wrong reason (the
    blocking file is simply gone), silently replacing operator-restored
    garbage with a fresh key instead of failing loudly.
    """
    probe_name = tmp_name + ".hlprobe"
    try:
        os.link(tmp_name, probe_name)
    except OSError as exc:
        if exc.errno in _KEY_LINK_FALLBACK_ERRNOS:
            return False
        raise
    with contextlib.suppress(OSError):
        os.unlink(probe_name)
    return True


def _recover_invalid_final_after_link_exists(key_path: Path, tmp_name: str, key: bytes) -> bytes:
    """Recover from the primary (hardlink) commit's ``FileExistsError`` when the
    existing final is INVALID (the caller already handles the valid case: adopt
    it as-is, unchanged).

    ``os.link``'s EEXIST check fires before a filesystem's hardlink-support
    check, so this same ``FileExistsError`` -- not the
    ``_KEY_LINK_FALLBACK_ERRNOS`` ``OSError`` -- also occurs on a
    hardlink-refusing data dir (exFAT/FAT/SMB) whenever ``key_path`` already
    holds a crashed fallback committer's invalid partial. That masks the
    ``OSError`` fallback branch that would otherwise route into
    :func:`_publish_key_no_hardlink`'s lock-guarded, age-gated recovery,
    forcing a manual ``secret.key`` deletion to unstick a fresh install.

    Recovery here is a lock-guarded, age-gated attempt -- the wait for the
    lock is bounded by :data:`_PUBLISH_LOCK_ACQUIRE_TIMEOUT_SECONDS`, never
    unbounded -- and, critically, proves hardlink capability BEFORE ever
    reaping anything:

    1. Acquire the publish lock (mirroring :func:`_publish_key_no_hardlink`'s
       own use of it) and call :func:`_probe_hardlink_capability`, which
       hardlinks the tempfile to a throwaway probe path -- never touching
       ``key_path`` -- to find out, non-destructively, whether this
       filesystem supports hardlinks at all.
    2. If the probe SUCCEEDS, the filesystem is genuinely hardlink-capable, so
       a crash could never have left a partial final here: the existing
       invalid file is operator-restored garbage, not a crash artifact, and
       must never be reaped. Re-check the final (a valid key may have
       materialized meanwhile) and either adopt it or fail loudly with the
       corrupt-key error -- exactly as before this recovery path existed
       (never silently replaced, at any age).
    3. If the probe raises the hardlink-fallback ``OSError``, this filesystem
       genuinely refuses hardlinks, so it is now safe to call
       :func:`_reap_stale_partial_final` (age-gated, validity re-checked
       immediately before the unlink) and retry the real ``os.link`` once
       more.
    4. Release the lock, then act on step 2/3's outcome: adopt/fail loud, or
       retry the real link (adopting a racer's win on ``FileExistsError``, or
       delegating to :func:`_publish_key_no_hardlink` on the fallback
       ``OSError`` -- now unmasked because the blocking destination is gone).

    If the publish lock is already held -- a live commit in progress via
    either route, OR a crashed holder's leftover from handling this exact
    invalid final -- this function waits for/reaps it and acquires the lock
    ITSELF (mirroring :func:`_publish_key_no_hardlink`'s own acquire loop)
    rather than delegating straight to :func:`_publish_key_no_hardlink`.
    Delegating on mere lock contention would skip step 1's probe entirely:
    that function reaps an invalid partial unconditionally once it holds the
    lock, with no hardlink-capability check at all, so a stale lock alone
    (e.g. this helper crashed after creating the lock while still deciding
    what to do about an old invalid ``secret.key``) would let a
    hardlink-CAPABLE filesystem's operator-restored invalid key be silently
    reaped and replaced instead of failing loud -- exactly the regression the
    probe exists to prevent. Winning the lock ourselves guarantees the probe
    always runs before any reap, no matter how the lock came to be held.
    """
    lock_dir = key_path.with_name(key_path.name + _PUBLISH_LOCK_SUFFIX)
    deadline = time.monotonic() + _PUBLISH_LOCK_ACQUIRE_TIMEOUT_SECONDS
    while True:
        try:
            os.mkdir(os.fspath(lock_dir), 0o700)
            break
        except FileExistsError:
            # A live holder is mid-commit (ride it out and adopt its result),
            # or a crashed one left the lock behind (age-reap it, then retry
            # acquiring it OURSELVES -- never hand off to
            # _publish_key_no_hardlink here, which would skip the probe).
            adopted = _read_valid_key(key_path)  # bounded retry: ride out a live commit
            if adopted is not None:
                return adopted
            _reap_stale_publish_lock(lock_dir)
            if time.monotonic() >= deadline:
                raise _unbreakable_publish_lock_error(key_path, lock_dir) from None
    try:
        if _probe_hardlink_capability(tmp_name):
            # Proven hardlink-capable WITHOUT touching the existing final:
            # a crash can never leave a partial here, so this is genuine
            # operator-restored garbage -- never reaped, at any age.
            existing = _read_valid_key(key_path)
            if existing is not None:
                return existing
            raise _corrupt_key_error(key_path)
        # Proven hardlink-refusing: only now is it safe to reap.
        _reap_stale_partial_final(key_path)
    finally:
        with contextlib.suppress(OSError):
            os.rmdir(os.fspath(lock_dir))
    try:
        os.link(tmp_name, os.fspath(key_path))
    except FileExistsError:
        existing = _read_valid_key(key_path)
        if existing is not None:
            return existing
        raise _corrupt_key_error(key_path) from None
    except OSError as exc:
        if exc.errno not in _KEY_LINK_FALLBACK_ERRNOS:
            raise
        return _publish_key_no_hardlink(key_path, key)
    logger.info("generated new encryption key at %s", key_path)
    return key


def _generate_key_file(key_path: Path) -> bytes:
    """Atomically mint the first-run key, or re-read an existing one on a lost race.

    A private (mode ``0o600``) tempfile in ``key_path``'s own directory holds the
    key; it is written, flushed, and fsynced BEFORE it is ever named ``key_path``,
    so the inode holds a COMPLETE key at the moment it becomes visible. Publishing
    via ``os.link`` (not ``os.rename``) makes that visibility change atomic AND
    refuses to clobber an existing ``key_path`` — a racing loser's
    ``FileExistsError`` re-reads the winner's key (bounded retry, in case the
    winner's own link is still in flight) rather than truncating it, which would
    orphan the winner's already-cached key and render any data it encrypted
    permanently undecryptable. An existing key is therefore NEVER overwritten.
    The linked file inherits the tempfile's ``0o600`` mode.

    A ``FileExistsError`` with an INVALID existing final is not necessarily a
    race loss, though: ``os.link``'s EEXIST check fires before a filesystem's
    hardlink-support check, so a hardlink-refusing data dir raises this same
    error -- not the fallback ``OSError`` below -- when the final is a crashed
    fallback committer's partial.
    :func:`_recover_invalid_final_after_link_exists` routes that case into the
    same lock-guarded, age-gated recovery the ``OSError`` branch below uses,
    instead of failing outright (issue #149).

    On a filesystem that does not support hardlinks at all (exFAT/FAT/SMB —
    ``os.link`` raises ``OSError`` with an errno in ``_KEY_LINK_FALLBACK_ERRNOS``,
    NOT ``FileExistsError``), delegate to :func:`_publish_key_no_hardlink`, whose
    commit is an atomic no-overwrite ``O_CREAT|O_EXCL`` create of the final path
    (a rename commit is fencible by a stale-reaped-then-resumed holder: rename
    OVERWRITES, so a zombie could replace a racer's already-published key — the
    round-3 finding). The tempfile serves only the hardlink route; the fallback
    writes the final directly and converges by name. Any other ``OSError``
    (e.g. disk full) is a genuine failure and propagates uncaught — it must not
    be papered over as a race loss.
    """
    key = Fernet.generate_key()
    key_path.parent.mkdir(parents=True, exist_ok=True)
    # Clear tempfiles a crashed publish (this run's or a prior one's) may have
    # orphaned; the fresh tempfile below is created AFTER this sweep and is far too
    # young to be caught by its age bound.
    _reap_stale_key_tempfiles(key_path.parent)
    fd, tmp_name = tempfile.mkstemp(
        dir=key_path.parent, prefix=_KEY_TEMPFILE_PREFIX, suffix=_KEY_TEMPFILE_SUFFIX
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(key)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp_name, os.fspath(key_path))
        except FileExistsError:
            # Lost the race: another worker already published the canonical
            # key -- OR (see _recover_invalid_final_after_link_exists) the
            # final is a crashed fallback committer's INVALID partial on a
            # hardlink-refusing filesystem, where this same error fires
            # because EEXIST is checked before hardlink support (issue #149).
            existing = _read_valid_key(key_path)
            if existing is not None:
                return existing
            return _recover_invalid_final_after_link_exists(key_path, tmp_name, key)
        except OSError as exc:
            if exc.errno not in _KEY_LINK_FALLBACK_ERRNOS:
                raise
            return _publish_key_no_hardlink(key_path, key)
        logger.info("generated new encryption key at %s", key_path)
        return key
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)  # drop the temp in every branch -- os.link doesn't consume it


def get_fernet() -> Fernet:
    """Return the process-wide :class:`Fernet`, loading it once. Never creates.

    Loads from the ``PLEX_MANAGER_FERNET_KEY`` override or ``<data_dir>/secret.key``.
    If neither is present, raises an actionable error instead of minting a new
    key — a lost key must fail loudly rather than silently orphan existing
    ciphertext under a fresh, mismatched key.
    """
    global _fernet
    if _fernet is not None:
        return _fernet
    settings = get_settings()
    # An explicit override always wins and is never persisted to the file.
    override = _fernet_override(settings)
    if override is not None:
        _fernet = Fernet(override.encode())
        return _fernet
    key_path = secret_key_path(settings)
    if not key_path.exists():
        raise _missing_key_error(key_path)
    raw = _read_valid_key(key_path)
    if raw is None:
        raise _corrupt_key_error(key_path)
    _fernet = Fernet(raw)
    return _fernet


def ensure_secret_key() -> Fernet:
    """First-run key creation: generate ``<data_dir>/secret.key`` if it is absent.

    Use ONLY before the system is initialized (i.e. no encrypted data exists
    yet). On an already-initialized system call :func:`get_fernet`, which refuses
    to paper over a lost key by minting a replacement.

    An existing VALID key is always adopted as-is. An existing INVALID file is
    routed into :func:`_generate_key_file` rather than rejected outright: on a
    hardlink-less filesystem it can be a crashed fallback commit's partial, and
    pre-init no data exists under any key, so the publish path may recover it
    (adopt a racing writer's completed key, or age-reap the stale partial and
    re-mint) -- including when that filesystem's ``os.link`` masks the crash as
    a ``FileExistsError`` rather than the expected fallback ``OSError``, since
    EEXIST is checked before hardlink support
    (:func:`_recover_invalid_final_after_link_exists`, issue #149). On a
    genuinely hardlink-capable filesystem a crash cannot leave a partial final
    at all, so an invalid file there still fails loudly with the corrupt-key
    error -- never silently replaced, at any age.
    """
    global _fernet
    if _fernet is not None:
        return _fernet
    settings = get_settings()
    override = _fernet_override(settings)
    if override is not None:
        _fernet = Fernet(override.encode())
        return _fernet
    key_path = secret_key_path(settings)
    existing = _read_valid_key_once(key_path)
    if existing is not None:
        _fernet = Fernet(existing)
        return _fernet
    _fernet = Fernet(_generate_key_file(key_path))
    return _fernet


def prepare_encryption(*, initialized: bool) -> Fernet:
    """Startup guard: fail fast on a lost key; mint one only on a fresh install.

    If ``initialized`` is True the key MUST already exist — a missing key aborts
    startup with an actionable error (via :func:`get_fernet`) rather than serving
    undecryptable data. On a fresh install (``initialized`` False) the key is
    generated via :func:`ensure_secret_key`.
    """
    if initialized:
        return get_fernet()
    return ensure_secret_key()


def reset_fernet_cache() -> None:
    """Clear the cached Fernet so the next load reloads it (tests)."""
    global _fernet
    _fernet = None


class EncryptedStr(TypeDecorator[str]):
    """A ``String`` column whose value is Fernet-encrypted at rest.

    The domain never sees cipher bytes: values are encrypted on the way into the
    database and decrypted on the way out. ``None`` is passed through unchanged
    (an empty/absent secret is not encrypted). If the key file is lost or
    replaced, decryption raises a clear, actionable error that names the file
    rather than silently returning a broken value.
    """

    impl = String
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect: Dialect) -> str | None:
        """Encrypt the plaintext on its way into the database."""
        if value is None:
            return None
        return get_fernet().encrypt(value.encode()).decode()

    def process_result_value(self, value: str | None, dialect: Dialect) -> str | None:
        """Decrypt the ciphertext on its way out of the database."""
        if value is None:
            return None
        try:
            return get_fernet().decrypt(value.encode()).decode()
        except InvalidToken as exc:
            key_path = secret_key_path()
            raise RuntimeError(
                "Failed to decrypt a stored secret: the encryption key at "
                f"{key_path} does not match the data. If the key file was lost or "
                "replaced, the encrypted columns cannot be recovered without the "
                "original key."
            ) from exc
