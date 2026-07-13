"""Guard the single version source of truth (issue #114).

``plex_manager.__version__`` is the one place the release version is written;
hatch reads it for the package build, FastAPI surfaces it as OpenAPI
``info.version``, and ``events.current_build_id()`` falls back to it. These
tests do not pin a specific value (no release has been promoted yet -- see
CHANGELOG.md ``[Unreleased]``) -- they guard the two things that would let the
package version and the reported API version silently disagree: an
unparseable value, and app/OpenAPI drifting from the package symbol.
"""

from __future__ import annotations

import re

from plex_manager import __version__

_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


def test_package_version_is_valid_semver() -> None:
    assert _SEMVER_RE.match(__version__), (
        f"__version__={__version__!r} must be a bare MAJOR.MINOR.PATCH string "
        "(the release checklist in CONTRIBUTING.md bumps it at promotion time)"
    )


def test_openapi_info_version_matches_package() -> None:
    from plex_manager.web.app import create_app

    app = create_app()
    assert app.version == __version__
    assert app.openapi()["info"]["version"] == __version__
