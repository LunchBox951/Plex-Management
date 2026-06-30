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

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

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
    """Mint a fresh key and persist it with owner-only permissions.

    Using ``os.open`` with an explicit mode avoids a window where the file is
    world-readable.
    """
    key = Fernet.generate_key()
    key_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(key)
    logger.info("generated new encryption key at %s", key_path)
    return key


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
    if settings.fernet_key:
        _fernet = Fernet(settings.fernet_key.encode())
        return _fernet
    key_path = secret_key_path(settings)
    if not key_path.exists():
        raise _missing_key_error(key_path)
    _fernet = Fernet(key_path.read_bytes())
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
    if settings.fernet_key:
        _fernet = Fernet(settings.fernet_key.encode())
        return _fernet
    key_path = secret_key_path(settings)
    if key_path.exists():
        _fernet = Fernet(key_path.read_bytes())
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
