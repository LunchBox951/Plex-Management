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


def test_downloads_root_unset_defaults_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PLEX_MANAGER_DOWNLOADS_ROOT", raising=False)

    settings = _settings_no_dotenv()

    assert settings.downloads_root is None


def test_downloads_root_reads_the_shared_compose_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issues #133/#157: ``PLEX_MANAGER_DOWNLOADS_ROOT`` is the SAME variable
    docker-compose already uses as the ``/downloads`` bind source (``env_file:
    .env`` hands it to the container too) -- the app must read it, not
    re-derive/duplicate it."""
    monkeypatch.setenv("PLEX_MANAGER_DOWNLOADS_ROOT", "/home/lunchbox/Downloads")

    settings = _settings_no_dotenv()

    assert settings.downloads_root == "/home/lunchbox/Downloads"


def test_downloads_root_blank_env_var_falls_back_to_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PLEX_MANAGER_DOWNLOADS_ROOT", "")

    settings = _settings_no_dotenv()

    assert settings.downloads_root is None


def test_trusted_proxy_hops_defaults_to_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PLEX_MANAGER_TRUSTED_PROXY_HOPS", raising=False)

    settings = _settings_no_dotenv()

    assert settings.trusted_proxy_hops == 0


def test_trusted_proxy_hops_blank_env_var_falls_back_to_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PLEX_MANAGER_TRUSTED_PROXY_HOPS", "")

    settings = _settings_no_dotenv()

    assert settings.trusted_proxy_hops == 0


def test_trusted_proxy_hops_set_env_var_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLEX_MANAGER_TRUSTED_PROXY_HOPS", "1")

    settings = _settings_no_dotenv()

    assert settings.trusted_proxy_hops == 1


def test_uninitialized_boot_needs_no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC1: a fresh install boots with ZERO auth env vars.

    ``Settings()`` constructs (== validates) without raising, and carries no setup
    token — first-run setup is claimed by the first Plex owner to sign in, never
    gated on an env token. The old tokenless-first-run startup refusal is gone.
    """
    monkeypatch.delenv("PLEX_MANAGER_SETUP_TOKEN", raising=False)

    settings = _settings_no_dotenv()  # constructing == validating; must not raise

    assert settings.setup_token is None
