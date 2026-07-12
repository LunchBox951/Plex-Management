"""Lightweight guard against CHANGELOG.md drifting back to stale claims (#223).

Does not assert a version/date section: no release has been promoted yet, and
inventing one would itself violate #223's "no fake tag/version/date" acceptance
criterion. This only guards that the curated content stays present and the
disproven "alpha, most things deferred" framing does not silently return.
"""

from __future__ import annotations

from pathlib import Path

_CHANGELOG = Path("CHANGELOG.md")


def test_changelog_keeps_unreleased_heading_with_no_invented_version() -> None:
    text = _CHANGELOG.read_text()

    assert "## [Unreleased]" in text
    # No `## [x.y.z] - <date>` release section should exist yet.
    import re

    assert not re.search(r"^## \[\d+\.\d+\.\d+\]", text, re.MULTILINE)


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
