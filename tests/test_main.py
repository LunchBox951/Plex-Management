"""Console entrypoint startup safety."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from plex_manager.__main__ import validate_startup_exposure
from plex_manager.config import Settings


def test_default_host_is_loopback() -> None:
    assert Settings().host == "127.0.0.1"


def test_public_bind_without_setup_token_is_rejected() -> None:
    settings = Settings(host="0.0.0.0")  # noqa: S104 - deliberate unsafe bind under test

    with pytest.raises(SystemExit, match="PLEX_MANAGER_SETUP_TOKEN"):
        validate_startup_exposure(settings)


def test_public_bind_with_setup_token_is_allowed() -> None:
    settings = Settings(
        host="0.0.0.0",  # noqa: S104 - deliberate public bind covered by token
        setup_token=SecretStr("boot-token"),
    )

    validate_startup_exposure(settings)
