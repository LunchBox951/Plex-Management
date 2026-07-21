"""Plex Manager — self-hosted, unified media request & automation service for Plex."""

# Single source of truth for the app/package version: hatch reads this file
# directly (see [tool.hatch.version] in pyproject.toml), FastAPI surfaces it as
# OpenAPI `info.version` (web/app.py), and events.current_build_id() falls back
# to it when no image build id is injected. Bumped off the "0.0.0" placeholder
# to track the v1 beta (CHANGELOG.md intentionally stays on `[Unreleased]` --
# this is not a release cut, see CONTRIBUTING.md's release checklist steps
# 1/3-5). The maintainer bumps this again to "1.0.0" at freeze-exit.
__version__ = "0.5.0"
