"""Key-file lifecycle: first-run creation vs. honest failure on a lost key.

These tests bypass the autouse env-override fixture (which injects a throwaway
``PLEX_MANAGER_FERNET_KEY``) by deleting that var and pointing ``data_dir`` at a
temp directory, so the file-based code paths are actually exercised.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from plex_manager.adapters import encryption
from plex_manager.config import Settings, get_settings


@pytest.fixture
def file_backed_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """Force file-based key handling rooted at a temp ``data_dir``."""
    monkeypatch.delenv("PLEX_MANAGER_FERNET_KEY", raising=False)
    monkeypatch.setenv("PLEX_MANAGER_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    encryption.reset_fernet_cache()
    yield tmp_path / "secret.key"
    get_settings.cache_clear()
    encryption.reset_fernet_cache()


def test_get_fernet_raises_when_key_missing(file_backed_key: Path) -> None:
    assert not file_backed_key.exists()
    with pytest.raises(RuntimeError, match="Encryption key not found"):
        encryption.get_fernet()
    # The lost-key path must NOT mint a replacement.
    assert not file_backed_key.exists()


def test_ensure_secret_key_creates_file_with_owner_only_mode(file_backed_key: Path) -> None:
    assert not file_backed_key.exists()
    encryption.ensure_secret_key()
    assert file_backed_key.exists()
    assert (file_backed_key.stat().st_mode & 0o777) == 0o600


def test_ensure_secret_key_does_not_overwrite_existing_key(file_backed_key: Path) -> None:
    # First worker mints the key.
    encryption.ensure_secret_key()
    original = file_backed_key.read_bytes()
    assert original
    # A second ensure_secret_key with the file already present (e.g. a racing
    # worker, or a fresh process) must RE-READ it, never truncate/overwrite — an
    # overwritten key would orphan data the first worker already encrypted.
    encryption.reset_fernet_cache()
    second = encryption.ensure_secret_key()
    assert file_backed_key.read_bytes() == original
    # The reloaded key must decrypt ciphertext produced by the original key.
    token = Fernet(original).encrypt(b"payload")
    assert second.decrypt(token) == b"payload"


def test_get_fernet_loads_an_existing_key(file_backed_key: Path) -> None:
    fernet = encryption.ensure_secret_key()
    token = fernet.encrypt(b"hello")
    encryption.reset_fernet_cache()
    # A fresh load of the same on-disk key decrypts prior ciphertext.
    assert encryption.get_fernet().decrypt(token) == b"hello"


def test_prepare_encryption_fresh_install_generates(file_backed_key: Path) -> None:
    encryption.prepare_encryption(initialized=False)
    assert file_backed_key.exists()


def test_prepare_encryption_initialized_aborts_on_missing_key(file_backed_key: Path) -> None:
    assert not file_backed_key.exists()
    with pytest.raises(RuntimeError, match="Encryption key not found"):
        encryption.prepare_encryption(initialized=True)
    assert not file_backed_key.exists()


def test_fernet_key_is_a_secret_and_never_leaks_in_repr() -> None:
    """The override key is a ``SecretStr`` so it cannot leak via ``repr``/logs."""
    key = Fernet.generate_key().decode()
    settings = Settings(fernet_key=SecretStr(key))
    assert isinstance(settings.fernet_key, SecretStr)
    # Neither the field repr nor the whole-settings repr exposes the plaintext.
    assert key not in repr(settings.fernet_key)
    assert key not in repr(settings)
    assert key not in str(settings)
    # The raw value is still recoverable for the encryption layer.
    assert settings.fernet_key.get_secret_value() == key


def test_secret_override_still_drives_encryption(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``SecretStr`` override is read via ``get_secret_value`` and used as the key."""
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("PLEX_MANAGER_FERNET_KEY", key)
    get_settings.cache_clear()
    encryption.reset_fernet_cache()
    try:
        assert isinstance(get_settings().fernet_key, SecretStr)
        token = encryption.get_fernet().encrypt(b"hi")
        assert Fernet(key.encode()).decrypt(token) == b"hi"
    finally:
        get_settings.cache_clear()
        encryption.reset_fernet_cache()
