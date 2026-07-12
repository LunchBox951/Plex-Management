"""Lightweight guard against CHANGELOG.md drifting back to stale claims (#223).

Does not unconditionally forbid a version/date section: CONTRIBUTING.md's
release checklist tells maintainers to move `[Unreleased]` into a new
`## [x.y.z] - <date>` section *together with* bumping
`plex_manager.__version__` when cutting a real release (steps 1-2). Once that
bump has happened, a `## [x.y.z]` section is the documented, legitimate
outcome of following that checklist -- not a fabricated tag -- and asserting
its absence would make following the release flow fail this guard's own test.

So the "no fabricated release section" rule is scoped to *before* the first
real release: while `plex_manager.__version__` is still the placeholder
`0.0.0` (no release has ever been cut), no `## [x.y.z]` section should exist,
because inventing one at that point would be exactly #223's disproven
"fake tag/version/date" framing. Once the version is bumped past `0.0.0`, a
release section is expected and this guard steps aside.
"""

from __future__ import annotations

import re
from pathlib import Path

import plex_manager

_CHANGELOG = Path("CHANGELOG.md")
_RELEASE_SECTION_RE = re.compile(r"^## \[\d+\.\d+\.\d+\]", re.MULTILINE)


def test_changelog_keeps_unreleased_heading_with_no_invented_version() -> None:
    text = _CHANGELOG.read_text()

    assert "## [Unreleased]" in text
    # No `## [x.y.z] - <date>` release section should exist BEFORE the first
    # real release (see module docstring) -- once __version__ is bumped past
    # the 0.0.0 placeholder, a release section is the documented outcome of
    # the release checklist, not a fabrication, so the guard no longer applies.
    if plex_manager.__version__ == "0.0.0":
        assert not _RELEASE_SECTION_RE.search(text)


def test_changelog_no_longer_claims_backend_alpha_with_broad_deferrals() -> None:
    text = _CHANGELOG.read_text()

    assert "Backend alpha" not in text
    assert "import, Plex dedupe, retention, and the front-end are deferred" not in text


def test_changelog_covers_shipped_beta_milestones() -> None:
    text = _CHANGELOG.read_text()

    for marker in (
        "ADR-0011",  # TV support
        "ADR-0014",  # correction verbs
        "ADR-0016",  # Plex owner sessions
        "ADR-0021",  # backup/rollback policy this PR introduces
        "db_backup",
    ):
        assert marker in text, f"expected {marker!r} to be covered in CHANGELOG.md"
