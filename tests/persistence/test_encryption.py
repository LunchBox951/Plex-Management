"""``EncryptedStr`` behaviour against a live SQLite column."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

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
