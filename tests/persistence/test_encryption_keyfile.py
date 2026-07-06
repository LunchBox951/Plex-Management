"""Key-file lifecycle: first-run creation vs. honest failure on a lost key.

These tests bypass the autouse env-override fixture (which injects a throwaway
``PLEX_MANAGER_FERNET_KEY``) by deleting that var and pointing ``data_dir`` at a
temp directory, so the file-based code paths are actually exercised.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
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


def test_generate_key_file_never_overwrites_existing_key(file_backed_key: Path) -> None:
    """GHSA-7fhf: the atomic-publish rewrite uses ``os.link`` (not ``os.rename``),
    which refuses to clobber an existing ``key_path`` -- calling
    ``_generate_key_file`` again against an already-present key must leave it
    byte-identical and hand back the EXISTING key, never a fresh one."""
    original = Fernet.generate_key()
    file_backed_key.write_bytes(original)

    result = encryption._generate_key_file(  # pyright: ignore[reportPrivateUsage]
        file_backed_key
    )

    assert file_backed_key.read_bytes() == original
    assert result == original


def test_concurrent_first_run_agrees_on_one_complete_key(file_backed_key: Path) -> None:
    """The barrier race GHSA-7fhf fixes: N workers all observe a fresh install
    and race to mint the key. Every worker must agree on the SAME complete key
    (no reader ever observes a partial/truncated write), and the published file
    keeps the private 0600 mode."""
    workers = 8
    barrier = threading.Barrier(workers)

    def _worker() -> bytes:
        barrier.wait()
        return encryption._generate_key_file(  # pyright: ignore[reportPrivateUsage]
            file_backed_key
        )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_worker) for _ in range(workers)]
        results = [future.result() for future in futures]

    assert len(set(results)) == 1  # every worker agrees on ONE key
    (winner,) = set(results)
    assert encryption._is_valid_fernet_key(  # pyright: ignore[reportPrivateUsage]
        winner
    )
    assert file_backed_key.read_bytes() == winner
    assert (file_backed_key.stat().st_mode & 0o777) == 0o600


def test_get_fernet_rejects_truncated_key(file_backed_key: Path) -> None:
    """A present-but-truncated key file must fail loudly (never silently mint a
    replacement, which would orphan data encrypted under the original) and must
    never be overwritten by the failed load attempt."""
    file_backed_key.write_bytes(b"tooshort")

    with pytest.raises(RuntimeError, match="not a valid Fernet key"):
        encryption.get_fernet()

    assert file_backed_key.read_bytes() == b"tooshort"


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
