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
# ``_publish_key_no_hardlink``). Both bounds are generous by orders of
# magnitude: a real publish (write 44 bytes + fsync + rename) is milliseconds,
# so a LIVE holder's lock/tempfile never looks stale and is never reaped.
_PUBLISH_LOCK_SUFFIX: Final = ".lock"
_STALE_PUBLISH_LOCK_SECONDS: Final = 30.0
_STALE_KEY_TEMPFILE_SECONDS: Final = 60.0
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

    Removing the lock is safe even under a reaper race: the caller only reaps when
    NO valid key is currently visible (i.e. a fresh install), so at worst two
    workers re-mint DIFFERENT first-run keys before any data exists to orphan -- an
    ALREADY-ESTABLISHED key is never reached here (the fast-path and retry reads
    adopt it first, so the lock is never even contended in that case). Total: never
    raises.
    """
    try:
        age = time.time() - lock_dir.stat().st_mtime
    except OSError:
        return
    if age <= _STALE_PUBLISH_LOCK_SECONDS:
        return
    with contextlib.suppress(OSError):
        os.rmdir(os.fspath(lock_dir))


def _publish_key_no_hardlink(key_path: Path, key: bytes, tmp_name: str) -> bytes:
    r"""Publish the first-run key WITHOUT hardlinks (the exFAT/FAT/SMB fallback).

    ``tmp_name`` already holds ``key`` (written + fsynced by the caller). The final
    ``key_path`` is made visible ONLY by atomically renaming ``tmp_name`` into
    place, so a reader NEVER observes a partial final file and a crash can NEVER
    leave a truncated ``secret.key``. The prior fallback (``O_CREAT|O_EXCL`` AT the
    final path, then write the bytes) recreated exactly that zero-byte/partial
    window -- on the very filesystems this fallback serves -- and a crash inside it
    left a corrupt ``secret.key`` that later startups refuse to replace.

    A sibling directory (``<name>.lock``) is the publish mutex: ``os.mkdir`` is
    atomic and no-overwrite on EVERY filesystem (including the hardlink-less ones),
    so exactly one worker publishes at a time and an existing key is NEVER
    clobbered. A racing loser rides out the winner's in-flight publish with the
    same bounded, validated read-retry readers use, then adopts the winner's key. A
    lock left by a CRASHED holder is reaped by age (:func:`_reap_stale_publish_lock`)
    so a half-finished publish is recoverable on the next startup. Returns the key
    it published, or an already-present one it adopted.

    Crash matrix (process death / power loss at each step; the next startup
    recovers cleanly every time -- no partial final file is ever exposed):

    * before ``mkdir``: only an orphan ``.secret.*.tmp`` (swept by age); no lock,
      no ``key_path`` -> next startup is a clean first-run.
    * after ``mkdir``, before ``rename``: an empty lock + an orphan tempfile, no
      ``key_path`` -> next startup reaps the stale lock (age) and the stale
      tempfile (age), then publishes fresh.
    * during ``rename``: atomic -> ``key_path`` is either fully present (adopted)
      or absent (as the previous case). Never partial.
    * after ``rename``, before ``rmdir``: ``key_path`` is complete + valid; the
      orphan lock is harmless (the normal load path reads the key directly and
      never contends for the lock).
    """
    # Fast path: a complete key already exists -> adopt it, never overwrite, no lock.
    existing = _read_valid_key_once(key_path)
    if existing is not None:
        return existing
    lock_dir = key_path.with_name(key_path.name + _PUBLISH_LOCK_SUFFIX)
    deadline = time.monotonic() + _PUBLISH_LOCK_ACQUIRE_TIMEOUT_SECONDS
    while True:
        try:
            os.mkdir(os.fspath(lock_dir), 0o700)
        except FileExistsError:
            # A concurrent publisher holds the lock (live), or a crashed one left it.
            adopted = _read_valid_key(key_path)  # bounded retry: ride out a live publish
            if adopted is not None:
                return adopted
            # No key yet: break the lock ONLY if its holder is demonstrably dead,
            # then retry acquiring (a live holder's fresh lock is left untouched and
            # we simply keep waiting for it to publish or age out).
            _reap_stale_publish_lock(lock_dir)
            if time.monotonic() >= deadline:
                raise _unbreakable_publish_lock_error(key_path, lock_dir) from None
            continue
        break  # acquired the publish lock
    try:
        # Under the lock a prior holder may have JUST published; never clobber it.
        # No in-flight partial is possible here (publish is atomic-rename), so a
        # single read settles it -- no retry needed.
        published = _read_valid_key_once(key_path)
        if published is not None:
            return published
        os.rename(os.fspath(tmp_name), os.fspath(key_path))
        logger.info("generated new encryption key at %s", key_path)
        return key
    finally:
        with contextlib.suppress(OSError):
            os.rmdir(os.fspath(lock_dir))


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

    On a filesystem that does not support hardlinks at all (exFAT/FAT/SMB —
    ``os.link`` raises ``OSError`` with an errno in ``_KEY_LINK_FALLBACK_ERRNOS``,
    NOT ``FileExistsError``), delegate to :func:`_publish_key_no_hardlink`, which
    publishes the fsynced tempfile via a lock-serialized ATOMIC RENAME so the final
    path is still same-no-overwrite and complete-bytes-before-visible (never a
    zero-byte/partial ``secret.key``), just without the hardlink. Any other
    ``OSError`` (e.g. disk full) is a genuine failure and propagates uncaught —
    it must not be papered over as a race loss.
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
            # Lost the race: another worker already published the canonical key.
            existing = _read_valid_key(key_path)
            if existing is None:
                raise _corrupt_key_error(key_path) from None
            return existing
        except OSError as exc:
            if exc.errno not in _KEY_LINK_FALLBACK_ERRNOS:
                raise
            return _publish_key_no_hardlink(key_path, key, tmp_name)
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
    if key_path.exists():
        raw = _read_valid_key(key_path)
        if raw is None:
            raise _corrupt_key_error(key_path)
        _fernet = Fernet(raw)
    else:
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
