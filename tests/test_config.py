"""Settings loading — blank env vars are ignored (``env_ignore_empty``).

A docs-following install copies ``.env.example`` to ``.env``; the optional knobs
there are left blank (e.g. ``PLEX_MANAGER_AUTH_COOKIE_SECURE=``). Without
``env_ignore_empty`` an empty string reaches the field and, for a ``bool | None``
or ``int`` field, fails validation at startup. These tests pin the "blank means
unset -> use the default" behavior so that regression cannot return.

``_env_file=None`` isolates each case from any real ``.env`` in the tree, so the
assertions turn only on the environment variable under test.
"""

from __future__ import annotations

import pytest

from plex_manager.config import Settings


def _settings_no_dotenv() -> Settings:
    """Build ``Settings`` reading ONLY the process env (no ``.env`` file).

    ``_env_file=None`` is a documented pydantic-settings init kwarg, but the
    generated ``Settings.__init__`` signature doesn't advertise it, so pyright
    (strict) can't see it -- hence the local ignore. Disabling the dotenv source
    isolates each case from any real ``.env`` in the tree: with ``env_ignore_empty``
    a blank env var falls through to the NEXT source, and a stray ``.env`` value
    there would otherwise mask the default we're asserting.
    """
    return Settings(_env_file=None)  # pyright: ignore[reportCallIssue]


def test_blank_optional_bool_env_var_infers_none(monkeypatch: pytest.MonkeyPatch) -> None:
    # The exact shape a verbatim ``.env.example`` copy produces for the Secure knob.
    monkeypatch.setenv("PLEX_MANAGER_AUTH_COOKIE_SECURE", "")

    settings = _settings_no_dotenv()  # constructing == validating; must not raise

    assert settings.auth_cookie_secure is None  # falls back to "infer from scheme"


def test_blank_int_env_var_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLEX_MANAGER_PORT", "")

    settings = _settings_no_dotenv()

    assert settings.port == 8000


def test_blank_bool_env_var_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLEX_MANAGER_DEV_AUTH_BYPASS", "")

    settings = _settings_no_dotenv()

    assert settings.dev_auth_bypass is False


def test_set_optional_bool_env_var_still_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    # A non-blank value is unaffected by env_ignore_empty — it still parses.
    monkeypatch.setenv("PLEX_MANAGER_AUTH_COOKIE_SECURE", "true")

    settings = _settings_no_dotenv()

    assert settings.auth_cookie_secure is True
