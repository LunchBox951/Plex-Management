"""Plex Manager — self-hosted, unified media request & automation service for Plex."""

# Single source of truth for the app/package version: hatch reads this file
# directly (see [tool.hatch.version] in pyproject.toml), FastAPI surfaces it as
# OpenAPI `info.version` (web/app.py), and events.current_build_id() falls back
# to it when no image build id is injected. Set to "1.0.0" at freeze entry so
# the Jul 25 - Aug 1 canary soaks the exact bytes carrying this label -- the
# OCI-label promotion gate (promote.yml, verified in #347) refuses to promote
# an image whose baked label doesn't match the requested version. CHANGELOG.md
# intentionally stays on `[Unreleased]` until the promotion-day release cut
# (CONTRIBUTING.md's release checklist; runbook on issue #3).
__version__ = "1.0.0"
