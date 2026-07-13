"""Key-file lifecycle: first-run creation vs. honest failure on a lost key.

These tests bypass the autouse env-override fixture (which injects a throwaway
``PLEX_MANAGER_FERNET_KEY``) by deleting that var and pointing ``data_dir`` at a
temp directory, so the file-based code paths are actually exercised.
"""

from __future__ import annotations

import errno
import os
import threading
import time
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


def test_generate_key_file_falls_back_on_hardlink_refusing_filesystem(
    file_backed_key: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A filesystem that rejects ``os.link`` outright (e.g. an exFAT/FAT mount
    -- a common self-hosted ``data_dir``, such as a Pi with a USB drive) must
    still complete first-run key creation instead of crashing with an
    uncaught ``OSError``. ``os.link`` raises ``OSError`` with an errno like
    ``EPERM``/``EOPNOTSUPP``/``EMLINK`` here, NOT ``FileExistsError`` -- the
    two must be handled differently."""
    assert not file_backed_key.exists()
    real_link = os.link

    def _refusing_link(src: str, dst: str, **kwargs: object) -> None:
        raise OSError(errno.EPERM, "hardlinks unsupported")

    monkeypatch.setattr(os, "link", _refusing_link)

    result = encryption._generate_key_file(  # pyright: ignore[reportPrivateUsage]
        file_backed_key
    )

    monkeypatch.setattr(os, "link", real_link)
    assert encryption._is_valid_fernet_key(  # pyright: ignore[reportPrivateUsage]
        result
    )
    assert file_backed_key.read_bytes() == result
    assert (file_backed_key.stat().st_mode & 0o777) == 0o600


def test_generate_key_file_hardlink_refusing_fallback_never_overwrites(
    file_backed_key: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On the hardlink-refusing fallback route, a key that already exists
    (another worker won the race) must still never be clobbered -- the
    fallback uses the same ``O_EXCL`` no-overwrite guarantee as the
    pre-hardlink code path."""
    original = Fernet.generate_key()
    file_backed_key.write_bytes(original)

    def _refusing_link(src: str, dst: str, **kwargs: object) -> None:
        raise OSError(errno.EOPNOTSUPP, "hardlinks unsupported")

    monkeypatch.setattr(os, "link", _refusing_link)

    result = encryption._generate_key_file(  # pyright: ignore[reportPrivateUsage]
        file_backed_key
    )

    assert file_backed_key.read_bytes() == original
    assert result == original


def test_generate_key_file_reraises_non_fallback_oserror(
    file_backed_key: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ``os.link`` failure that is NOT a known hardlink-unsupported errno
    (e.g. ``ENOSPC``) is a genuine failure and must propagate, never be
    silently swallowed as if it were a race loss."""

    def _failing_link(src: str, dst: str, **kwargs: object) -> None:
        raise OSError(errno.ENOSPC, "no space left on device")

    monkeypatch.setattr(os, "link", _failing_link)

    with pytest.raises(OSError) as exc_info:
        encryption._generate_key_file(  # pyright: ignore[reportPrivateUsage]
            file_backed_key
        )

    assert exc_info.value.errno == errno.ENOSPC
    assert not file_backed_key.exists()


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


@pytest.mark.parametrize("trailer", [b"\n", b"\r\n", b"  \n", b"\t", b"\r\n\r\n"])
def test_get_fernet_loads_key_with_surrounding_whitespace(
    file_backed_key: Path, trailer: bytes
) -> None:
    """A restored ``secret.key`` frequently carries a trailing newline (an editor
    or ``echo`` adds one) that Fernet ITSELF accepts -- rejecting it on a bare
    ``len == 44`` shape check turned a previously-working install
    undecryptable-looking after upgrade. The loader must tolerate surrounding ASCII
    whitespace/newlines and still decrypt data written under the bare key."""
    key = Fernet.generate_key()
    file_backed_key.write_bytes(key + trailer)

    fernet = encryption.get_fernet()

    token = Fernet(key).encrypt(b"payload")
    assert fernet.decrypt(token) == b"payload"


def test_get_fernet_still_rejects_43_byte_truncation(file_backed_key: Path) -> None:
    """Tolerating surrounding whitespace must NOT weaken the truncation guard: a
    key one byte short of the 44-char encoding is genuinely corrupt (Fernet rejects
    it), so the loader must fail loudly and leave the file untouched."""
    truncated = Fernet.generate_key()[:-1]
    assert len(truncated) == 43
    file_backed_key.write_bytes(truncated)

    with pytest.raises(RuntimeError, match="not a valid Fernet key"):
        encryption.get_fernet()

    assert file_backed_key.read_bytes() == truncated


def test_is_valid_fernet_key_rejects_44_byte_non_base64() -> None:
    """A blob that is EXACTLY 44 bytes but not valid urlsafe-base64 (so Fernet
    rejects it) must fail validation -- the length gate alone is not enough;
    Fernet is the authority that catches corrupt-but-right-length content."""
    corrupt = b"!" * 44  # 44 chars, but '!' is not in the base64 alphabet
    assert len(corrupt) == 44
    assert not encryption._is_valid_fernet_key(corrupt)  # pyright: ignore[reportPrivateUsage]
    # A real 44-char key is accepted, confirming the check is not over-broad.
    assert encryption._is_valid_fernet_key(  # pyright: ignore[reportPrivateUsage]
        Fernet.generate_key()
    )


def test_generate_key_file_fallback_leaves_no_partial_final_file(
    file_backed_key: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The no-hardlink fallback commit is a one-shot ``O_CREAT|O_EXCL`` create +
    complete write + fsync -- atomic no-overwrite, so a resumed zombie can never
    replace a published key; readers ride the microsecond create-to-write window
    with validated retries. After a successful publish the final file is a
    complete, Fernet-valid key and no lock/temp litter remains."""

    def _refusing_link(src: str, dst: str, **kwargs: object) -> None:
        raise OSError(errno.EPERM, "hardlinks unsupported")

    monkeypatch.setattr(os, "link", _refusing_link)

    result = encryption._generate_key_file(file_backed_key)  # pyright: ignore[reportPrivateUsage]

    on_disk = file_backed_key.read_bytes()
    assert on_disk == result
    assert encryption._is_valid_fernet_key(on_disk)  # pyright: ignore[reportPrivateUsage]
    # No leftovers: neither the publish lock directory nor an orphan tempfile.
    assert not file_backed_key.with_name(file_backed_key.name + ".lock").exists()
    assert list(file_backed_key.parent.glob(".secret.*.tmp")) == []


def test_generate_key_file_fallback_recovers_from_crashed_publish_lock(
    file_backed_key: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash between acquiring the publish lock and writing the key leaves the
    sibling ``<name>.lock`` directory behind with NO key. The next startup must reap
    that stale lock (by age) and complete first-run creation, never deadlock behind
    it -- north star #1: a lost key is recoverable without a terminal."""
    stale_lock = file_backed_key.with_name(file_backed_key.name + ".lock")
    stale_lock.mkdir()
    old = time.time() - 3600
    os.utime(stale_lock, (old, old))

    def _refusing_link(src: str, dst: str, **kwargs: object) -> None:
        raise OSError(errno.EOPNOTSUPP, "hardlinks unsupported")

    monkeypatch.setattr(os, "link", _refusing_link)

    result = encryption._generate_key_file(file_backed_key)  # pyright: ignore[reportPrivateUsage]

    assert encryption._is_valid_fernet_key(result)  # pyright: ignore[reportPrivateUsage]
    assert file_backed_key.read_bytes() == result
    assert (file_backed_key.stat().st_mode & 0o777) == 0o600
    assert not stale_lock.exists()  # reaped, then released after publish


def test_generate_key_file_fresh_lock_is_not_reaped(file_backed_key: Path) -> None:
    """A FRESH publish lock (a live holder mid-publish) must never be reaped: only a
    lock older than the stale bound is broken. A young lock with no key yet leaves
    the reaper waiting, so this asserts the age gate holds (the reap is a no-op)."""
    fresh_lock = file_backed_key.with_name(file_backed_key.name + ".lock")
    fresh_lock.mkdir()  # mtime ~now -> well within the stale bound

    encryption._reap_stale_publish_lock(fresh_lock)  # pyright: ignore[reportPrivateUsage]

    assert fresh_lock.exists()  # a live holder's lock is left untouched


def test_generate_key_file_fallback_raises_on_unbreakable_lock(
    file_backed_key: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the publish-lock path is occupied by something ``os.rmdir`` cannot clear
    (a stray FILE here, standing in for a permissions fault), acquisition must fail
    LOUDLY with an actionable error naming the lock -- never hang forever (north
    star #3). The stale-age gate still lets the reaper ATTEMPT the removal, which
    fails on a non-directory; the deadline then converts the non-progress into the
    error instead of an infinite loop."""
    lock_path = file_backed_key.with_name(file_backed_key.name + ".lock")
    lock_path.write_bytes(b"not a directory")
    old = time.time() - 3600  # old enough that the reaper tries (and fails) to rmdir it
    os.utime(lock_path, (old, old))

    def _refusing_link(src: str, dst: str, **kwargs: object) -> None:
        raise OSError(errno.EPERM, "hardlinks unsupported")

    monkeypatch.setattr(os, "link", _refusing_link)
    monkeypatch.setattr(encryption, "_PUBLISH_LOCK_ACQUIRE_TIMEOUT_SECONDS", 0.0)
    monkeypatch.setattr(encryption, "_KEY_READ_ATTEMPTS", 1)  # keep the retry-read instant

    with pytest.raises(RuntimeError, match="publish lock"):
        encryption._generate_key_file(file_backed_key)  # pyright: ignore[reportPrivateUsage]

    # The stray file is untouched (rmdir cannot remove it) and NO key was minted.
    assert lock_path.read_bytes() == b"not a directory"
    assert not file_backed_key.exists()


def test_generate_key_file_concurrent_fallback_agrees_on_one_key(
    file_backed_key: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a hardlink-refusing filesystem, N racing first-run workers must still
    agree on ONE complete key: the ``mkdir`` publish-lock serializes them and a
    loser ADOPTS the winner's key rather than clobbering it (no double-mint)."""

    def _refusing_link(src: str, dst: str, **kwargs: object) -> None:
        raise OSError(errno.EOPNOTSUPP, "hardlinks unsupported")

    monkeypatch.setattr(os, "link", _refusing_link)

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
    assert encryption._is_valid_fernet_key(winner)  # pyright: ignore[reportPrivateUsage]
    assert file_backed_key.read_bytes() == winner
    assert (file_backed_key.stat().st_mode & 0o777) == 0o600


def test_fallback_resumed_holder_cannot_replace_published_key(
    file_backed_key: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-3 fencing regression: worker A acquires the publish lock, passes its
    re-check, then PAUSES past the stale window right before its commit; worker B
    stale-reaps A's lock and publishes key B. When A resumes, its commit must
    LOSE (``O_EXCL`` is no-overwrite -- the prior ``os.rename`` commit REPLACED
    B's published key at exactly this point) and A must converge by adopting B's
    key BY NAME: both workers return the SAME key and the final file equals it."""
    key_a = Fernet.generate_key()
    key_b = Fernet.generate_key()
    lock_dir = file_backed_key.with_name(file_backed_key.name + ".lock")
    real_commit = encryption._commit_key_exclusive  # pyright: ignore[reportPrivateUsage]
    b_returned: list[bytes] = []

    def _paused_then_raced_commit(key_path: Path, key: bytes) -> bool:
        # Worker A is "paused" here, holding a now-stale lock. The world moves on:
        monkeypatch.setattr(encryption, "_commit_key_exclusive", real_commit)
        os.rmdir(lock_dir)  # worker B age-reaps A's stale lock...
        assert real_commit(key_path, key_b)  # ...and publishes ITS OWN key,
        b_returned.append(file_backed_key.read_bytes())  # returning it by name.
        return real_commit(key_path, key)  # A resumes its own commit attempt.

    monkeypatch.setattr(encryption, "_commit_key_exclusive", _paused_then_raced_commit)

    result_a = encryption._publish_key_no_hardlink(  # pyright: ignore[reportPrivateUsage]
        file_backed_key, key_a
    )

    assert b_returned == [key_b]  # B returned the key it published
    assert result_a == key_b  # A converged on the SAME key -- never its own
    assert result_a != key_a
    assert file_backed_key.read_bytes() == key_b  # the published key was NOT replaced


def test_reap_stale_partial_final_age_and_validity_gates(tmp_path: Path) -> None:
    """The partial-final reaper fires ONLY on an invalid-shaped file older than
    the stale bound: a young invalid file (a racer's in-flight commit) is kept,
    an old invalid file (a crashed committer's partial) is removed, and a VALID
    key is never touched at any age."""
    target = tmp_path / "secret.key"
    old = time.time() - 3600

    # Young invalid: kept (could be a live racer mid-commit).
    target.write_bytes(b"partial")
    encryption._reap_stale_partial_final(target)  # pyright: ignore[reportPrivateUsage]
    assert target.exists()

    # Old invalid: reaped (a crashed committer's partial).
    os.utime(target, (old, old))
    encryption._reap_stale_partial_final(target)  # pyright: ignore[reportPrivateUsage]
    assert not target.exists()

    # Old VALID: never reaped, at any age.
    key = Fernet.generate_key()
    target.write_bytes(key)
    os.utime(target, (old, old))
    encryption._reap_stale_partial_final(target)  # pyright: ignore[reportPrivateUsage]
    assert target.read_bytes() == key


def test_generate_key_file_fallback_recovers_crashed_partial_final(
    file_backed_key: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash mid-commit (after the ``O_EXCL`` create, before the write finished)
    leaves an INVALID partial at the final path. Once it is older than the stale
    bound, the next publish attempt must reap it under the lock and mint a fresh
    complete key -- recoverable without a terminal (north star #1)."""
    file_backed_key.write_bytes(b"\x00" * 10)  # a crashed committer's partial
    old = time.time() - 3600
    os.utime(file_backed_key, (old, old))

    def _refusing_link(src: str, dst: str, **kwargs: object) -> None:
        raise OSError(errno.EPERM, "hardlinks unsupported")

    monkeypatch.setattr(os, "link", _refusing_link)

    result = encryption._generate_key_file(file_backed_key)  # pyright: ignore[reportPrivateUsage]

    assert encryption._is_valid_fernet_key(result)  # pyright: ignore[reportPrivateUsage]
    assert file_backed_key.read_bytes() == result
    assert not file_backed_key.with_name(file_backed_key.name + ".lock").exists()


def test_fallback_never_reaps_a_young_partial(
    file_backed_key: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A YOUNG invalid final (a racer's possibly-in-flight commit) must never be
    reaped. With the give-up deadline forced to zero the publish loop raises the
    corrupt-key error instead of ever touching the young file."""
    file_backed_key.write_bytes(b"in-flight")  # young: mtime ~now

    def _refusing_link(src: str, dst: str, **kwargs: object) -> None:
        raise OSError(errno.EOPNOTSUPP, "hardlinks unsupported")

    monkeypatch.setattr(os, "link", _refusing_link)
    monkeypatch.setattr(encryption, "_KEY_READ_ATTEMPTS", 1)
    monkeypatch.setattr(encryption, "_PUBLISH_LOCK_ACQUIRE_TIMEOUT_SECONDS", 0.0)

    with pytest.raises(RuntimeError, match="not a valid Fernet key"):
        encryption._generate_key_file(  # pyright: ignore[reportPrivateUsage]
            file_backed_key
        )

    assert file_backed_key.read_bytes() == b"in-flight"  # untouched


def test_ensure_secret_key_recovers_crashed_partial_on_fallback_fs(
    file_backed_key: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Requirement (c) end-to-end: a crashed fallback commit's stale partial at
    ``secret.key`` must not brick the NEXT STARTUP. Pre-init (no data under any
    key), ``ensure_secret_key`` routes the invalid file into the publish path,
    which age-reaps it and mints a fresh working key."""
    file_backed_key.write_bytes(b"crashed-partial")
    old = time.time() - 3600
    os.utime(file_backed_key, (old, old))

    def _refusing_link(src: str, dst: str, **kwargs: object) -> None:
        raise OSError(errno.EPERM, "hardlinks unsupported")

    monkeypatch.setattr(os, "link", _refusing_link)

    fernet = encryption.ensure_secret_key()

    stored = file_backed_key.read_bytes()
    assert encryption._is_valid_fernet_key(stored)  # pyright: ignore[reportPrivateUsage]
    # The returned Fernet and the stored key agree (round-trip decrypts).
    assert Fernet(stored).decrypt(fernet.encrypt(b"payload")) == b"payload"


def test_ensure_secret_key_still_rejects_invalid_key_on_hardlink_fs(
    file_backed_key: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a hardlink-CAPABLE filesystem a crash cannot leave a partial final at
    all (the ``os.link`` publish is complete-bytes-before-visible), so an invalid
    existing file there is operator-restored garbage: ``ensure_secret_key`` must
    still fail loudly with the corrupt-key error, never silently replace it."""
    file_backed_key.write_bytes(b"tooshort")
    monkeypatch.setattr(encryption, "_KEY_READ_ATTEMPTS", 1)  # keep the retry instant

    with pytest.raises(RuntimeError, match="not a valid Fernet key"):
        encryption.ensure_secret_key()

    assert file_backed_key.read_bytes() == b"tooshort"


def test_ensure_secret_key_still_rejects_old_invalid_key_on_real_hardlink_fs(
    file_backed_key: Path,
) -> None:
    """Regression pin for the reap/probe ordering bug: on a REAL hardlink-capable
    filesystem (no ``os.link`` monkeypatch here -- these tests run on
    ext4/tmpfs, the beta hosts' real filesystem class) an invalid key OLDER
    than the stale-partial bound must still fail loudly and be left completely
    untouched. Age alone must never be sufficient to reap+replace real
    operator-restored garbage; only a filesystem PROVEN (non-destructively) to
    refuse hardlinks may ever recover a crashed partial. Pre-fix, the reap ran
    before the hardlink-capability probe, so this exact case was silently
    reaped and replaced with a fresh key instead of failing loudly."""
    file_backed_key.write_bytes(b"tooshort")
    old = time.time() - 3600
    os.utime(file_backed_key, (old, old))

    with pytest.raises(RuntimeError, match="not a valid Fernet key"):
        encryption.ensure_secret_key()

    assert file_backed_key.read_bytes() == b"tooshort"


def test_probe_hardlink_capability_true_on_real_fs(tmp_path: Path) -> None:
    """The capability probe hardlinks the tempfile to a throwaway sibling path
    and cleans up after itself, without ever touching any other file."""
    tmp_name = str(tmp_path / ".secret.probe-src.tmp")
    Path(tmp_name).write_bytes(b"key-bytes")

    assert encryption._probe_hardlink_capability(  # pyright: ignore[reportPrivateUsage]
        tmp_name
    )

    assert not Path(tmp_name + ".hlprobe").exists()  # probe link removed
    assert Path(tmp_name).exists()  # the tempfile itself is untouched


def test_probe_hardlink_capability_false_when_fs_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``os.link`` raises a known hardlink-unsupported errno, the probe
    reports incapability rather than propagating -- this is the signal that
    makes it safe to reap a crashed partial."""
    tmp_name = str(tmp_path / ".secret.probe-src.tmp")
    Path(tmp_name).write_bytes(b"key-bytes")

    def _refusing_link(src: str, dst: str, **kwargs: object) -> None:
        raise OSError(errno.EPERM, "hardlinks unsupported")

    monkeypatch.setattr(os, "link", _refusing_link)

    assert not encryption._probe_hardlink_capability(  # pyright: ignore[reportPrivateUsage]
        tmp_name
    )


def test_probe_hardlink_capability_reraises_non_fallback_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A genuine failure (e.g. ``ENOSPC``) during the probe must propagate, not
    be swallowed as if it meant "hardlinks unsupported"."""
    tmp_name = str(tmp_path / ".secret.probe-src.tmp")
    Path(tmp_name).write_bytes(b"key-bytes")

    def _failing_link(src: str, dst: str, **kwargs: object) -> None:
        raise OSError(errno.ENOSPC, "no space left on device")

    monkeypatch.setattr(os, "link", _failing_link)

    with pytest.raises(OSError) as exc_info:
        encryption._probe_hardlink_capability(tmp_name)  # pyright: ignore[reportPrivateUsage]

    assert exc_info.value.errno == errno.ENOSPC


def test_reap_stale_key_tempfiles_removes_old_orphans_only(tmp_path: Path) -> None:
    """The stale-tempfile sweep removes a crashed publish's orphaned
    ``.secret.*.tmp`` (by age) but never a concurrent live publish's young
    tempfile."""
    orphan = tmp_path / ".secret.orphan.tmp"
    orphan.write_bytes(b"junk")
    old = time.time() - 3600
    os.utime(orphan, (old, old))
    fresh = tmp_path / ".secret.fresh.tmp"
    fresh.write_bytes(b"new")

    encryption._reap_stale_key_tempfiles(tmp_path)  # pyright: ignore[reportPrivateUsage]

    assert not orphan.exists()
    assert fresh.exists()


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


def _existence_first_link(src: str, dst: str, **kwargs: object) -> None:
    """Test double for a hardlink-refusing filesystem (issue #149).

    A REAL hardlink-refusing mount (exFAT/FAT/SMB) still raises
    ``FileExistsError`` -- not the hardlink-unsupported ``OSError`` -- when
    the destination already exists: the kernel's EEXIST check fires before it
    ever gets to ask whether the filesystem supports hardlinks at all. Only
    once the destination is absent does the "no hardlinks here" ``OSError``
    (``EPERM``/``EOPNOTSUPP``/etc.) surface. Beta hosts are ext4 (genuinely
    hardlink-capable), so this fake is what stands in for that filesystem class
    in tests -- never a real exotic-FS mount.
    """
    if os.path.lexists(dst):
        raise FileExistsError(errno.EEXIST, "File exists", dst)
    raise OSError(errno.EPERM, "hardlinks unsupported", dst)


def test_generate_key_file_recovers_crashed_partial_behind_file_exists_error(
    file_backed_key: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #149: on a hardlink-refusing filesystem, a crashed fallback
    committer's INVALID partial at ``secret.key`` makes the PRIMARY (hardlink)
    path's ``os.link`` raise ``FileExistsError`` -- not the ``OSError`` the
    no-hardlink fallback branch expects -- because existence is checked before
    hardlink support. That must no longer short-circuit straight to the
    corrupt-key error: once the partial is old enough to be provably a crash
    victim (not a live racer), it must be routed into the same lock-guarded
    reap-and-republish recovery the ``OSError`` branch uses, and first-run key
    creation must complete instead of requiring a manual ``secret.key``
    deletion."""
    file_backed_key.write_bytes(b"\x00" * 10)  # a crashed committer's partial
    old = time.time() - 3600
    os.utime(file_backed_key, (old, old))
    monkeypatch.setattr(os, "link", _existence_first_link)

    result = encryption._generate_key_file(file_backed_key)  # pyright: ignore[reportPrivateUsage]

    assert encryption._is_valid_fernet_key(result)  # pyright: ignore[reportPrivateUsage]
    assert file_backed_key.read_bytes() == result
    assert (file_backed_key.stat().st_mode & 0o777) == 0o600
    assert not file_backed_key.with_name(file_backed_key.name + ".lock").exists()


def test_generate_key_file_existence_first_link_still_adopts_valid_key(
    file_backed_key: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No data loss for a valid existing key: on the same hardlink-refusing,
    existence-checked-first filesystem double, a VALID existing key must be
    adopted as-is -- never reaped, never replaced -- even though the primary
    path observes the identical ``FileExistsError`` as the crashed-partial
    case."""
    original = Fernet.generate_key()
    file_backed_key.write_bytes(original)
    old = time.time() - 3600  # old enough to be reap-eligible IF it were invalid
    os.utime(file_backed_key, (old, old))
    monkeypatch.setattr(os, "link", _existence_first_link)

    result = encryption._generate_key_file(file_backed_key)  # pyright: ignore[reportPrivateUsage]

    assert result == original
    assert file_backed_key.read_bytes() == original


def test_generate_key_file_existence_first_link_never_reaps_young_partial(
    file_backed_key: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The age gate still applies under the existence-first fake: a YOUNG
    invalid partial (a possibly-live racer's in-flight commit) must be left
    untouched and the call must fail loudly and PROMPTLY -- no long wait for
    it to age out mid-call."""
    file_backed_key.write_bytes(b"in-flight")  # young: mtime ~now
    monkeypatch.setattr(os, "link", _existence_first_link)
    monkeypatch.setattr(encryption, "_KEY_READ_ATTEMPTS", 1)  # keep retries instant

    with pytest.raises(RuntimeError, match="not a valid Fernet key"):
        encryption._generate_key_file(file_backed_key)  # pyright: ignore[reportPrivateUsage]

    assert file_backed_key.read_bytes() == b"in-flight"  # untouched


def test_ensure_secret_key_recovers_crashed_partial_behind_file_exists_error(
    file_backed_key: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Requirement (c) end-to-end via ``ensure_secret_key`` (the real first-run
    entry point): a stale crashed partial that manifests as ``FileExistsError``
    on the primary path must not brick a fresh install -- no manual
    ``secret.key`` deletion required."""
    file_backed_key.write_bytes(b"crashed-partial")
    old = time.time() - 3600
    os.utime(file_backed_key, (old, old))
    monkeypatch.setattr(os, "link", _existence_first_link)

    fernet = encryption.ensure_secret_key()

    stored = file_backed_key.read_bytes()
    assert encryption._is_valid_fernet_key(stored)  # pyright: ignore[reportPrivateUsage]
    assert Fernet(stored).decrypt(fernet.encrypt(b"payload")) == b"payload"
