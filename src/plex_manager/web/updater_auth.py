"""Authentication boundary for the private updater coordination API."""

from __future__ import annotations

import hmac
from pathlib import Path
from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from plex_manager.config import Settings, get_settings
from plex_manager.web.errors import AppError

__all__ = ["require_updater"]

_bearer = HTTPBearer(auto_error=False, scheme_name="UpdaterBearer")


def _read_secret(settings: Settings) -> str | None:
    path = settings.updater_secret_file
    if not path:
        return None
    try:
        value = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    # Refuse suspiciously large files before comparison. The random install
    # token is normally 43 characters; this generous cap permits rotation
    # formats without accepting an accidental file/device read.
    return value if 32 <= len(value) <= 512 else None


async def require_updater(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    """Require the dedicated Compose secret; public credentials never fall through."""
    expected = _read_secret(settings)
    if expected is None:
        raise AppError(
            status_code=503,
            code="updater_coordinator_unavailable",
            message="The private updater coordinator is not configured.",
        )
    if (
        credentials is None
        or credentials.scheme.lower() != "bearer"
        or not hmac.compare_digest(credentials.credentials, expected)
    ):
        raise AppError(
            status_code=401,
            code="invalid_updater_credential",
            message="The updater credential was rejected.",
        )
