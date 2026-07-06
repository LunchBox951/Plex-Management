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

import base64
import binascii
import contextlib
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
_FERNET_KEY_RAW_LENGTH: Final = 32
_KEY_READ_ATTEMPTS: Final = 50
_KEY_READ_DELAY_SECONDS: Final = 0.02  # ~1s max, rides out an in-flight atomic publish


def _is_valid_fernet_key(raw: bytes) -> bool:
    """Whether ``raw`` is a complete, well-shaped Fernet key. Total: never raises."""
    if len(raw) != _FERNET_KEY_LENGTH:
        return False
    try:
        return len(base64.urlsafe_b64decode(raw)) == _FERNET_KEY_RAW_LENGTH
    except (binascii.Error, ValueError):
        return False


def _read_valid_key(key_path: Path) -> bytes | None:
    """Read a COMPLETE, shape-valid key, retrying briefly to ride out a racing
    writer's atomic publish (GHSA-7fhf). Returns the key bytes, or ``None`` if
    none materialized within the bound (absent, or present-but-never-valid).
    """
    for attempt in range(_KEY_READ_ATTEMPTS):
        try:
            raw = key_path.read_bytes()
        except FileNotFoundError:
            raw = b""
        if _is_valid_fernet_key(raw):
            return raw
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
    """
    key = Fernet.generate_key()
    key_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=key_path.parent, prefix=".secret.", suffix=".tmp")
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
