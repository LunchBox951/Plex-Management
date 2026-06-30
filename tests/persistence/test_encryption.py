"""``EncryptedStr`` behaviour against a live SQLite column."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession

from plex_manager.adapters import encryption
from plex_manager.config import get_settings
from plex_manager.models import Setting, User

_TOKEN = "plex-token-super-secret-value"  # noqa: S105 — test fixture, not a real secret
_API_KEY = "prowlarr-api-key-abcdef"


async def test_encrypted_str_roundtrips_and_is_ciphertext_at_rest(
    session: AsyncSession,
) -> None:
    user = User(username="alice", encrypted_plex_token=_TOKEN)
    session.add(user)
    await session.flush()
    user_id = user.id

    # A raw SQL read bypasses the TypeDecorator, exposing what is truly stored.
    stored = (
        await session.execute(
            sa.text("SELECT encrypted_plex_token FROM users WHERE id = :id"),
            {"id": user_id},
        )
    ).scalar_one()
    assert stored != _TOKEN
    assert _TOKEN not in stored

    # Going back through the ORM decrypts transparently.
    session.expire(user)
    await session.refresh(user)
    assert user.encrypted_plex_token == _TOKEN


async def test_encrypted_str_passes_none_through(session: AsyncSession) -> None:
    user = User(username="bob", encrypted_plex_token=None)
    session.add(user)
    await session.flush()

    stored = (
        await session.execute(
            sa.text("SELECT encrypted_plex_token FROM users WHERE id = :id"),
            {"id": user.id},
        )
    ).scalar_one()
    assert stored is None

    session.expire(user)
    await session.refresh(user)
    assert user.encrypted_plex_token is None


async def test_setting_encrypted_value_is_ciphertext_at_rest(
    session: AsyncSession,
) -> None:
    setting = Setting(key="prowlarr_api_key", encrypted_value=_API_KEY, is_secret=True)
    session.add(setting)
    await session.flush()

    stored = (
        await session.execute(
            sa.text("SELECT encrypted_value FROM settings WHERE id = :id"),
            {"id": setting.id},
        )
    ).scalar_one()
    assert stored != _API_KEY
    assert _API_KEY not in stored

    session.expire(setting)
    await session.refresh(setting)
    assert setting.encrypted_value == _API_KEY


async def test_decrypt_with_replaced_key_raises_actionable_runtime_error(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ciphertext written under one key then read under a DIFFERENT key must fail
    loudly (honesty over silence): EncryptedStr names the key file in a clear
    RuntimeError instead of silently returning a broken value."""
    # Stored under the key the autouse fixture installed.
    user = User(username="carol", encrypted_plex_token=_TOKEN)
    session.add(user)
    await session.flush()

    # Swap in a DIFFERENT key, as if the operator replaced/lost the original.
    monkeypatch.setenv("PLEX_MANAGER_FERNET_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()
    encryption.reset_fernet_cache()

    session.expire(user)
    with pytest.raises(RuntimeError, match="does not match the data"):
        await session.refresh(user)
